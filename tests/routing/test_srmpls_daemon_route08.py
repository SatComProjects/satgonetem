"""Regression tests for ROUTE-08: SR-MPLS batches must fail fast.

The daemon used to join route commands with ``"; "`` and run them through
``sh -lc``. POSIX ``sh`` reports only the status of the last command, so a
failure in an earlier command was invisible: the daemon committed the
``_prev_sr_*`` caches as if the whole batch had been installed.

All batches must therefore start with ``set -e`` (or an equivalent fail-fast
form), and the previous-route cache must only be committed when
``execute_command`` actually succeeds.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import networkx as nx

from satgonetem.models.ground_station import GroundStation
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


class _FakeLabelManager:
    def __init__(self, sids):
        self._sids = sids

    def get_node_sid(self, node_id):
        return self._sids.get(node_id)

    def get_custom_path(self, source_id, destination_id):
        return None

    def get_label_stack_for_path(self, path, use_php=False):
        return [16002]


class _FakeLFIBEntry:
    def __init__(self, command):
        self._command = command

    def to_iproute2_command(self):
        return self._command


class _RecordingNode:
    """Minimal node stub that records the batches passed to execute_command."""

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


def _batch_texts(node):
    return [call[2] for call in node.commands]


def test_node_sid_batch_is_fail_fast():
    sat = _RecordingNode(1, "Sat1")
    daemon = _make_daemon(satellites={1: sat}, sids={1: 16001})

    daemon._apply_sr_node_sids_to_satellites()

    assert sat.commands
    assert all(text.startswith("set -e; ") for text in _batch_texts(sat))


def test_ground_station_batch_is_fail_fast():
    gs = _RecordingNode(20, "GS20")
    daemon = _make_daemon(ground_stations={20: gs}, sids={20: 16020})

    daemon._apply_sr_routes_to_ground_stations()

    assert gs.commands
    assert all(text.startswith("set -e; ") for text in _batch_texts(gs))


def test_satellite_batch_is_fail_fast():
    sat = _RecordingNode(30, "Sat30")
    daemon = _make_daemon(satellites={30: sat}, sids={30: 16030})

    daemon._apply_sr_routes_to_satellites()

    assert sat.commands
    assert all(text.startswith("set -e; ") for text in _batch_texts(sat))


def test_failed_apply_batch_does_not_commit_cache():
    sat = _RecordingNode(40, "Sat40", fail=True)
    previous = "ip -f mpls route replace 16001 dev lo"
    daemon = _make_daemon(
        satellites={40: sat},
        sids={},
        sr_lfib={
            40: [
                _FakeLFIBEntry(
                    "ip route replace 10.40.0.1/32 via 10.0.0.2 dev sat40.1"
                )
            ]
        },
    )
    daemon._prev_sr_sat_routes[40] = [previous]

    daemon._apply_sr_routes_to_satellites()

    assert len(sat.commands) == 1
    assert sat.commands[0][2].startswith("set -e; ")
    assert daemon._prev_sr_sat_routes[40] == [previous]


def _update_daemon(*, fail):
    source = GroundStation("Gnd1")
    destination = GroundStation("Gnd2")
    source.container = object()
    destination.loopback.ipv4 = "10.0.0.2"

    interface = SimpleNamespace(
        peer=SimpleNamespace(ipv4="10.0.0.2"),
        get_iname=lambda: "gnd1.2",
    )

    graph = nx.Graph()
    graph.add_edge(source.id, destination.id, weight=1)

    source.execute_command = MagicMock(
        side_effect=RuntimeError("batch failed") if fail else None,
        return_value="",
    )

    ground_stations = {source.id: source, destination.id: destination}
    daemon = _make_daemon(ground_stations=ground_stations, sids={source.id: 16001})
    daemon.topology.get_current_graph = lambda: graph
    daemon.topology.get_ground_stations = lambda: [source, destination]
    daemon.topology._get_interface_to_peer = lambda node, peer_id: interface
    daemon.mpls_config = SimpleNamespace(use_php=False)
    daemon._enable_mpls = lambda node: None
    return daemon, source


def test_update_sr_route_for_source_batch_is_fail_fast():
    daemon, source = _update_daemon(fail=False)

    daemon._update_sr_route_for_source(source.id)

    source.execute_command.assert_called_once()
    batch = source.execute_command.call_args.args[0]
    assert batch[:2] == ["sh", "-lc"]
    assert batch[2].startswith("set -e; ")
    assert daemon._prev_sr_gs_routes[source.id]


def test_failed_update_sr_route_for_source_does_not_commit_cache():
    daemon, source = _update_daemon(fail=True)
    previous = ["ip -f mpls route replace 16001 dev lo"]
    daemon._prev_sr_gs_routes[source.id] = previous.copy()

    daemon._update_sr_route_for_source(source.id)

    source.execute_command.assert_called_once()
    assert daemon._prev_sr_gs_routes[source.id] == previous
