"""Live link QoS and control-plane checks against the GoNetEm backend."""

from __future__ import annotations

from typing import Any

import pytest

from satgonetem.models.link import Link
from satgonetem.services.topology_satcom import TopologyManager

pytestmark = pytest.mark.integration


def _endpoint_interfaces(link: Link) -> list[tuple[Any, Any]]:
    """Return ``(node, interface)`` pairs in source/target order."""
    source_name, target_name = link.get_names()
    by_name = {interface.name: interface for interface in link.peer_interfaces}
    return [
        (link.source, by_name[source_name]),
        (link.target, by_name[target_name]),
    ]


def test_live_qdiscs_show_netem_and_tbf(live_topology: TopologyManager) -> None:
    """Every link endpoint has TBF rate shaping and a netem delay/limit."""
    topology = live_topology
    netem_limit = getattr(topology, "link_netem_limit", 1000)
    assert topology.get_links()

    for link in topology.get_links():
        assert link.is_active, f"{link.get_names()} is not active"
        expected_delay = max(int(link.delay), 1)
        expected_rate = f"rate {link.get_capacity() // 1000}Mbit"
        for node, interface in _endpoint_interfaces(link):
            iname = interface.get_iname()
            qdiscs = node.execute_command(f"tc qdisc show dev {iname}")
            assert "qdisc tbf" in qdiscs, (
                f"{node.name}/{iname}: no TBF root qdisc:\n{qdiscs}"
            )
            assert "qdisc netem" in qdiscs, (
                f"{node.name}/{iname}: no netem qdisc:\n{qdiscs}"
            )
            assert expected_rate in qdiscs, (
                f"{node.name}/{iname}: expected {expected_rate!r}:\n{qdiscs}"
            )
            assert f"delay {expected_delay}ms" in qdiscs, (
                f"{node.name}/{iname}: expected delay {expected_delay}ms:\n{qdiscs}"
            )
            assert f"limit {netem_limit}" in qdiscs, (
                f"{node.name}/{iname}: expected netem limit {netem_limit}:\n{qdiscs}"
            )


def test_impairment_and_admin_state_reach_the_backend(
    live_topology: TopologyManager,
) -> None:
    """Loss/jitter and link down/up are visible in both endpoint namespaces."""
    topology = live_topology
    link = next(
        link for link in topology.get_links() if link.type == "GroundStationLink"
    )
    endpoints = _endpoint_interfaces(link)

    try:
        topology.set_link_impairment(link, loss_percent=7.5, jitter_ms=11)
        assert link.loss == 7.5
        assert link.jitter == 11
        assert link.to_update is False, "backend did not acknowledge the update"

        for node, interface in endpoints:
            iname = interface.get_iname()
            qdiscs = node.execute_command(f"tc qdisc show dev {iname}")
            assert "loss 7.5%" in qdiscs, (
                f"{node.name}/{iname}: loss did not reach the backend:\n{qdiscs}"
            )
            assert "11ms" in qdiscs, (
                f"{node.name}/{iname}: jitter did not reach the backend:\n{qdiscs}"
            )

        topology.set_link_state(link, False)
        assert link.admin_up is False
        for node, interface in endpoints:
            iname = interface.get_iname()
            state = node.execute_command(f"ip link show {iname}")
            assert "state DOWN" in state, (
                f"{node.name}/{iname}: interface is not down:\n{state}"
            )

        topology.set_link_state(link, True)
        assert link.admin_up is True
        for node, interface in endpoints:
            iname = interface.get_iname()
            state = node.execute_command(f"ip link show {iname}")
            assert "state UP" in state, (
                f"{node.name}/{iname}: interface is not up:\n{state}"
            )
    finally:
        try:
            topology.set_link_state(link, True)
        finally:
            topology.set_link_impairment(link, loss_percent=0.0, jitter_ms=0)
