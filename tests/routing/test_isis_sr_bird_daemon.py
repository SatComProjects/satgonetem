"""Tests for the IS-IS Bird config helper."""

from unittest.mock import MagicMock

from satgonetem.models.node import Node
from satgonetem.routing.isis_sr_bird_daemon import (
    ISISBirdSRDaemon,
    _apply_config,
    _build_isis_sr_config,
)


def make_node(name: str = "Sat0", loopback_ip: str = "10.0.0.1") -> Node:
    node = Node(name)
    node.loopback.ipv4 = loopback_ip
    node.execute_command = MagicMock()
    return node


def test_isis_config_has_no_hostname():
    # Bird 2.0.7 (the satellite image) rejects the directive.
    config = _build_isis_sr_config(make_node())
    assert "hostname" not in config


def test_apply_config_uses_direct_bird():
    node = make_node()
    _apply_config(node, "config content")
    cmd = node.execute_command.call_args[0][0]

    assert "start-service.py" not in cmd
    assert "/etc/bird/bird.conf" in cmd
    assert "bird -c /etc/bird/bird.conf -s /run/bird/bird.ctl" in cmd


def test_stop_node_uses_birdc_down():
    node = make_node()
    daemon = ISISBirdSRDaemon(MagicMock())
    daemon._stop_node(node)

    node.execute_command.assert_called_once_with(
        "birdc -s /run/bird/bird.ctl down 2>/dev/null || true"
    )
