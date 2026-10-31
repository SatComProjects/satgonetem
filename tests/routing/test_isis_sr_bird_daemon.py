"""Tests for the IS-IS Bird config helper."""

from unittest.mock import MagicMock

from satgonetem.models.node import Node
from satgonetem.routing.isis_sr_bird_daemon import _build_isis_sr_config


def make_node(name: str = "Sat0", loopback_ip: str = "10.0.0.1") -> Node:
    node = Node(name)
    node.loopback.ipv4 = loopback_ip
    node.execute_command = MagicMock()
    return node


def test_isis_config_has_no_hostname():
    # Bird 2.0.7 (the satellite image) rejects the directive.
    config = _build_isis_sr_config(make_node())
    assert "hostname" not in config
