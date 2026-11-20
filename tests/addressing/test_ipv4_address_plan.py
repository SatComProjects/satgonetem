"""Tests for the pure IPv4 address planner."""

from __future__ import annotations

import ipaddress

import pytest

from satgonetem.addressing import (
    AddressPlanConfigurationError,
    AddressPlanExhausted,
    Ipv4AddressPlan,
    NodeKind,
)


def make_plan(
    *,
    gs_pool: str = "10.0.0.0/10",
    sat_pool: str = "10.128.0.0/9",
    ut_pool: str = "10.64.0.0/10",
    gs_prefix: int = 20,
    sat_prefix: int = 27,
    ut_prefix: int = 20,
) -> Ipv4AddressPlan:
    return Ipv4AddressPlan(
        ground_station_pool=gs_pool,
        satellite_pool=sat_pool,
        user_terminal_pool=ut_pool,
        ground_station_prefix_len=gs_prefix,
        satellite_prefix_len=sat_prefix,
        user_terminal_prefix_len=ut_prefix,
    )


class TestNodeBlocks:
    def test_ground_station_gets_20_and_loopback(self):
        plan = make_plan()
        gs0 = plan.register_node(77, NodeKind.GROUND_STATION)
        gs1 = plan.register_node(78, NodeKind.GROUND_STATION)

        assert gs0.block == ipaddress.ip_network("10.0.0.0/20")
        assert gs0.loopback == ipaddress.ip_interface("10.0.0.1/32")
        assert gs1.block == ipaddress.ip_network("10.0.16.0/20")
        assert gs1.loopback == ipaddress.ip_interface("10.0.16.1/32")

    def test_satellite_gets_27_and_loopback(self):
        plan = make_plan()
        sat0 = plan.register_node(0, NodeKind.SATELLITE)
        sat1 = plan.register_node(1, NodeKind.SATELLITE)

        assert sat0.block == ipaddress.ip_network("10.128.0.0/27")
        assert sat0.loopback == ipaddress.ip_interface("10.128.0.1/32")
        assert sat1.block == ipaddress.ip_network("10.128.0.32/27")
        assert sat1.loopback == ipaddress.ip_interface("10.128.0.33/32")

    def test_user_terminal_gets_20_and_loopback(self):
        plan = make_plan()
        ut = plan.register_node(10_000, NodeKind.USER_TERMINAL)
        assert ut.block == ipaddress.ip_network("10.64.0.0/20")
        assert ut.loopback == ipaddress.ip_interface("10.64.0.1/32")

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

    def test_arbitrary_node_ids_do_not_need_thirteen_bits(self):
        plan = make_plan()
        node = plan.register_node(10_000, NodeKind.SATELLITE)
        assert node.slot == 0
        assert node.block == ipaddress.ip_network("10.128.0.0/27")

    def test_node_pool_exhaustion(self):
        plan = make_plan(sat_pool="10.128.0.0/23", sat_prefix=24)
        plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)
        with pytest.raises(AddressPlanExhausted, match="capacity is 2"):
            plan.register_node(2, NodeKind.SATELLITE)

    def test_capacities(self):
        plan = make_plan()
        assert plan.capacity_for(NodeKind.GROUND_STATION) == 1024
        assert plan.capacity_for(NodeKind.SATELLITE) == 262_144
        assert plan.capacity_for(NodeKind.USER_TERMINAL) == 1024
        gs = plan.register_node(0, NodeKind.GROUND_STATION)
        sat = plan.register_node(1, NodeKind.SATELLITE)
        assert plan.link_capacity_for(gs.node_id) == 2046
        assert plan.link_capacity_for(sat.node_id) == 14


class TestLinkAddresses:
    def test_gs_sat_link_is_owned_by_ground_station(self):
        plan = make_plan()
        gs = plan.register_node(77, NodeKind.GROUND_STATION)
        sat = plan.register_node(6, NodeKind.SATELLITE)

        link = plan.allocate_link("Gnd77--Sat6", sat.node_id, gs.node_id)

        assert link.owner_id == gs.node_id
        assert link.prefix_len == 31
        # The lower-priority (ground-station) endpoint gets the lower address.
        assert link.target == ipaddress.ip_interface("10.0.0.4/31")
        assert link.source == ipaddress.ip_interface("10.0.0.5/31")
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(gs.block)

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
        # The ground station always receives the lower address of the /31.
        assert forward.source == ipaddress.ip_interface("10.0.0.4/31")
        assert reverse.target == ipaddress.ip_interface("10.0.0.4/31")

    def test_satellite_link_owned_by_lower_id(self):
        plan = make_plan()
        # Register 5 first so its block is 10.128.0.0/24; 3 must still own the
        # link even though it was registered second.
        sat5 = plan.register_node(5, NodeKind.SATELLITE)
        sat3 = plan.register_node(3, NodeKind.SATELLITE)

        link = plan.allocate_link("Sat5--Sat3", 5, 3)

        assert link.owner_id == 3
        assert link.owner_id != sat5.node_id
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(sat3.block)
        assert link.target == ipaddress.ip_interface("10.128.0.36/31")
        assert link.source == ipaddress.ip_interface("10.128.0.37/31")
        assert ipaddress.ip_network(link.source, strict=False) == ipaddress.ip_network(
            link.target, strict=False
        )

    def test_lower_endpoint_gets_lower_address(self):
        plan = make_plan()
        plan.register_node(5, NodeKind.SATELLITE)
        plan.register_node(3, NodeKind.SATELLITE)

        link = plan.allocate_link("link", 3, 5)
        assert link.source.ip < link.target.ip
        assert int(link.source.ip) == int(link.target.ip) - 1

        reverse = plan.allocate_link("reverse", 5, 3)
        # The lower-ID endpoint is the target here, so it receives .4 and the
        # source receives .5.
        assert reverse.target.ip < reverse.source.ip

    def test_gs_gs_link_uses_lower_gs(self):
        plan = make_plan()
        plan.register_node(10, NodeKind.GROUND_STATION)
        plan.register_node(20, NodeKind.GROUND_STATION)

        link = plan.allocate_link("gs-gs", 20, 10)
        assert link.owner_id == 10
        assert link.source.is_private and link.target.is_private
        assert ipaddress.ip_network(link.source, strict=False).subnet_of(
            plan.node_address(10).block
        )

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

    def test_link_slot_exhaustion(self):
        plan = make_plan(sat_pool="10.128.0.0/26", sat_prefix=29)
        plan.register_node(0, NodeKind.SATELLITE)
        plan.register_node(1, NodeKind.SATELLITE)
        plan.register_node(2, NodeKind.SATELLITE)
        plan.register_node(3, NodeKind.SATELLITE)

        # /29 has room for exactly two /31s after the loopback.
        plan.allocate_link("a", 0, 1)
        plan.allocate_link("b", 0, 2)
        with pytest.raises(AddressPlanExhausted, match="no free /31 slots"):
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
            "10.0.0.1/9",  # host bits
            "2001:db8::/32",  # IPv6
            "224.0.0.0/4",  # multicast
            "127.0.0.0/8",  # loopback
            "8.8.8.0/24",  # globally routable
        ],
    )
    def test_bad_pool_rejected(self, bad_pool):
        with pytest.raises(AddressPlanConfigurationError):
            make_plan(gs_pool=bad_pool)

    def test_overlapping_pools_rejected(self):
        with pytest.raises(AddressPlanConfigurationError, match="overlaps"):
            make_plan(gs_pool="10.0.0.0/8", sat_pool="10.128.0.0/9")

    def test_prefix_must_fit_pool(self):
        with pytest.raises(AddressPlanConfigurationError, match="does not fit"):
            make_plan(gs_pool="10.0.0.0/21", gs_prefix=20)

    def test_tiny_block_rejected(self):
        with pytest.raises(AddressPlanConfigurationError, match="too small"):
            make_plan(sat_pool="10.128.0.0/24", sat_prefix=30)

    def test_separate_pools_are_exposed(self):
        plan = make_plan()
        assert str(plan.pool_for(NodeKind.GROUND_STATION)) == "10.0.0.0/10"
        assert plan.prefix_len_for(NodeKind.SATELLITE) == 27
