"""Regression tests for ROUTE-09: SR-MPLS init failure reporting.

``SRMPLSDaemon.init()`` used to return ``True`` unconditionally even when
MPLS enablement or route installation failed. These tests verify that error
counts propagate from the per-node apply steps through ``init()`` while
``update()`` keeps its ``None`` contract.

No containers are involved; node objects are mocks and the heavy internals
(label allocation, route building) are patched.
"""

from unittest.mock import MagicMock, patch

import pytest

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


def make_topology(gonetem_is_on: bool = True) -> MagicMock:
    """Build a minimal topology mock for SRMPLSDaemon."""
    topology = MagicMock()
    topology.gonetem_is_on = gonetem_is_on
    topology.satellites = {}
    topology.ground_stations = {}
    return topology


@pytest.fixture
def topology() -> MagicMock:
    return make_topology()


@pytest.fixture
def daemon(topology: MagicMock) -> SRMPLSDaemon:
    return SRMPLSDaemon(topology)


def make_node(name: str, node_id: int) -> MagicMock:
    """Build a container-backed node mock that fails command execution."""
    node = MagicMock()
    node.name = name
    node.id = node_id
    node.container = object()
    node.execute_command.side_effect = RuntimeError("container error")
    return node


class TestInitErrorReporting:
    """init() must reflect failures reported by the apply/enable steps."""

    def test_returns_false_when_enable_step_reports_errors(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_configure_mpls"),
            patch.object(daemon, "_init_sr_mpls"),
            patch.object(daemon, "_build_sr_gs_routes"),
            patch.object(daemon, "_enable_mpls_on_all_nodes", return_value=1),
            patch.object(daemon, "_apply_sr_node_sids_to_satellites", return_value=0),
            patch.object(daemon, "_apply_sr_routes_to_ground_stations", return_value=0),
            patch.object(daemon, "_apply_sr_routes_to_satellites", return_value=0),
        ):
            result = daemon.init()

        assert result is False

    def test_returns_false_when_apply_step_reports_errors(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_configure_mpls"),
            patch.object(daemon, "_init_sr_mpls"),
            patch.object(daemon, "_build_sr_gs_routes"),
            patch.object(daemon, "_enable_mpls_on_all_nodes", return_value=0),
            patch.object(daemon, "_apply_sr_node_sids_to_satellites", return_value=2),
            patch.object(daemon, "_apply_sr_routes_to_ground_stations", return_value=0),
            patch.object(daemon, "_apply_sr_routes_to_satellites", return_value=0),
        ):
            result = daemon.init()

        assert result is False

    def test_returns_true_when_all_steps_succeed(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_configure_mpls"),
            patch.object(daemon, "_init_sr_mpls"),
            patch.object(daemon, "_build_sr_gs_routes"),
            patch.object(daemon, "_enable_mpls_on_all_nodes", return_value=0),
            patch.object(daemon, "_apply_sr_node_sids_to_satellites", return_value=0),
            patch.object(daemon, "_apply_sr_routes_to_ground_stations", return_value=0),
            patch.object(daemon, "_apply_sr_routes_to_satellites", return_value=0),
        ):
            result = daemon.init()

        assert result is True


class TestErrorCountAggregation:
    """_apply_sr_mpls_routes() must sum errors from every step."""

    def test_aggregates_enable_and_all_apply_steps(
        self, daemon: SRMPLSDaemon
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_enable_mpls_on_all_nodes", return_value=1),
            patch.object(daemon, "_apply_sr_node_sids_to_satellites", return_value=2),
            patch.object(daemon, "_apply_sr_routes_to_ground_stations", return_value=3),
            patch.object(daemon, "_apply_sr_routes_to_satellites", return_value=4),
        ):
            errors = daemon._apply_sr_mpls_routes(init_flag=True)

        assert errors == 10

    def test_aggregates_same_steps_during_update(
        self, daemon: SRMPLSDaemon
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_enable_mpls_on_all_nodes", return_value=1) as enable,
            patch.object(daemon, "_apply_sr_node_sids_to_satellites", return_value=2),
            patch.object(daemon, "_apply_sr_routes_to_ground_stations", return_value=3),
            patch.object(daemon, "_apply_sr_routes_to_satellites", return_value=4),
        ):
            errors = daemon._apply_sr_mpls_routes(init_flag=False)

        assert errors == 10
        # ROUTE-05 made MPLS enablement run on every apply, not only on init.
        enable.assert_called_once_with(max_workers=32)

    def test_rebuild_propagates_apply_errors(
        self, daemon: SRMPLSDaemon
    ) -> None:
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_build_sr_gs_routes"),
            patch.object(daemon, "_apply_sr_mpls_routes", return_value=5),
        ):
            errors = daemon._rebuild_sr_mpls_routing(init_flag=True)

        assert errors == 5

    def test_rebuild_returns_zero_when_simulator_is_off(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        topology.gonetem_is_on = False
        daemon._label_manager = MagicMock()

        with (
            patch.object(daemon, "_build_sr_gs_routes"),
            patch.object(daemon, "_apply_sr_mpls_routes") as apply_routes,
        ):
            errors = daemon._rebuild_sr_mpls_routing(init_flag=True)

        assert errors == 0
        apply_routes.assert_not_called()

    def test_update_keeps_none_contract(self, daemon: SRMPLSDaemon) -> None:
        with patch.object(
            daemon, "_rebuild_sr_mpls_routing", return_value=3
        ) as rebuild:
            result = daemon.update([])

        assert result is None
        rebuild.assert_called_once_with(init_flag=False, max_workers=128)


class TestPerStepErrorCounting:
    """The real enable/apply loops must count per-node failures."""

    def test_enable_mpls_counts_node_failures(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        topology.satellites = {
            0: make_node("Sat0", 0),
            1: make_node("Sat1", 1),
        }

        def enable(node: MagicMock) -> None:
            if node.name == "Sat1":
                raise RuntimeError("container error")

        with patch.object(daemon, "_enable_mpls", side_effect=enable):
            errors = daemon._enable_mpls_on_all_nodes(max_workers=2)

        assert errors == 1

    def test_ground_station_apply_counts_failures(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        gs = make_node("Gnd0", 0)
        topology.ground_stations = {0: gs}
        daemon._label_manager = MagicMock()
        daemon._label_manager.get_node_sid.return_value = 16000

        errors = daemon._apply_sr_routes_to_ground_stations()

        assert errors == 1

    def test_satellite_apply_counts_failures(
        self, daemon: SRMPLSDaemon, topology: MagicMock
    ) -> None:
        sat = make_node("Sat0", 0)
        sat.is_addressable.return_value = True
        topology.satellites = {0: sat}
        daemon._label_manager = MagicMock()
        daemon._label_manager.get_node_sid.return_value = 16000

        errors = daemon._apply_sr_routes_to_satellites()

        assert errors == 1
