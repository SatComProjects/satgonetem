"""Tests for the pure IPv6 address planner."""

from __future__ import annotations

import ipaddress

import pytest

from satgonetem.addressing import (
    AddressPlanConfigurationError,
    AddressPlanExhausted,
    Ipv6AddressPlan,
    NodeKind,
)


def make_plan(
    *,
    gs_pool: str = "fd00:1::/40",
    sat_pool: str = "fd00:2::/40",
    ut_pool: str = "fd00:3::/40",
    gs_prefix: int = 64,
    sat_prefix: int = 64,
    ut_prefix: int = 64,
) -> Ipv6AddressPlan:
    return Ipv6AddressPlan(
        ground_station_pool=gs_pool,
        satellite_pool=sat_pool,
        user_terminal_pool=ut_pool,
        ground_station_prefix_len=gs_prefix,
        satellite_prefix_len=sat_prefix,
        user_terminal_prefix_len=ut_prefix,
    )


class TestDefaults:
    def test_default_pools_and_prefix_lengths(self):
        plan = make_plan()

        assert plan.pool_for(NodeKind.GROUND_STATION) == ipaddress.ip_network(
            "fd00:1::/40"
        )
        assert plan.pool_for(NodeKind.SATELLITE) == ipaddress.ip_network("fd00:2::/40")
        assert plan.pool_for(NodeKind.USER_TERMINAL) == ipaddress.ip_network(
            "fd00:3::/40"
        )
        for kind in NodeKind:
            assert plan.prefix_len_for(kind) == 64

    def test_capacities(self):
        plan = make_plan()

        assert plan.capacity_for(NodeKind.GROUND_STATION) == 1 << 24
        assert plan.capacity_for(NodeKind.SATELLITE) == 1 << 24
        assert plan.capacity_for(NodeKind.USER_TERMINAL) == 1 << 24

    def test_pools_are_private_ula(self):
        plan = make_plan()
        for kind in NodeKind:
            pool = plan.pool_for(kind)
            assert pool.is_private
            assert not pool.is_global
            assert not pool.is_multicast


class TestNodeBlocks:
    def test_ground_station_gets_slash64_and_slash128_loopback(self):
        plan = make_plan()
        gs0 = plan.register_node(77, NodeKind.GROUND_STATION)
        gs1 = plan.register_node(78, NodeKind.GROUND_STATION)

        assert gs0.block == ipaddress.ip_network("fd00:1::/64")
        assert gs0.loopback == ipaddress.ip_interface("fd00:1::1/128")
        assert gs1.block == ipaddress.ip_network("fd00:1:0:1::/64")
        assert gs1.loopback == ipaddress.ip_interface("fd00:1:0:1::1/128")

    def test_satellite_gets_slash64_and_slash128_loopback(self):
        plan = make_plan()
        sat0 = plan.register_node(0, NodeKind.SATELLITE)
        sat1 = plan.register_node(1, NodeKind.SATELLITE)

        assert sat0.block == ipaddress.ip_network("fd00:2::/64")
        assert sat0.loopback == ipaddress.ip_interface("fd00:2::1/128")
        assert sat1.block == ipaddress.ip_network("fd00:2:0:1::/64")
        assert sat1.loopback == ipaddress.ip_interface("fd00:2:0:1::1/128")

    def test_user_terminal_gets_slash64_and_slash128_loopback(self):
        plan = make_plan()
        ut = plan.register_node(10_000, NodeKind.USER_TERMINAL)

        assert ut.block == ipaddress.ip_network("fd00:3::/64")
        assert ut.loopback == ipaddress.ip_interface("fd00:3::1/128")

    def test_blocks_are_unique(self):
        plan = make_plan()
        blocks = {
            plan.register_node(node_id, NodeKind.SATELLITE).block
            for node_id in range(10)
        }
        assert len(blocks) == 10

    def test_registration_is_idempotent_for_same_kind(self):
        plan = make_plan()
        first = plan.register_node(1, NodeKind.SATELLITE)
        second = plan.register_node(1, NodeKind.SATELLITE)
        assert first is second

    def test_reregister_with_different_kind_fails(self):
        plan = make_plan()
        plan.register_node(1, NodeKind.SATELLITE)
        with pytest.raises(ValueError, match="already registered"):
            plan.register_node(1, NodeKind.GROUND_STATION)

    def test_node_addresses_preserve_registration_order(self):
        plan = make_plan()
        plan.register_node(2, NodeKind.SATELLITE)
        plan.register_node(0, NodeKind.SATELLITE)

        assert [address.node_id for address in plan.node_addresses()] == [2, 0]
        assert plan.node_address(2).block == ipaddress.ip_network("fd00:2::/64")

    def test_unregistered_node_address_fails(self):
        plan = make_plan()
        with pytest.raises(ValueError, match="not registered"):
            plan.node_address(3)

    def test_node_pool_exhaustion(self):
        plan = make_plan(sat_pool="fd00:2::/123", sat_prefix=124)
        plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)

        with pytest.raises(AddressPlanExhausted, match="capacity is 2"):
            plan.register_node(2, NodeKind.SATELLITE)

    def test_invalid_node_ids_rejected(self):
        plan = make_plan()
        for bad in (-1, 1.5, True, "3"):
            with pytest.raises(ValueError, match="non-negative integer"):
                plan.register_node(bad, NodeKind.SATELLITE)  # type: ignore[arg-type]


class TestLinkAddresses:
    def test_gs_sat_link_is_owned_by_ground_station(self):
        plan = make_plan()
        gs = plan.register_node(77, NodeKind.GROUND_STATION)
        sat = plan.register_node(6, NodeKind.SATELLITE)

        link = plan.allocate_link("Gnd77--Sat6", sat.node_id, gs.node_id)

        assert link.owner_id == gs.node_id
        assert link.prefix_len == 127
        # The ground-station endpoint always receives the lower address.
        assert link.target == ipaddress.ip_interface("fd00:1::4/127")
        assert link.source == ipaddress.ip_interface("fd00:1::5/127")
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(gs.block)

    def test_every_link_gets_a_unique_slash127(self):
        plan = make_plan()
        satellite_0 = plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)
        plan.register_node(2, NodeKind.SATELLITE)

        first = plan.allocate_link("a", 0, 1)
        second = plan.allocate_link("b", 0, 2)

        assert first.slot == 0
        assert second.slot == 1
        assert ipaddress.ip_network(first.source, strict=False) == ipaddress.ip_network(
            "fd00:2::4/127"
        )
        assert ipaddress.ip_network(second.source, strict=False) == (
            ipaddress.ip_network("fd00:2::6/127")
        )
        assert first.owner_id == satellite_0.node_id
        assert second.owner_id == satellite_0.node_id

    def test_link_addresses_are_orientation_independent(self):
        def allocate(source_id: int, target_id: int):
            plan = make_plan()
            plan.register_node(0, NodeKind.GROUND_STATION)
            plan.register_node(1, NodeKind.SATELLITE)
            return plan.allocate_link("same-link", source_id, target_id)

        forward = allocate(0, 1)
        reverse = allocate(1, 0)

        assert forward.owner_id == reverse.owner_id == 0
        assert {forward.source, forward.target} == {reverse.source, reverse.target}
        assert forward.source == ipaddress.ip_interface("fd00:1::4/127")
        assert reverse.target == ipaddress.ip_interface("fd00:1::4/127")

    def test_satellite_link_owned_by_lower_id(self):
        plan = make_plan()
        sat5 = plan.register_node(5, NodeKind.SATELLITE)
        sat3 = plan.register_node(3, NodeKind.SATELLITE)

        link = plan.allocate_link("Sat5--Sat3", 5, 3)

        assert link.owner_id == 3
        assert link.owner_id != sat5.node_id
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(sat3.block)
        assert link.target == ipaddress.ip_interface("fd00:2:0:1::4/127")
        assert link.source == ipaddress.ip_interface("fd00:2:0:1::5/127")

    def test_lower_endpoint_gets_lower_address(self):
        plan = make_plan()
        plan.register_node(5, NodeKind.SATELLITE)
        plan.register_node(3, NodeKind.SATELLITE)

        link = plan.allocate_link("link", 3, 5)
        assert link.source.ip < link.target.ip
        assert int(link.source.ip) == int(link.target.ip) - 1

        reverse = plan.allocate_link("reverse", 5, 3)
        assert reverse.target.ip < reverse.source.ip

    def test_ut_link_is_owned_by_ut(self):
        plan = make_plan()
        ut = plan.register_node(100, NodeKind.USER_TERMINAL)
        sat = plan.register_node(0, NodeKind.SATELLITE)

        link = plan.allocate_link("ut-sat", sat.node_id, ut.node_id)
        assert link.owner_id == ut.node_id
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(ut.block)

    def test_link_allocation_is_cached(self):
        plan = make_plan()
        plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)

        first = plan.allocate_link("x", 0, 1)
        second = plan.allocate_link("x", 1, 0)
        assert first is second

    def test_release_reuses_lowest_slot(self):
        plan = make_plan()
        plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)
        plan.register_node(2, NodeKind.SATELLITE)

        first = plan.allocate_link("a", 0, 1)
        second = plan.allocate_link("b", 0, 2)
        plan.release_link("a")
        third = plan.allocate_link("c", 0, 2)

        assert first.slot == 0
        assert second.slot == 1
        assert third.slot == 0
        assert third.source == first.source
        assert third.target == first.target

    def test_releasing_unknown_link_is_a_noop(self):
        plan = make_plan()
        plan.release_link("missing")
        assert plan.link_addresses() == ()

    def test_link_slot_exhaustion(self):
        # A /125 block has two /127 slots after the reserved base addresses.
        plan = make_plan(sat_pool="fd00:2::/123", sat_prefix=125)
        for node_id in range(4):
            plan.register_node(node_id, NodeKind.SATELLITE)

        plan.allocate_link("a", 0, 1)
        plan.allocate_link("b", 0, 2)
        with pytest.raises(AddressPlanExhausted, match="no free /127 slots"):
            plan.allocate_link("c", 0, 3)

    def test_self_link_fails(self):
        plan = make_plan()
        plan.register_node(0, NodeKind.SATELLITE)
        with pytest.raises(ValueError, match="cannot connect node"):
            plan.allocate_link("self", 0, 0)

    def test_unregistered_node_fails(self):
        plan = make_plan()
        with pytest.raises(ValueError, match="not registered"):
            plan.allocate_link("missing", 0, 1)


class TestConfiguration:
    @pytest.mark.parametrize(
        "bad_pool",
        [
            "fd00:1::1/40",  # host bits set
            "10.0.0.0/8",  # IPv4
            "ff00::/8",  # multicast
            "::1/128",  # loopback
            "fe80::/10",  # link-local
            "::/0",  # unspecified
            "2001:4860::/32",  # globally routable
        ],
    )
    def test_bad_pool_rejected(self, bad_pool):
        with pytest.raises(AddressPlanConfigurationError):
            make_plan(gs_pool=bad_pool)

    def test_overlapping_pools_rejected(self):
        with pytest.raises(AddressPlanConfigurationError, match="overlaps"):
            make_plan(gs_pool="fd00:2::/39", sat_pool="fd00:2::/40")

    def test_prefix_must_fit_pool(self):
        with pytest.raises(AddressPlanConfigurationError, match="does not fit"):
            make_plan(gs_pool="fd00:1::/65", gs_prefix=64)

    @pytest.mark.parametrize("bad_prefix", [-1, 129, 1.5, True, "64"])
    def test_prefix_must_be_an_integer_in_range(self, bad_prefix):
        with pytest.raises(AddressPlanConfigurationError, match="prefix length"):
            make_plan(gs_prefix=bad_prefix)

    def test_tiny_block_rejected(self):
        with pytest.raises(AddressPlanConfigurationError, match="too small"):
            make_plan(sat_pool="fd00:2::/124", sat_prefix=126)

    def test_separate_pools_are_exposed(self):
        plan = make_plan()
        assert plan.pool_for(NodeKind.GROUND_STATION) == ipaddress.ip_network(
            "fd00:1::/40"
        )
        assert plan.prefix_len_for(NodeKind.SATELLITE) == 64
        gs = plan.register_node(0, NodeKind.GROUND_STATION)
        assert plan.link_capacity_for(gs.node_id) == (1 << 64) // 2 - 2
