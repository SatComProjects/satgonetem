#!/usr/bin/env python3
"""Verify the IPv4 address plan at a 900-satellite scale.

This is a standalone scale check, not part of the default pytest suite.  It
builds a synthetic topology with 900 satellites, 5 ground stations, and 20
user terminals, allocates ring and cross ISLs plus ground/terminal links, and
checks that every address is unique and belongs to the expected node block.

Run with:
    .venv/bin/python scripts/verify_900_satellites.py
"""

from __future__ import annotations

from collections import Counter

from satgonetem.addressing import Ipv4AddressPlan, NodeKind


SATELLITES = 900
GROUND_STATIONS = 5
USER_TERMINALS = 20
RING_LINKS_PER_SATELLITE = 2
CROSS_LINK_OFFSET = 37
GROUND_LINKS_PER_GS = 20
TERMINAL_LINKS_PER_UT = 5


def endpoint_key(address) -> tuple[int, int]:
    """Return the canonical link-ownership key."""
    return (address.kind.priority, address.node_id)


def main() -> int:
    plan = Ipv4AddressPlan()

    nodes = {}
    for sat_id in range(SATELLITES):
        nodes[sat_id] = plan.register_node(sat_id, NodeKind.SATELLITE)
    for gs_idx in range(GROUND_STATIONS):
        node_id = 9000 + gs_idx
        nodes[node_id] = plan.register_node(node_id, NodeKind.GROUND_STATION)
    for ut_idx in range(USER_TERMINALS):
        node_id = 20000 + ut_idx
        nodes[node_id] = plan.register_node(node_id, NodeKind.USER_TERMINAL)

    # Every node has a unique block and a loopback at block base + 1.
    blocks = [address.block for address in nodes.values()]
    assert len(set(blocks)) == len(blocks), "duplicate node blocks"
    for address in nodes.values():
        assert address.loopback.network.prefixlen == 32
        assert address.loopback.ip == address.block.network_address + 1
        assert address.loopback.ip in address.block

    allocated_networks = set()
    allocated_ips = {str(address.loopback.ip) for address in nodes.values()}
    link_count = 0

    def allocate(label: str, source_id: int, target_id: int) -> None:
        nonlocal link_count
        link = plan.allocate_link(label, source_id, target_id)
        source_address = nodes[source_id]
        target_address = nodes[target_id]
        owner = min((source_address, target_address), key=endpoint_key)

        assert link.prefix_len == 31
        assert link.source.network == link.target.network
        assert link.source.ip != link.target.ip
        assert link.source.network not in allocated_networks
        assert link.source.ip in owner.block
        assert link.target.ip in owner.block
        assert link.source.ip not in allocated_ips
        assert link.target.ip not in allocated_ips

        allocated_networks.add(link.source.network)
        allocated_ips.add(str(link.source.ip))
        allocated_ips.add(str(link.target.ip))
        link_count += 1

    # Ring plus offset cross-links; each satellite has four ISLs.
    for sat_id in range(SATELLITES):
        for offset in range(1, RING_LINKS_PER_SATELLITE + 1):
            peer = (sat_id + offset) % SATELLITES
            if sat_id == peer:
                continue
            pair = tuple(sorted((sat_id, peer)))
            allocate(f"isl-ring-{pair}", sat_id, peer)

        peer = (sat_id + CROSS_LINK_OFFSET) % SATELLITES
        pair = tuple(sorted((sat_id, peer)))
        allocate(f"isl-cross-{pair}", sat_id, peer)

    # Ground-station links: each GS owns a /20, so all 100 links fit easily.
    for gs_idx in range(GROUND_STATIONS):
        gs_id = 9000 + gs_idx
        for link_idx in range(GROUND_LINKS_PER_GS):
            sat_id = (gs_idx * GROUND_LINKS_PER_GS + link_idx) % SATELLITES
            allocate(f"gsl-{gs_id}-{sat_id}", gs_id, sat_id)

    # User-terminal links: each UT owns a /20 and links to distinct satellites.
    for ut_idx in range(USER_TERMINALS):
        ut_id = 20000 + ut_idx
        for link_idx in range(TERMINAL_LINKS_PER_UT):
            sat_id = (ut_idx * TERMINAL_LINKS_PER_UT + link_idx) % SATELLITES
            allocate(f"utl-{ut_id}-{sat_id}", ut_id, sat_id)

    expected_links = (
        SATELLITES * RING_LINKS_PER_SATELLITE
        + SATELLITES
        + GROUND_STATIONS * GROUND_LINKS_PER_GS
        + USER_TERMINALS * TERMINAL_LINKS_PER_UT
    )
    assert link_count == expected_links, (link_count, expected_links)
    assert len(allocated_networks) == expected_links
    assert len(allocated_ips) == 2 * expected_links + len(nodes)
    assert len(plan.link_addresses()) == expected_links

    # Pool capacities.
    assert plan.capacity_for(NodeKind.GROUND_STATION) == 1024
    assert plan.capacity_for(NodeKind.USER_TERMINAL) == 1024
    assert plan.capacity_for(NodeKind.SATELLITE) == 262_144

    # Satellite block sizes and per-node link capacity.
    satellite_blocks = Counter(
        address.kind for address in nodes.values()
    )
    assert satellite_blocks[NodeKind.SATELLITE] == SATELLITES
    for sat_id in range(SATELLITES):
        assert nodes[sat_id].block.prefixlen == 27
        assert plan.link_capacity_for(sat_id) == 14

    print(f"satellites: {SATELLITES}")
    print(f"ground stations: {GROUND_STATIONS} (one /20 each)")
    print(f"user terminals: {USER_TERMINALS} (one /20 each)")
    print(f"links: {link_count} (all unique /31s)")
    print(f"unique interface addresses: {2 * link_count}")
    print(f"unique loopbacks: {len(nodes)}")
    print(f"satellite pool capacity: {plan.capacity_for(NodeKind.SATELLITE):,}")
    print("OK: all nodes, loopbacks, and links are uniquely addressed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
