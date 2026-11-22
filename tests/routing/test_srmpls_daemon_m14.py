"""Regression tests for stale per-source SR-MPLS routes (audit M14).

``_update_sr_route_for_source`` replaced the cached command list but never
emitted deletes for routes that disappeared from the rebuild, unlike the
full ground-station pass which diffs against its cache.  When a destination
became unreachable, its old ``ip route replace`` line stayed in the kernel.

The rebuild must prepend deletes for stale route identities in the same
fail-fast batch that installs the replacement commands.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import networkx as nx
import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.satellite import Satellite
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.srmpls_label_manager import SegmentRoutingManager

pytestmark = pytest.mark.unit


class _FakeTopology:
    def __init__(self, ground_stations, satellites, graph) -> None:
        self.ground_stations = {node.id: node for node in ground_stations}
        self.satellites = {node.id: node for node in satellites}
        self.status = True
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


def _make_ground_station(node_id: int, loopback: str) -> GroundStation:
    gs = GroundStation(f"Gnd{node_id}")
    gs.loopback.set_ip(loopback)
    gs.container = object()
    gs.execute_command = MagicMock(return_value="")
    return gs


def _make_satellite(node_id: int, loopback: str) -> Satellite:
    sat = Satellite(f"Sat{node_id}")
    sat.set_addressable(True)
    sat.loopback.set_ip(loopback)
    return sat


def _connect(left, right, left_ip: str, right_ip: str) -> None:
    left_iface = Interface(f"{left.name}.{right.id}")
    right_iface = Interface(f"{right.name}.{left.id}")
    left_iface.peer = right_iface
    right_iface.peer = left_iface
    left_iface.set_ip(left_ip)
    right_iface.set_ip(right_ip)
    left.interfaces.append(left_iface)
    right.interfaces.append(right_iface)


def _make_topology() -> tuple[_FakeTopology, GroundStation, GroundStation, nx.Graph]:
    source = _make_ground_station(0, "10.0.0.1")
    far_gs = _make_ground_station(2, "10.0.0.3")
    sat1 = _make_satellite(1, "10.0.0.2")
    sat3 = _make_satellite(3, "10.0.0.4")
    sat3.set_addressable(False)
    _connect(source, sat1, "10.10.0.1", "10.10.0.2")
    _connect(sat1, far_gs, "10.10.0.3", "10.10.0.4")
    _connect(source, sat3, "10.10.0.5", "10.10.0.6")
    _connect(sat3, far_gs, "10.10.0.7", "10.10.0.8")

    graph = nx.Graph()
    graph.add_edge(source.id, sat1.id, weight=1)
    graph.add_edge(sat1.id, far_gs.id, weight=1)
    graph.add_edge(source.id, sat3.id, weight=1)
    graph.add_edge(sat3.id, far_gs.id, weight=1)

    topology = _FakeTopology([source, far_gs], [sat1, sat3], graph)
    return topology, source, far_gs, graph


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
    daemon._enable_mpls = lambda node: None
    return daemon


def _batch_text(source: GroundStation) -> str:
    return source.execute_command.call_args.args[0][2]


def test_unreachable_destination_is_deleted_from_kernel_and_cache() -> None:
    topology, source, far_gs, graph = _make_topology()
    daemon = _make_daemon(topology)

    daemon._update_sr_route_for_source(source.id)
    first_batch = _batch_text(source)
    assert f"ip route replace {far_gs.loopback.ipv4}/32" in first_batch
    assert any(
        far_gs.loopback.ipv4 in line for line in daemon._prev_sr_gs_routes[source.id]
    )

    # The transit links to the far ground station disappear.
    graph.remove_edge(1, far_gs.id)
    graph.remove_edge(3, far_gs.id)
    source.execute_command.reset_mock()

    daemon._update_sr_route_for_source(source.id)

    second_batch = _batch_text(source)
    assert second_batch.startswith("set -e; ")
    assert f"ip route del {far_gs.loopback.ipv4}/32" in second_batch
    assert not any(
        far_gs.loopback.ipv4 in line for line in daemon._prev_sr_gs_routes[source.id]
    )


def test_changed_path_replaces_the_route_without_deleting_it() -> None:
    topology, source, far_gs, _graph = _make_topology()
    daemon = _make_daemon(topology)
    daemon._update_sr_route_for_source(source.id)
    source.execute_command.reset_mock()

    daemon._label_manager.set_custom_path(
        source.id, far_gs.id, [source.id, 3, far_gs.id]
    )
    daemon._update_sr_route_for_source(source.id)

    batch = _batch_text(source)
    assert f"ip route del {far_gs.loopback.ipv4}/32" not in batch
    assert f"ip route replace {far_gs.loopback.ipv4}/32" in batch


class _KernelBackend:
    """Minimal emulation of the iproute2 batches executed by the daemon.

    A tolerant delete is generated as an existence check followed by the
    delete, so a ``route del`` for a prefix the kernel no longer has is
    skipped. A failing delete of a present route aborts the ``set -e`` batch.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], str] = {}
        self.batches: list[str] = []
        self.fail_next_install = False
        self.delete_error: Exception | None = None
        self._delete_allowed = False

    def __call__(self, argv) -> str:
        batch = argv[2]
        self.batches.append(batch)
        for command in batch.split("; "):
            command = command.strip()
            if not command or command == "set -e":
                continue
            if command.startswith("if "):
                self._delete_allowed = self._route_present(command[3:])
                continue
            if command == "fi":
                self._delete_allowed = False
                continue
            if command.startswith("then "):
                if self._delete_allowed:
                    self._run(command[len("then ") :])
                continue
            self._run(command)
        return ""

    def _route_present(self, condition: str) -> bool:
        show = condition.removesuffix(" 2>&1 | grep -q .")
        parts = show.split()
        if parts[:5] == ["ip", "-f", "mpls", "route", "show"]:
            return ("mpls", parts[6]) in self.routes
        if parts[:4] == ["ip", "route", "show", "exact"]:
            return ("ip", parts[4]) in self.routes
        raise AssertionError(f"unexpected condition: {condition!r}")

    def _run(self, command: str) -> None:
        if command.startswith("ip route del "):
            self._delete(("ip", command.split()[3]))
        elif command.startswith("ip -f mpls route del "):
            self._delete(("mpls", command.split()[5]))
        elif command.startswith("ip route replace "):
            self._install(("ip", command.split()[3]), command)
        elif command.startswith("ip -f mpls route replace "):
            self._install(("mpls", command.split()[5]), command)
        else:
            raise AssertionError(f"unexpected command: {command}")

    def _delete(self, key: tuple[str, str]) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        if key not in self.routes:
            raise RuntimeError("RTNETLINK answers: No such process")
        del self.routes[key]

    def _install(self, key: tuple[str, str], command: str) -> None:
        if self.fail_next_install:
            self.fail_next_install = False
            raise RuntimeError("RTNETLINK answers: Network is down")
        self.routes[key] = command


def test_stale_route_absent_from_kernel_does_not_wedge_the_update() -> None:
    """A delete of an already-absent route must not abort the whole batch.

    A container restart (or a partially applied earlier batch) can leave the
    cache holding a route the kernel no longer has.  ``set -e`` would abort at
    that delete, the cache would keep the stale key, and every later update
    would fail at the same line.
    """
    topology, source, far_gs, graph = _make_topology()
    daemon = _make_daemon(topology)
    backend = _KernelBackend()
    source.execute_command = backend

    daemon._update_sr_route_for_source(source.id)

    stale_line = "ip route replace 10.9.9.9/32 via 10.10.0.2 dev Gnd0.2"
    daemon._prev_sr_gs_routes[source.id].append(stale_line)
    assert ("ip", "10.9.9.9/32") not in backend.routes

    # Force a route change so the same batch also installs new routes.
    graph.remove_edge(source.id, 1)
    daemon._update_sr_route_for_source(source.id)

    batch = backend.batches[-1]
    assert (
        "if ip route show exact 10.9.9.9/32 2>&1 | grep -q .; "
        "then ip route del 10.9.9.9/32; fi"
    ) in batch
    assert stale_line not in daemon._prev_sr_gs_routes[source.id]

    far_route = backend.routes[("ip", f"{far_gs.loopback.ipv4}/32")]
    assert "16003" in far_route, "the replacement route was not installed"
    assert any(
        "16003" in line and far_gs.loopback.ipv4 in line
        for line in daemon._prev_sr_gs_routes[source.id]
    ), "the cache did not commit the surviving routes"


def test_failed_install_after_tolerant_delete_recovers_on_next_update() -> None:
    topology, source, far_gs, graph = _make_topology()
    daemon = _make_daemon(topology)
    backend = _KernelBackend()
    source.execute_command = backend

    daemon._update_sr_route_for_source(source.id)

    stale_line = "ip route replace 10.9.9.9/32 via 10.10.0.2 dev Gnd0.2"
    daemon._prev_sr_gs_routes[source.id].append(stale_line)
    graph.remove_edge(source.id, 1)
    backend.fail_next_install = True

    daemon._update_sr_route_for_source(source.id)

    # The batch aborted after the stale delete; the cache must not claim the
    # new routes were installed.
    assert stale_line in daemon._prev_sr_gs_routes[source.id]
    assert ("ip", f"{far_gs.loopback.ipv4}/32") in backend.routes
    assert "16003" not in backend.routes[("ip", f"{far_gs.loopback.ipv4}/32")]

    daemon._update_sr_route_for_source(source.id)

    assert stale_line not in daemon._prev_sr_gs_routes[source.id]
    assert "16003" in backend.routes[("ip", f"{far_gs.loopback.ipv4}/32")]
    assert any(
        "16003" in line and far_gs.loopback.ipv4 in line
        for line in daemon._prev_sr_gs_routes[source.id]
    )
