"""Unit tests for HILManager.

All kernel and subprocess calls (setns, IPRoute, nsenter, docker) are mocked
so these tests run without root privileges or real hardware.

HILManager is standalone: it does not subclass any launcher. These tests
verify routing logic, state tracking, and the public interface used by
TopologyManager.
"""

import pytest
from unittest.mock import MagicMock, patch, call

from satgonetem.launchers.hil_manager import HILManager


# Fixtures


@pytest.fixture
def manager():
    """HILManager with Gnd0 mapped to eth1."""
    return HILManager(
        gnd_hardware_map={"Gnd0": "eth1"},
        gnd_capacity_kbps=50_000,
    )


def _make_node(name: str, container_pid: int = 0):
    """Build a minimal mock node."""
    node = MagicMock()
    node.name = name
    node.id = int(name[3:])
    node.container_pid = container_pid
    return node


def _make_link(src_name: str, tgt_name: str, link_type: str, delay: int = 10, src_pid: int = 0, tgt_pid: int = 0):
    """Build a minimal mock link."""
    link = MagicMock()
    link.source = _make_node(src_name, src_pid)
    link.target = _make_node(tgt_name, tgt_pid)
    link.type = link_type
    link.delay = delay
    link.is_active = True
    link.peer1_capacity = 0
    link.peer2_capacity = 0
    return link


# is_hil_node


class TestIsHilNode:
    def test_returns_true_for_mapped_gnd(self, manager):
        assert manager.is_hil_node("Gnd0") is True

    def test_returns_false_for_unmapped_gnd(self, manager):
        assert manager.is_hil_node("Gnd1") is False

    def test_returns_false_for_satellite(self, manager):
        assert manager.is_hil_node("Sat0") is False


# is_hil_link


class TestIsHilLink:
    def test_true_when_target_is_hil_gnd(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink")
        assert manager.is_hil_link(link) is True

    def test_true_when_source_is_hil_gnd(self, manager):
        link = _make_link("Gnd0", "Sat0", "GroundStationLink")
        assert manager.is_hil_link(link) is True

    def test_false_for_isl(self, manager):
        link = _make_link("Sat0", "Sat1", "InterSatelliteLink")
        assert manager.is_hil_link(link) is False

    def test_false_when_gnd_not_in_map(self, manager):
        link = _make_link("Sat0", "Gnd1", "GroundStationLink")
        assert manager.is_hil_link(link) is False

    def test_false_for_link_missing_type(self, manager):
        link = MagicMock(spec=[])
        assert manager.is_hil_link(link) is False


# _get_hil_gnd / _get_sat


class TestEndpointHelpers:
    def test_get_hil_gnd_from_target(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink")
        assert manager._get_hil_gnd(link).name == "Gnd0"

    def test_get_hil_gnd_from_source(self, manager):
        link = _make_link("Gnd0", "Sat0", "GroundStationLink")
        assert manager._get_hil_gnd(link).name == "Gnd0"

    def test_get_sat_from_source(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink")
        assert manager._get_sat(link).name == "Sat0"

    def test_get_sat_from_target(self, manager):
        link = _make_link("Gnd0", "Sat0", "GroundStationLink")
        assert manager._get_sat(link).name == "Sat0"


# setup_link


class TestSetupLink:
    def test_setup_link_calls_create_veth_and_bridge(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=1234)

        with patch.object(manager, "_create_veth", return_value=True) as mock_veth, \
             patch.object(manager, "_create_bridge", return_value=True) as mock_bridge, \
             patch.object(manager, "_apply_qos") as mock_qos:
            manager.setup_link(link)

        mock_veth.assert_called_once_with("Sat0", 1234, 0)
        mock_bridge.assert_called_once_with("Gnd0", 0)
        mock_qos.assert_called_once()

    def test_setup_link_skips_when_no_pid(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=0)

        with patch.object(manager, "_create_veth") as mock_veth:
            manager.setup_link(link)
            mock_veth.assert_not_called()

    def test_setup_link_skips_bridge_when_veth_fails(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=1234)

        with patch.object(manager, "_create_veth", return_value=False), \
             patch.object(manager, "_create_bridge") as mock_bridge:
            manager.setup_link(link)
            mock_bridge.assert_not_called()


# teardown_link


class TestTeardownLink:
    def test_teardown_calls_bridge_then_veth_delete(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=5678)

        with patch.object(manager, "_teardown_bridge") as mock_br, \
             patch.object(manager, "_delete_sat_veth") as mock_veth:
            manager.teardown_link(link)

        mock_br.assert_called_once_with("Gnd0")
        mock_veth.assert_called_once_with("Sat0", 5678, 0)

    def test_teardown_skips_veth_delete_when_no_pid(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=0)

        with patch.object(manager, "_teardown_bridge"), \
             patch.object(manager, "_delete_sat_veth") as mock_veth:
            manager.teardown_link(link)
            mock_veth.assert_not_called()

    def test_teardown_always_tears_down_bridge_even_without_pid(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=0)

        with patch.object(manager, "_teardown_bridge") as mock_br, \
             patch.object(manager, "_delete_sat_veth"):
            manager.teardown_link(link)
            mock_br.assert_called_once_with("Gnd0")


# update_link


class TestUpdateLink:
    def test_update_calls_tc_on_sat_side_only(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", delay=25, src_pid=9999)

        with patch("satgonetem.launchers.hil_manager._run_tc_batch") as mock_tc:
            manager.update_link(link)

        mock_tc.assert_called_once()
        pid_arg, cmds_arg = mock_tc.call_args[0]
        assert pid_arg == 9999
        assert any("eth0" in cmd for cmd in cmds_arg)
        assert any("25ms" in cmd for cmd in cmds_arg)

    def test_update_skips_when_no_pid(self, manager):
        link = _make_link("Sat0", "Gnd0", "GroundStationLink", src_pid=0)

        with patch("satgonetem.launchers.hil_manager._run_tc_batch") as mock_tc:
            manager.update_link(link)
            mock_tc.assert_not_called()


# wire_links


class TestWireLinks:
    def test_wire_links_calls_setup_for_each(self, manager):
        link_a = _make_link("Sat0", "Gnd0", "GroundStationLink")
        link_b = _make_link("Sat1", "Gnd0", "GroundStationLink")

        with patch.object(manager, "setup_link") as mock_setup:
            manager.wire_links([link_a, link_b])

        assert mock_setup.call_count == 2
        mock_setup.assert_any_call(link_a)
        mock_setup.assert_any_call(link_b)

    def test_wire_links_empty_list(self, manager):
        with patch.object(manager, "setup_link") as mock_setup:
            manager.wire_links([])
            mock_setup.assert_not_called()


# teardown_all


class TestTeardownAll:
    def test_tears_down_all_tracked_bridges(self, manager):
        manager._hil_bridges = {"Gnd0": "brhil0", "Gnd2": "brhil2"}
        torn_down = []

        def fake_teardown(gnd_name):
            torn_down.append(gnd_name)
            manager._hil_bridges.pop(gnd_name, None)

        with patch.object(manager, "_teardown_bridge", side_effect=fake_teardown):
            manager.teardown_all()

        assert set(torn_down) == {"Gnd0", "Gnd2"}

    def test_teardown_all_noop_when_no_bridges(self, manager):
        with patch.object(manager, "_teardown_bridge") as mock_br:
            manager.teardown_all()
            mock_br.assert_not_called()


# _teardown_bridge


class TestTeardownBridge:
    def test_deletes_bridge_and_clears_tracking(self, manager):
        manager._hil_bridges["Gnd0"] = "brhil0"
        manager._hil_veths["Gnd0"] = "hil0"

        mock_ipr = MagicMock()
        mock_ipr.__enter__ = MagicMock(return_value=mock_ipr)
        mock_ipr.__exit__ = MagicMock(return_value=False)
        mock_ipr.link.return_value = [{"index": 10}]

        with patch("satgonetem.launchers.hil_manager.IPRoute", return_value=mock_ipr):
            manager._teardown_bridge("Gnd0")

        assert "Gnd0" not in manager._hil_bridges
        assert "Gnd0" not in manager._hil_veths
        mock_ipr.link.assert_any_call("del", index=10)

    def test_noop_when_no_bridge_tracked(self, manager):
        with patch("satgonetem.launchers.hil_manager.IPRoute") as mock_cls:
            manager._teardown_bridge("Gnd0")
            mock_cls.assert_not_called()


# link_capacity_kbps fallback


class TestLinkCapacity:
    def test_uses_peer1_capacity_when_set(self, manager):
        link = MagicMock()
        link.peer1_capacity = 30_000
        assert manager._link_capacity_kbps(link) == 30_000

    def test_falls_back_to_gnd_capacity_when_peer1_zero(self, manager):
        link = MagicMock()
        link.peer1_capacity = 0
        assert manager._link_capacity_kbps(link) == manager._gnd_capacity_kbps
