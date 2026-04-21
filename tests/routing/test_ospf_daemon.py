"""Tests for OSPFDaemon and its Bird config helpers."""

import pytest
from unittest.mock import MagicMock, call, patch

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.routing.ospf_daemon import (
    OSPF_AREA,
    OSPF_DEAD_COUNT,
    OSPF_HELLO_INTERVAL,
    OSPFDaemon,
    _apply_config,
    _assign_ips,
    _build_ospf_config,
)


def make_node(name: str, loopback_ip: str = "10.0.0.1") -> Node:
    """Build a real Node with a mocked execute_command."""
    node = Node(name)
    node.loopback.ipv4 = loopback_ip
    node.execute_command = MagicMock()
    return node


def make_iface(iname: str, peer_name: str, ip: str = "10.1.0.0") -> Interface:
    """Build a minimal Interface with a peer stub."""
    iface = Interface(name=iname)
    iface.ipv4 = ip
    iface.peer = MagicMock()
    iface.peer.name = peer_name
    return iface


def make_topology(status: bool = True, satellites=None, ground_stations=None):
    """Build a minimal topology mock."""
    topo = MagicMock()
    topo.status = status
    topo.get_satellites.return_value = satellites or []
    topo.get_ground_stations.return_value = ground_stations or []
    return topo


@pytest.fixture
def topology():
    return make_topology()


@pytest.fixture
def daemon(topology):
    return OSPFDaemon(topology)


class TestOSPFDaemonInit:
    """Tests for OSPFDaemon.init()."""

    def test_returns_true_when_all_nodes_succeed(self, daemon, topology):
        node = make_node("Sat0")
        topology.get_satellites.return_value = [node]

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            result = daemon.init()

        assert result is True

    def test_returns_false_when_any_node_fails(self, daemon, topology):
        node = make_node("Sat0")
        node.execute_command.side_effect = RuntimeError("container error")
        topology.get_satellites.return_value = [node]

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            result = daemon.init()

        assert result is False

    def test_calls_execute_command_on_each_node(self, daemon, topology):
        sat = make_node("Sat0")
        gs = make_node("Gnd0", loopback_ip="10.0.1.1")
        topology.get_satellites.return_value = [sat]
        topology.get_ground_stations.return_value = [gs]

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            daemon.init()

        assert sat.execute_command.called
        assert gs.execute_command.called

    def test_passes_max_workers(self, daemon, topology):
        topology.get_satellites.return_value = []
        topology.get_ground_stations.return_value = []

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            result = daemon.init(max_workers=8)

        assert result is True

    def test_empty_topology_returns_true(self, daemon, topology):
        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            result = daemon.init()

        assert result is True


class TestOSPFDaemonUpdate:
    """Tests for OSPFDaemon.update()."""

    def test_skips_when_topology_inactive(self, daemon, topology, caplog):
        import logging

        topology.status = False
        link = MagicMock()

        with caplog.at_level(logging.WARNING):
            daemon.update(new_links=[link])

        assert "not yet active" in caplog.text

    def test_reinits_source_and_target_of_each_link(self, daemon, topology):
        sat = make_node("Sat0")
        gs = make_node("Gnd0", loopback_ip="10.0.1.1")
        link = MagicMock()
        link.source = sat
        link.target = gs
        topology.status = True

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            daemon.update(new_links=[link])

        assert sat.execute_command.called
        assert gs.execute_command.called

    def test_deduplicates_affected_nodes(self, daemon, topology):
        sat = make_node("Sat0")
        gs = make_node("Gnd0", loopback_ip="10.0.1.1")
        link1 = MagicMock()
        link1.source = sat
        link1.target = gs
        link2 = MagicMock()
        link2.source = sat
        link2.target = gs
        topology.status = True

        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            daemon.update(new_links=[link1, link2])

        # sat appears in both links but should only be init'd once
        assert sat.execute_command.call_count == gs.execute_command.call_count

    def test_empty_links_does_nothing(self, daemon, topology):
        topology.status = True
        daemon.update(new_links=[])


class TestOSPFDaemonRemove:
    """Tests for OSPFDaemon.remove()."""

    def test_stops_bird_on_single_node(self, daemon):
        node = make_node("Sat0")
        daemon.remove(node=node)
        node.execute_command.assert_called_once_with("service bird stop")

    def test_stops_bird_on_all_nodes_when_no_node_given(self, daemon, topology):
        sat = make_node("Sat0")
        gs = make_node("Gnd0", loopback_ip="10.0.1.1")
        topology.get_satellites.return_value = [sat]
        topology.get_ground_stations.return_value = [gs]

        daemon.remove()

        sat.execute_command.assert_called_once_with("service bird stop")
        gs.execute_command.assert_called_once_with("service bird stop")

    def test_logs_error_and_continues_on_failure(self, daemon, topology, caplog):
        import logging

        failing = make_node("Sat0")
        failing.execute_command.side_effect = RuntimeError("boom")
        ok = make_node("Sat1")
        topology.get_satellites.return_value = [failing, ok]
        topology.get_ground_stations.return_value = []

        with caplog.at_level(logging.ERROR):
            daemon.remove()

        assert "boom" in caplog.text
        ok.execute_command.assert_called_once_with("service bird stop")


class TestBuildOspfConfig:
    """Tests for the _build_ospf_config module-level helper."""

    def test_contains_router_id(self):
        node = make_node("Sat3", loopback_ip="10.0.0.3")
        config = _build_ospf_config(node)
        assert "router id 10.0.0.3;" in config

    def test_contains_hostname(self):
        node = make_node("Sat3", loopback_ip="10.0.0.3")
        config = _build_ospf_config(node)
        assert 'hostname "Sat3";' in config

    def test_loopback_is_stub(self):
        node = make_node("Sat0")
        config = _build_ospf_config(node)
        assert 'interface "lo"' in config
        assert "stub yes;" in config

    def test_includes_each_interface(self):
        node = make_node("Sat0")
        node.interfaces = [make_iface("Sat0.1", "Gnd0")]
        config = _build_ospf_config(node)
        assert 'interface "eth1"' in config
        assert "# link to Gnd0" in config

    def test_includes_ospf_timers(self):
        node = make_node("Sat0")
        node.interfaces = [make_iface("Sat0.1", "Sat1")]
        config = _build_ospf_config(node)
        assert f"hello {OSPF_HELLO_INTERVAL};" in config
        assert f"dead count {OSPF_DEAD_COUNT};" in config

    def test_ospf_area_value(self):
        node = make_node("Sat0")
        config = _build_ospf_config(node)
        assert f"area {OSPF_AREA}" in config

    def test_no_interfaces_still_valid(self):
        node = make_node("Sat0")
        config = _build_ospf_config(node)
        assert "protocol ospf v2 ospf1" in config

    def test_multiple_interfaces_all_present(self):
        node = make_node("Sat0")
        node.interfaces = [
            make_iface("Sat0.1", "Sat1"),
            make_iface("Sat0.2", "Sat2"),
        ]
        config = _build_ospf_config(node)
        assert 'interface "eth1"' in config
        assert 'interface "eth2"' in config


class TestAssignIps:
    """Tests for the _assign_ips module-level helper."""

    def test_skips_when_no_interfaces_and_no_loopback(self):
        node = make_node("Sat0")
        node.loopback.ipv4 = None
        _assign_ips(node)
        node.execute_command.assert_not_called()

    def test_assigns_loopback(self):
        node = make_node("Sat0", loopback_ip="10.0.0.1")
        _assign_ips(node)
        cmd = node.execute_command.call_args[0][0]
        assert "addr replace 10.0.0.1/32 dev lo" in _decode_b64_cmd(cmd)

    def test_assigns_interface_ip(self):
        node = make_node("Sat0")
        node.interfaces = [make_iface("Sat0.1", "Gnd0", ip="10.1.0.0")]
        _assign_ips(node)
        cmd = node.execute_command.call_args[0][0]
        assert "addr replace 10.1.0.0/31 dev eth1" in _decode_b64_cmd(cmd)

    def test_skips_interface_without_ip(self):
        node = make_node("Sat0")
        iface = Interface(name="Sat0.1")
        iface.ipv4 = None
        iface.peer = MagicMock()
        node.interfaces = [iface]
        node.loopback.ipv4 = None
        _assign_ips(node)
        node.execute_command.assert_not_called()


class TestApplyConfig:
    """Tests for the _apply_config module-level helper."""

    def test_writes_config_to_tmpfile(self):
        node = make_node("Sat0")
        m = MagicMock()
        with patch("satgonetem.routing.ospf_daemon.open", m, create=True):
            _apply_config(node, "config content")
        m.assert_called_once()
        handle = m.return_value.__enter__.return_value
        handle.write.assert_called_once_with("config content")

    def test_calls_start_service(self):
        node = make_node("Sat0")
        with patch("satgonetem.routing.ospf_daemon.open", create=True):
            _apply_config(node, "config content")
        cmd = node.execute_command.call_args[0][0]
        assert "/usr/bin/start-service.py --service bird --bird-config" in cmd


def _decode_b64_cmd(cmd: str) -> str:
    """Extract and decode the base64 payload from an ip-batch shell command."""
    import base64
    import re

    match = re.search(r"echo (\S+) \| base64 -d", cmd)
    assert match, f"No base64 payload found in: {cmd}"
    return base64.b64decode(match.group(1)).decode()
