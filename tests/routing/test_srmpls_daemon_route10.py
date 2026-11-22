"""Regression tests for ROUTE-10: one owner per satellite route.

The satellite SID pass and the satellite label-stack pass each installed its
own copy of the node's SID and an IPv4 route for a directly connected ground
station (plain in the SID pass, ``encap mpls`` in the stack pass). Each pass
kept its own ``_prev_sr_*`` cache, so a topology change could make one pass
delete a prefix the other pass had installed, and both passes could delete the
same prefix: the first delete succeeded, the second ``set -e`` batch failed,
its cache never committed, and the failure repeated on every update.

These tests drive consecutive real updates with a fake kernel that rejects
deleting a missing route. They assert that a satellite route has exactly one
owner, that no pass deletes a route owned by the other, that batches succeed,
that caches commit, and that the kernel converges to the cached state.
"""

from types import SimpleNamespace

import networkx as nx

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager


class _FakeKernel:
    """Route table that fails like iproute2 when deleting a missing route."""

    def __init__(self) -> None:
        self.mpls: dict[str, str] = {}
        self.ip: dict[str, str] = {}
        self.batches: list[str] = []
        self.failures: list[str] = []

    def execute(self, command: list[str]) -> str:
        assert command[:2] == ["sh", "-lc"], command
        batch = command[2]
        self.batches.append(batch)
        try:
            for line in batch.split("; "):
                self._run(line)
        except RuntimeError as exc:
            self.failures.append(str(exc))
            raise
        return ""

    def _run(self, line: str) -> None:
        parts = line.split()
        if parts == ["set", "-e"]:
            return
        if parts[:4] == ["ip", "-f", "mpls", "route"] and parts[4] == "del":
            if parts[5] not in self.mpls:
                raise RuntimeError(f"mpls label {parts[5]} missing")
            del self.mpls[parts[5]]
            return
        if parts[:4] == ["ip", "-f", "mpls", "route"] and parts[4] == "replace":
            self.mpls[parts[5]] = line
            return
        if parts[:3] == ["ip", "route", "del"]:
            if parts[3] not in self.ip:
                raise RuntimeError(f"ip prefix {parts[3]} missing")
            del self.ip[parts[3]]
            return
        if parts[:3] == ["ip", "route", "replace"]:
            self.ip[parts[3]] = line
            return
        raise RuntimeError(f"unsupported command: {line!r}")


class _FakeTopology:
    """Topology stub exposing the lookups used by SR-MPLS route building."""

    def __init__(self, satellites, ground_stations, graph) -> None:
        self.satellites = {sat.id: sat for sat in satellites}
        self.ground_stations = {gs.id: gs for gs in ground_stations}
        self.gonetem_is_on = True
        self._graph = graph

    def get_ground_stations(self):
        return list(self.ground_stations.values())

    def get_satellites(self):
        return list(self.satellites.values())

    def get_current_graph(self):
        return self._graph

    def _get_interface_to_peer(self, node, peer_id):
        for iface in node.interfaces:
            try:
                if int(iface.name.split(".")[1]) == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None


def _make_satellite(node_id: int, loopback: str) -> Satellite:
    sat = Satellite(f"Sat{node_id}")
    sat.set_addressable(True)
    sat.loopback.set_ip(loopback)
    sat.container = object()
    sat.kernel = _FakeKernel()
    sat.execute_command = sat.kernel.execute
    return sat


def _make_ground_station(node_id: int, loopback: str, kernel=None) -> GroundStation:
    gs = GroundStation(f"Gnd{node_id}")
    gs.loopback.set_ip(loopback)
    gs.container = object()
    gs.kernel = kernel if kernel is not None else _FakeKernel()
    gs.execute_command = gs.kernel.execute
    return gs


def _connect(left, right, left_ip: str, right_ip: str) -> tuple[Interface, Interface]:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)
    return left_iface, right_iface


def _make_daemon(topology: _FakeTopology) -> SRMPLSDaemon:
    daemon = SRMPLSDaemon(topology)
    manager = SegmentRoutingManager(node_sid_base=16000)
    for node in [
        *topology.satellites.values(),
        *topology.ground_stations.values(),
    ]:
        manager.allocate_node_sid(node.id, node.name)
    daemon._label_manager = manager
    daemon.mpls_config = SimpleNamespace(use_php=False)
    daemon._enable_mpls_on_all_nodes = lambda max_workers=32: 0
    return daemon


def _route_key(command: str) -> tuple[str, str]:
    parts = command.split()
    if (
        parts[:4] == ["ip", "-f", "mpls", "route"]
        and parts[4] == "replace"
        and len(parts) >= 6
    ):
        return ("mpls", parts[5])
    if parts[:3] == ["ip", "route", "replace"] and len(parts) >= 5:
        return ("ip", parts[3])
    raise AssertionError(f"unexpected route command: {command!r}")


def _cache_keys(daemon: SRMPLSDaemon, sat_id: int) -> tuple[set, set]:
    sid_keys = {
        _route_key(line) for line in daemon._prev_sr_node_sid_routes.get(sat_id, [])
    }
    stack_keys = {
        _route_key(line) for line in daemon._prev_sr_sat_routes.get(sat_id, [])
    }
    return sid_keys, stack_keys


def _assert_one_owner_and_convergence(daemon: SRMPLSDaemon, sat: Satellite) -> None:
    """Caches must be disjoint and together describe the installed kernel state."""
    sid_keys, stack_keys = _cache_keys(daemon, sat.id)
    assert not sid_keys & stack_keys, "satellite route identity has two owners"

    cached = {key for key in sid_keys | stack_keys}
    installed = {("mpls", label) for label in sat.kernel.mpls} | {
        ("ip", prefix) for prefix in sat.kernel.ip
    }
    assert installed == cached, (
        f"kernel diverged from caches: installed={installed}, cached={cached}"
    )


def _install_lines_in(batches: list[str], prefix: str) -> list[str]:
    return [
        line
        for batch in batches
        for line in batch.split("; ")
        if line.startswith(f"ip route replace {prefix} ")
    ]


def _delete_lines(kernel: _FakeKernel, prefix: str) -> list[str]:
    return [
        line
        for batch in kernel.batches
        for line in batch.split("; ")
        if line == f"ip route del {prefix}"
    ]


def test_removed_destination_is_deleted_once_and_caches_commit():
    """Both passes used to re-delete a removed prefix forever."""
    sat = _make_satellite(1, "10.0.0.1")
    gs = _make_ground_station(2, "10.0.0.2")
    _connect(sat, gs, "10.10.0.1", "10.10.0.2")

    graph = nx.Graph()
    graph.add_edge(sat.id, gs.id, weight=1)
    topology = _FakeTopology([sat], [gs], graph)
    daemon = _make_daemon(topology)

    assert daemon._rebuild_sr_mpls_routing(init_flag=True) == 0
    assert sat.kernel.ip.keys() == {"10.0.0.2/32"}

    # The ground-station link disappears: both satellite passes lose the route.
    sat.interfaces.clear()
    gs.interfaces.clear()
    graph.remove_edge(sat.id, gs.id)

    batches_before = len(sat.kernel.batches)
    assert daemon._rebuild_sr_mpls_routing(init_flag=False) == 0
    assert sat.kernel.failures == []
    assert _delete_lines(sat.kernel, "10.0.0.2/32") == ["ip route del 10.0.0.2/32"], (
        "the prefix must be deleted exactly once"
    )
    assert daemon._prev_sr_sat_routes[sat.id] == []
    assert daemon._prev_sr_node_sid_routes[sat.id] == [
        "ip -f mpls route replace 16001 dev lo"
    ]
    _assert_one_owner_and_convergence(daemon, sat)

    # A third update must be a no-op instead of retrying the failed delete.
    batches_after_convergence = len(sat.kernel.batches)
    assert daemon._rebuild_sr_mpls_routing(init_flag=False) == 0
    assert len(sat.kernel.batches) == batches_after_convergence
    assert sat.kernel.failures == []
    assert len(sat.kernel.batches) > batches_before  # the second update did work
    _assert_one_owner_and_convergence(daemon, sat)


def test_path_change_does_not_delete_prefix_owned_by_stack_pass():
    """Losing a direct link must not let the SID pass delete the stack route."""
    sat1 = _make_satellite(1, "10.0.0.1")
    sat3 = _make_satellite(3, "10.0.0.3")
    gs2 = _make_ground_station(2, "10.0.0.2")

    _connect(sat1, sat3, "10.10.0.1", "10.10.0.2")
    _connect(sat3, gs2, "10.10.0.5", "10.10.0.6")
    _, gs2_direct_iface = _connect(sat1, gs2, "10.10.0.9", "10.10.0.10")

    graph = nx.Graph()
    graph.add_edge(sat1.id, sat3.id, weight=1)
    graph.add_edge(sat3.id, gs2.id, weight=1)
    graph.add_edge(sat1.id, gs2.id, weight=1)
    topology = _FakeTopology([sat1, sat3], [gs2], graph)
    daemon = _make_daemon(topology)

    assert daemon._rebuild_sr_mpls_routing(init_flag=True) == 0

    # Direct link disappears. The stack route survives via Sat3; the SID pass
    # must not emit a stale delete for the prefix the stack pass still owns.
    sat1.interfaces = [iface for iface in sat1.interfaces if iface.name != "Sat1.2"]
    gs2.interfaces = [
        iface for iface in gs2.interfaces if iface is not gs2_direct_iface
    ]
    graph.remove_edge(sat1.id, gs2.id)

    batches_before = len(sat1.kernel.batches)
    assert daemon._rebuild_sr_mpls_routing(init_flag=False) == 0

    assert _delete_lines(sat1.kernel, "10.0.0.2/32") == []
    assert len(sat1.kernel.batches) > batches_before
    assert sat1.kernel.failures == []
    route = sat1.kernel.ip["10.0.0.2/32"]
    assert "encap mpls 16002 via 10.10.0.2 dev eth3" in route
    _assert_one_owner_and_convergence(daemon, sat1)

    # Directly connected GS: only the stack pass owns its prefix, and the SID
    # pass owns the node's own SID. Neither may be installed twice per update.
    first_update_batches = sat1.kernel.batches[:batches_before]
    first_update_installs = _install_lines_in(first_update_batches, "10.0.0.2/32")
    assert len(first_update_installs) == 1
    assert "via 10.10.0.10 dev eth2" in first_update_installs[0]
    own_sid_installs = [
        line
        for batch in first_update_batches
        for line in batch.split("; ")
        if line.startswith("ip -f mpls route replace 16001 ")
    ]
    assert own_sid_installs == ["ip -f mpls route replace 16001 dev lo"]
