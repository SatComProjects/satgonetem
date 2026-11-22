"""Direct contract tests for SR-MPLS route identity and teardown helpers."""

from __future__ import annotations

import pytest

from satgonetem.routing.srmpls_route_ops import (
    build_batch,
    cached_route_keys,
    delete_command,
    route_changes,
    route_key,
    tolerant_delete_command,
)

pytestmark = pytest.mark.unit


class TestRouteKey:
    def test_mpls_route_is_identified_by_its_label(self) -> None:
        assert route_key("ip -f mpls route replace 16001 dev lo") == ("mpls", "16001")

    def test_mpls_identity_ignores_next_hop_and_attributes(self) -> None:
        plain = "ip -f mpls route replace 16001 dev lo"
        forwarded = "ip -f mpls route replace 16001 via inet 10.0.0.2 dev Sat1.1"

        assert route_key(plain) == route_key(forwarded) == ("mpls", "16001")

    def test_ipv4_route_is_identified_by_its_prefix(self) -> None:
        command = (
            "ip route replace 10.0.0.9/32 encap mpls 16003/16007 via 10.1.0.2 dev GS0.1"
        )

        assert route_key(command) == ("ip", "10.0.0.9/32")

    @pytest.mark.parametrize(
        "command",
        [
            "ip -f mpls route del 16001",
            "ip route del 10.0.0.9/32",
            "ip route replace",
            "ip -f mpls route replace",
            "sysctl -w net.mpls.platform_labels=1048575",
        ],
    )
    def test_unsupported_commands_raise(self, command: str) -> None:
        with pytest.raises(ValueError, match="Unsupported route command"):
            route_key(command)


class TestDeleteCommand:
    def test_mpls_delete_drops_next_hop_attributes(self) -> None:
        command = "ip -f mpls route replace 16001 via inet 10.0.0.2 dev Sat1.1"

        assert delete_command(command) == "ip -f mpls route del 16001"

    def test_ipv4_delete_drops_encap_and_next_hop(self) -> None:
        command = (
            "ip route replace 10.0.0.9/32 encap mpls 16003/16007 via 10.1.0.2 dev GS0.1"
        )

        assert delete_command(command) == "ip route del 10.0.0.9/32"


class TestTolerantDeleteCommand:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (
                "ip -f mpls route replace 16001 via inet 10.0.0.2 dev Sat1.1",
                "if ip -f mpls route show exact 16001 2>&1 | grep -q .; "
                "then ip -f mpls route del 16001; fi",
            ),
            (
                "ip route replace 10.0.0.9/32 via 10.1.0.2 dev GS0.1",
                "if ip route show exact 10.0.0.9/32 2>&1 | grep -q .; "
                "then ip route del 10.0.0.9/32; fi",
            ),
        ],
    )
    def test_delete_runs_only_when_the_route_exists(
        self, command: str, expected: str
    ) -> None:
        assert tolerant_delete_command(command) == expected

    def test_guard_does_not_suppress_delete_failures(self) -> None:
        guard = tolerant_delete_command("ip -f mpls route replace 16001 dev lo")

        assert guard.startswith("if ip -f mpls route show exact 16001 ")
        assert guard.endswith("then ip -f mpls route del 16001; fi")
        assert "|| true" not in guard


class TestCachedRouteKeys:
    def test_returns_the_identity_of_every_cached_command(self) -> None:
        commands = [
            "ip -f mpls route replace 16001 dev lo",
            "ip route replace 10.0.0.9/32 encap mpls 16001 via 10.0.0.2 dev Sat1.1",
        ]

        assert cached_route_keys(commands) == {("mpls", "16001"), ("ip", "10.0.0.9/32")}

    def test_empty_cache_has_no_keys(self) -> None:
        assert cached_route_keys([]) == set()


class TestRouteChanges:
    def test_removed_route_is_deleted(self) -> None:
        stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev Sat1.9"

        deletes, installs = route_changes([stale], [], set())

        assert deletes == [stale]
        assert installs == []

    def test_changed_next_hop_is_a_replace_not_a_delete(self) -> None:
        previous = "ip route replace 10.0.0.9/32 via 10.0.0.2 dev Sat1.1"
        desired = "ip route replace 10.0.0.9/32 via 10.0.0.3 dev Sat1.2"

        deletes, installs = route_changes([previous], [desired], set())

        assert deletes == []
        assert installs == [desired]

    def test_unchanged_route_is_not_reinstalled(self) -> None:
        command = "ip -f mpls route replace 16001 dev lo"

        assert route_changes([command], [command], set()) == ([], [])

    def test_foreign_route_is_never_deleted_here(self) -> None:
        foreign = "ip route replace 10.0.0.9/32 via 10.0.0.2 dev Sat1.1"
        stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev Sat1.9"

        deletes, installs = route_changes([foreign, stale], [], {route_key(foreign)})

        assert deletes == [stale]
        assert installs == []


class TestBuildBatch:
    def test_installs_only_batch(self) -> None:
        install = "ip route replace 10.0.0.9/32 via 10.0.0.2 dev Sat1.1"

        assert build_batch([], [install]) == f"set -e; {install}"

    def test_tolerant_deletes_precede_installs(self) -> None:
        stale = "ip -f mpls route replace 16009 via inet 10.0.0.9 dev Sat1.9"
        install = "ip -f mpls route replace 16001 dev lo"

        assert build_batch([stale], [install]) == (
            "set -e; if ip -f mpls route show exact 16009 2>&1 | grep -q .; "
            "then ip -f mpls route del 16009; fi; "
            "ip -f mpls route replace 16001 dev lo"
        )

    def test_deletes_only_batch_still_checks_for_absence(self) -> None:
        stale = "ip route replace 10.0.0.9/32 via 10.0.0.2 dev Sat1.1"

        assert build_batch([stale], []) == (
            "set -e; if ip route show exact 10.0.0.9/32 2>&1 | grep -q .; "
            "then ip route del 10.0.0.9/32; fi"
        )

    def test_empty_batch_has_no_command(self) -> None:
        assert build_batch([], []) == ""
