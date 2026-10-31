"""Regression tests for ROUTE-04: SR-MPLS updates must delete stale routes.

The three incremental apply methods used to send only additions and to keep
the previous-route cache when the new command list was empty. A route that
disappeared from the topology therefore stayed installed forever, and a stale
cache prevented re-installation when the route came back.
"""

from types import SimpleNamespace

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


class _FakeLabelManager:
    def __init__(self, sids):
        self._sids = sids

    def get_node_sid(self, node_id):
        return self._sids.get(node_id)


class _FakeLFIBEntry:
    def __init__(self, command):
        self._command = command

    def to_iproute2_command(self):
        return self._command


class _RecordingNode:
    """Minimal node stub that records execute_command batches."""

    def __init__(self, node_id, name, *, fail=False):
        self.id = node_id
        self.name = name
        self.container = object()
        self.interfaces = []
        self.commands = []
        self._fail = fail

    def execute_command(self, command):
        self.commands.append(command)
        if self._fail:
            raise RuntimeError("batch failed")
        return ""

    def is_addressable(self):
        return True


def _make_daemon(*, satellites=None, ground_stations=None, sids=None, sr_lfib=None):
    daemon = SRMPLSDaemon.__new__(SRMPLSDaemon)
    daemon._label_manager = _FakeLabelManager(sids or {})
    daemon._sr_graph = object()
    daemon.sr_lfib = sr_lfib if sr_lfib is not None else {}
    daemon.topology = SimpleNamespace(
        satellites=satellites or {},
        ground_stations=ground_stations or {},
    )
    daemon._prev_sr_node_sid_routes = {}
    daemon._prev_sr_gs_routes = {}
    daemon._prev_sr_sat_routes = {}
    return daemon


def test_node_sid_removed_entry_emits_mpls_delete():
    sat = _RecordingNode(1, "Sat1")
    daemon = _make_daemon(satellites={1: sat}, sids={1: 16001})

    own = "ip -f mpls route replace 16001 dev lo"
    stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev sat1.9"
    daemon._prev_sr_node_sid_routes[1] = [own, stale]

    daemon._apply_sr_node_sids_to_satellites()

    # Delete keeps only the label; next-hop attributes are dropped.
    assert sat.commands == [["sh", "-lc", "ip -f mpls route del 16009"]]
    assert daemon._prev_sr_node_sid_routes[1] == [own]


def test_ground_station_empty_route_list_deletes_and_clears_cache():
    gs = _RecordingNode(20, "GS20")
    daemon = _make_daemon(ground_stations={20: gs}, sids={})

    own = "ip -f mpls route replace 16001 dev lo"
    stale_ip = (
        "ip route replace 10.20.0.1/32 encap mpls 16001 "
        "via 10.0.0.2 dev gs20.1"
    )
    daemon._prev_sr_gs_routes[20] = [own, stale_ip]

    daemon._apply_sr_routes_to_ground_stations()

    assert gs.commands == [
        [
            "sh",
            "-lc",
            "ip -f mpls route del 16001; ip route del 10.20.0.1/32",
        ]
    ]
    # An empty route set must be committed, not leave the old cache behind.
    assert daemon._prev_sr_gs_routes[20] == []


def test_returning_route_is_reemitted_after_removal():
    sat = _RecordingNode(30, "Sat30")
    sr_lfib = {}
    daemon = _make_daemon(satellites={30: sat}, sr_lfib=sr_lfib)

    route = (
        "ip route replace 10.30.0.1/32 encap mpls 16001 "
        "via 10.0.0.2 dev sat30.1"
    )

    sr_lfib[30] = [_FakeLFIBEntry(route)]
    daemon._apply_sr_routes_to_satellites()
    assert sat.commands == [["sh", "-lc", route]]
    assert daemon._prev_sr_sat_routes[30] == [route]

    # Neighbour disappears: delete route and commit the empty cache.
    sr_lfib[30] = []
    daemon._apply_sr_routes_to_satellites()
    assert sat.commands[-1] == ["sh", "-lc", "ip route del 10.30.0.1/32"]
    assert daemon._prev_sr_sat_routes[30] == []

    # Neighbour returns: no stale cache entry may suppress the re-install.
    sr_lfib[30] = [_FakeLFIBEntry(route)]
    daemon._apply_sr_routes_to_satellites()
    assert sat.commands[-1] == ["sh", "-lc", route]
    assert daemon._prev_sr_sat_routes[30] == [route]


def test_failed_batch_keeps_cache_for_retry():
    sat = _RecordingNode(40, "Sat40", fail=True)
    previous = "ip -f mpls route replace 16001 dev lo"
    daemon = _make_daemon(
        satellites={40: sat},
        sids={},
        sr_lfib={40: [_FakeLFIBEntry("ip route replace 10.40.0.1/32 via 10.0.0.2 dev sat40.1")]},
    )
    daemon._prev_sr_sat_routes[40] = [previous]

    daemon._apply_sr_routes_to_satellites()

    assert len(sat.commands) == 1
    assert daemon._prev_sr_sat_routes[40] == [previous]
