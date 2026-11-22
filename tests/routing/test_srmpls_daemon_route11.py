"""Regression tests for ROUTE-11: stale deletes in the satellite SR passes.

Both satellite apply passes run their route batch with ``set -e``. A cached
route can outlive the kernel entry (container restart, or a partially applied
earlier batch), so an unguarded ``route del`` for an already-absent route
aborts the whole batch and the cache never commits: every later update wedges
at the same line. Deletes of stale routes must tolerate absence; install
failures must stay fatal and keep the cache for the next retry.
"""

from types import SimpleNamespace

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


class _Kernel:
    """Route table that fails like iproute2, including ``|| true`` handling."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], str] = {}
        self.batches: list[str] = []
        self.fail_next_install = False

    def execute(self, argv: list[str]) -> str:
        assert argv[:2] == ["sh", "-lc"], argv
        batch = argv[2]
        self.batches.append(batch)
        for command in batch.split("; "):
            command = command.strip()
            if not command or command == "set -e":
                continue
            if command.startswith("ip route del "):
                self._delete(("ip", command.split()[3]), command)
            elif command.startswith("ip -f mpls route del "):
                self._delete(("mpls", command.split()[5]), command)
            elif command.startswith("ip route replace "):
                self._install(("ip", command.split()[3]), command)
            elif command.startswith("ip -f mpls route replace "):
                self._install(("mpls", command.split()[5]), command)
            else:
                raise AssertionError(f"unexpected command: {command}")
        return ""

    def _delete(self, key: tuple[str, str], command: str) -> None:
        if key not in self.routes:
            if "2>/dev/null || true" in command:
                return
            raise RuntimeError("RTNETLINK answers: No such process")
        del self.routes[key]

    def _install(self, key: tuple[str, str], command: str) -> None:
        if self.fail_next_install:
            self.fail_next_install = False
            raise RuntimeError("RTNETLINK answers: Network is down")
        self.routes[key] = command


class _Node:
    def __init__(self, node_id: int, name: str) -> None:
        self.id = node_id
        self.name = name
        self.container = object()
        self.interfaces: list = []
        self.kernel = _Kernel()

    def execute_command(self, argv: list[str]) -> str:
        return self.kernel.execute(argv)

    def is_addressable(self) -> bool:
        return True


class _FakeLFIBEntry:
    def __init__(self, command: str) -> None:
        self._command = command

    def to_iproute2_command(self) -> str:
        return self._command


class _FakeLabelManager:
    def __init__(self, sids: dict[int, int]) -> None:
        self._sids = sids

    def get_node_sid(self, node_id: int) -> int | None:
        return self._sids.get(node_id)


def _make_daemon(
    satellites: list[_Node],
    *,
    sids: dict[int, int] | None = None,
) -> SRMPLSDaemon:
    daemon = SRMPLSDaemon.__new__(SRMPLSDaemon)
    daemon._label_manager = _FakeLabelManager(sids or {})
    daemon._sr_graph = object()
    daemon.sr_lfib = {}
    daemon.topology = SimpleNamespace(
        satellites={sat.id: sat for sat in satellites},
        ground_stations={},
    )
    daemon._prev_sr_node_sid_routes = {}
    daemon._prev_sr_gs_routes = {}
    daemon._prev_sr_sat_routes = {}
    return daemon


def test_absent_stale_route_does_not_wedge_satellite_stack_pass() -> None:
    """A stack-pass delete of an already-absent prefix must be tolerated."""
    sat = _Node(30, "Sat30")
    daemon = _make_daemon([sat], sids={30: 16030})
    route = "ip route replace 10.30.0.1/32 encap mpls 16030 via 10.0.0.2 dev Sat30.1"
    daemon.sr_lfib[30] = [_FakeLFIBEntry(route)]
    stale = "ip route replace 10.30.0.9/32 encap mpls 16009 via 10.0.0.9 dev Sat30.9"
    daemon._prev_sr_sat_routes[30] = [stale]

    assert daemon._apply_sr_routes_to_satellites() == 0

    batch = sat.kernel.batches[-1]
    assert "ip route del 10.30.0.9/32 2>/dev/null || true" in batch
    assert sat.kernel.routes[("ip", "10.30.0.1/32")] == route
    assert daemon._prev_sr_sat_routes[30] == [route]


def test_absent_stale_route_does_not_wedge_node_sid_pass() -> None:
    """A SID-pass delete of an already-absent label must be tolerated."""
    sat = _Node(30, "Sat30")
    daemon = _make_daemon([sat], sids={30: 16030})
    own = "ip -f mpls route replace 16030 dev lo"
    stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev Sat30.9"
    daemon._prev_sr_node_sid_routes[30] = [stale]

    assert daemon._apply_sr_node_sids_to_satellites() == 0

    batch = sat.kernel.batches[-1]
    assert "ip -f mpls route del 16009 2>/dev/null || true" in batch
    assert sat.kernel.routes[("mpls", "16030")] == own
    assert daemon._prev_sr_node_sid_routes[30] == [own]


def test_satellite_stack_pass_install_failure_still_aborts() -> None:
    """Tolerating a stale delete must not swallow a failed install."""
    sat = _Node(30, "Sat30")
    daemon = _make_daemon([sat], sids={30: 16030})
    route = "ip route replace 10.30.0.1/32 encap mpls 16030 via 10.0.0.2 dev Sat30.1"
    daemon.sr_lfib[30] = [_FakeLFIBEntry(route)]
    stale = "ip route replace 10.30.0.9/32 encap mpls 16009 via 10.0.0.9 dev Sat30.9"
    daemon._prev_sr_sat_routes[30] = [stale]
    sat.kernel.fail_next_install = True

    assert daemon._apply_sr_routes_to_satellites() == 1

    assert ("ip", "10.30.0.1/32") not in sat.kernel.routes
    assert daemon._prev_sr_sat_routes[30] == [stale]


def test_node_sid_pass_install_failure_still_aborts() -> None:
    """The SID pass keeps its cache when the install part of the batch fails."""
    sat = _Node(30, "Sat30")
    daemon = _make_daemon([sat], sids={30: 16030})
    stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev Sat30.9"
    daemon._prev_sr_node_sid_routes[30] = [stale]
    sat.kernel.routes[("mpls", "16009")] = stale
    sat.kernel.fail_next_install = True

    assert daemon._apply_sr_node_sids_to_satellites() == 1

    assert ("mpls", "16030") not in sat.kernel.routes
    assert daemon._prev_sr_node_sid_routes[30] == [stale]
