"""Direct contract tests for the SR-MPLS teardown collaborator."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from satgonetem.routing.srmpls_teardown import SRMPLSTeardown, cached_prefixes

pytestmark = pytest.mark.unit


def _node(node_id: int = 7, name: str = "GS1") -> MagicMock:
    node = MagicMock()
    node.id = node_id
    node.name = name
    node.container = object()
    return node


class TestCachedPrefixes:
    def test_only_ip_route_replace_lines_carry_a_prefix(self) -> None:
        commands = [
            "ip -f mpls route replace 16007 dev lo",
            "ip route replace 10.0.0.9/32 encap mpls 16003/16007 via 10.1.0.2 dev GS0.1",
            "ip route replace 10.0.0.10/32 via 10.1.0.3 dev GS0.2",
        ]

        assert cached_prefixes(commands) == ["10.0.0.9/32", "10.0.0.10/32"]

    def test_prefix_starting_line_must_match_exactly(self) -> None:
        commands = [
            "ip route replaceX 10.0.0.9/32",
            " ip route replace 10.0.0.10/32 via 10.1.0.3 dev GS0.2",
        ]

        assert cached_prefixes(commands) == ["10.0.0.10/32"]

    def test_multiline_command_is_split_and_deduplicated(self) -> None:
        commands = [
            "ip route replace 10.0.0.9/32 via 10.1.0.2 dev GS0.1\n"
            "ip route replace 10.0.0.10/32 via 10.1.0.3 dev GS0.2",
            "ip route replace 10.0.0.9/32 via 10.9.0.9 dev GS9.1",
        ]

        assert cached_prefixes(commands) == ["10.0.0.9/32", "10.0.0.10/32"]

    def test_short_lines_are_ignored(self) -> None:
        assert cached_prefixes(["ip route replace"]) == []


class TestFlushNode:
    def test_pops_cached_commands_before_deleting(self) -> None:
        caches = [{}]
        caches[0][7] = [
            "ip route replace 10.0.0.9/32 encap mpls 16003 via 10.1.0.2 dev GS0.1"
        ]
        node = _node()

        SRMPLSTeardown(caches).flush_node(node)

        assert caches[0] == {}, "the cache must be dropped even if deletion fails"
        assert node.execute_command.call_args_list[0].args[0] == [
            "sh",
            "-lc",
            "ip route del 10.0.0.9/32",
        ]
        assert node.execute_command.call_args_list[-1].args[0] == [
            "sh",
            "-lc",
            "ip -f mpls route flush table all",
        ]

    def test_containerless_node_is_skipped(self) -> None:
        caches = [{}]
        node = _node()
        node.container = None

        SRMPLSTeardown(caches).flush_node(node)

        node.execute_command.assert_not_called()

    def test_delete_failure_is_logged_and_flush_still_runs(self, caplog) -> None:
        node = _node()
        node.execute_command.side_effect = [RuntimeError("delete failed"), ""]
        caches = [{7: ["ip route replace 10.0.0.9/32 via 10.0.0.2 dev GS1.1"]}]

        with caplog.at_level(logging.ERROR):
            SRMPLSTeardown(caches).flush_node(node)

        assert node.execute_command.call_count == 2
        assert "Failed to delete SR route 10.0.0.9/32 on GS1" in caplog.text
        assert "delete failed" in caplog.text
        assert node.execute_command.call_args_list[-1].args[0] == [
            "sh",
            "-lc",
            "ip -f mpls route flush table all",
        ]
