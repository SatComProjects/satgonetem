"""Regression tests for ROUTE-06: SR-MPLS teardown must flush all routes.

``SRMPLSDaemon.remove()`` previously ran the invalid
``ip -f mpls route flush 2>/dev/null; true`` command and never deleted the
IPv4/encap prefixes installed by the daemon. These tests pin the corrected
flush command and the per-node prefix cleanup.
"""

import logging
from unittest.mock import MagicMock

from satgonetem.routing.srmpls_daemon import SRMPLSDaemon


def make_daemon():
    """Build a daemon without running __init__, with empty route caches."""
    daemon = SRMPLSDaemon.__new__(SRMPLSDaemon)
    daemon._prev_sr_gs_routes = {}
    daemon._prev_sr_sat_routes = {}
    daemon._prev_sr_node_sid_routes = {}
    return daemon


def make_node(node_id=7, name="GS1"):
    """Build a mock node whose container is present and commands are recorded."""
    node = MagicMock()
    node.id = node_id
    node.name = name
    node.container = object()
    node.execute_command = MagicMock(return_value="")
    return node


def recorded_commands(node):
    """Return the shell command text of each execute_command call."""
    return [call.args[0][-1] for call in node.execute_command.call_args_list]


class TestFlushNodeRoute06:
    def test_flush_uses_valid_mpls_flush_command(self):
        daemon = make_daemon()
        node = make_node()

        daemon._flush_node(node)

        assert recorded_commands(node) == ["ip -f mpls route flush table all"]
        flush = node.execute_command.call_args.args[0]
        assert flush == ["sh", "-lc", "ip -f mpls route flush table all"]
        assert "; true" not in flush[-1]
        assert "2>/dev/null" not in flush[-1]

    def test_deletes_cached_ipv4_encap_prefix_before_flushing(self):
        daemon = make_daemon()
        node = make_node()
        daemon._prev_sr_gs_routes[node.id] = [
            "ip -f mpls route replace 16007 dev lo",
            "ip route replace 10.0.0.9/32 encap mpls 16003/16007 "
            "via 10.1.0.2 dev GS0.1",
        ]
        # The same prefix may also appear in another cache; delete it once.
        daemon._prev_sr_node_sid_routes[node.id] = [
            "ip route replace 10.0.0.9/32 via 10.1.0.2 dev GS0.1",
        ]

        daemon._flush_node(node)

        assert recorded_commands(node) == [
            "ip route del 10.0.0.9/32",
            "ip -f mpls route flush table all",
        ]
        assert daemon._prev_sr_gs_routes == {}
        assert daemon._prev_sr_node_sid_routes == {}

    def test_deletes_plain_ipv4_route_from_node_sid_cache(self):
        daemon = make_daemon()
        node = make_node(node_id=3, name="Sat3")
        daemon._prev_sr_node_sid_routes[node.id] = [
            "ip -f mpls route replace 16003 dev lo",
            "ip route replace 10.0.0.10/32 via 10.1.0.3 dev Sat2.1",
        ]

        daemon._flush_node(node)

        assert recorded_commands(node) == [
            "ip route del 10.0.0.10/32",
            "ip -f mpls route flush table all",
        ]

    def test_failing_flush_is_logged_not_silently_ignored(self, caplog):
        daemon = make_daemon()
        node = make_node()
        node.execute_command.side_effect = RuntimeError("flush failed")

        with caplog.at_level(logging.ERROR):
            daemon._flush_node(node)

        assert node.execute_command.called
        assert "Failed to flush MPLS routes on GS1" in caplog.text
        assert "flush failed" in caplog.text

    def test_node_without_container_is_skipped(self):
        daemon = make_daemon()
        node = make_node()
        node.container = None

        daemon._flush_node(node)

        node.execute_command.assert_not_called()
