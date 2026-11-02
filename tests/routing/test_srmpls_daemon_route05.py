"""Regression tests for ROUTE-05: new interfaces must get MPLS input enabled.

``_apply_sr_mpls_routes(init_flag=False)`` runs on every topology update.
It must enable MPLS input on all current interfaces each time, otherwise
interfaces added after init keep the kernel default
``net.mpls.conf.<iface>.input=0`` and drop MPLS traffic.
"""

from unittest.mock import MagicMock, call

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


def test_apply_sr_mpls_routes_enables_mpls_when_not_init() -> None:
    """Non-init updates must still enable MPLS on all nodes (idempotent)."""
    daemon = SRMPLSDaemon(MagicMock())
    sentinel_label_manager = object()
    daemon._label_manager = sentinel_label_manager

    daemon._enable_mpls_on_all_nodes = MagicMock()
    daemon._apply_sr_node_sids_to_satellites = MagicMock()
    daemon._apply_sr_routes_to_ground_stations = MagicMock()
    daemon._apply_sr_routes_to_satellites = MagicMock()

    daemon._apply_sr_mpls_routes(init_flag=False, max_workers=3)

    daemon._enable_mpls_on_all_nodes.assert_called_once_with(max_workers=3)
    daemon._apply_sr_node_sids_to_satellites.assert_called_once_with()
    daemon._apply_sr_routes_to_ground_stations.assert_called_once_with()
    daemon._apply_sr_routes_to_satellites.assert_called_once_with()


def test_enable_mpls_sets_input_on_every_current_interface() -> None:
    """Every interface present at update time gets MPLS input enabled."""
    node = Node("Sat0")
    node.interfaces = [
        Interface(name="Sat0.1"),
        Interface(name="Sat0.2"),
        Interface(name="Sat0.12"),
    ]
    node.execute_command = MagicMock()

    daemon = SRMPLSDaemon(MagicMock())
    daemon._enable_mpls(node)

    command = node.execute_command.call_args[0][0]
    for iname in ("eth1", "eth2", "eth12"):
        assert f"sysctl -w net.mpls.conf.{iname}.input=1" in command
    assert "sysctl -w net.mpls.conf.lo.input=1" in command
    assert "net.mpls.platform_labels=1048575" in command
    assert node.execute_command.call_args == call(command)
