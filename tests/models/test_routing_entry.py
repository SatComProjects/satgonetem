"""Tests for the RoutingEntry model."""

import pytest

from satgonetem.models.interface import Interface
from satgonetem.models.routing_entry import RoutingEntry


@pytest.fixture
def iface():
    return Interface(name="Sat0.1")


class TestRoutingEntryInitIPv4:
    """Tests for RoutingEntry initialisation with IPv4."""

    def test_valid_ipv4_stores_summarized_destination(self, iface):
        entry = RoutingEntry(
            destination="10.1.2.3",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        assert entry.destination == "10.0.0.0"

    def test_valid_ipv4_stores_prefix(self, iface):
        entry = RoutingEntry(
            destination="192.168.1.5",
            interface=iface,
            gateway="192.168.1.1",
            prefix=16,
            protocol="ipv4",
        )
        assert entry.prefix == 16

    def test_valid_ipv4_stores_gateway(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.254",
            prefix=8,
            protocol="ipv4",
        )
        assert entry.gateway == "10.0.0.254"

    def test_valid_ipv4_stores_interface(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        assert entry.interface is iface

    def test_valid_ipv4_update_flag_true(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        assert entry.update is True

    def test_source_summarized_when_provided(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
            source="10.1.2.3",
        )
        assert entry.source == "10.0.0.0"

    def test_source_empty_string_when_not_provided(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        assert entry.source == ""

    def test_target_node_stored(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
            target_node="Sat5",
        )
        assert entry.target_node == "Sat5"


class TestRoutingEntryValidation:
    """Tests for RoutingEntry input validation."""

    def test_invalid_protocol_raises_value_error(self, iface):
        with pytest.raises(ValueError, match="Unsupported protocol"):
            RoutingEntry(
                destination="10.0.0.1",
                interface=iface,
                gateway="10.0.0.1",
                protocol="tcp",
            )

    def test_invalid_ipv4_address_raises_value_error(self, iface):
        with pytest.raises(ValueError, match="Invalid IPv4"):
            RoutingEntry(
                destination="not.valid.ip",
                interface=iface,
                gateway="10.0.0.1",
                protocol="ipv4",
            )

    def test_ipv4_prefix_above_32_raises_value_error(self, iface):
        with pytest.raises(ValueError, match="Invalid prefix"):
            RoutingEntry(
                destination="10.0.0.1",
                interface=iface,
                gateway="10.0.0.1",
                prefix=33,
                protocol="ipv4",
            )

    def test_ipv4_prefix_below_0_raises_value_error(self, iface):
        with pytest.raises(ValueError, match="Invalid prefix"):
            RoutingEntry(
                destination="10.0.0.1",
                interface=iface,
                gateway="10.0.0.1",
                prefix=-1,
                protocol="ipv4",
            )

class TestRoutingEntryEquality:
    """Tests for RoutingEntry.__eq__."""

    def test_equal_entries(self, iface):
        entry1 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        entry2 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        assert entry1 == entry2

    def test_not_equal_different_gateway(self, iface):
        entry1 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        entry2 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.2",
            prefix=8,
            protocol="ipv4",
        )
        assert entry1 != entry2

    def test_not_equal_different_prefix(self, iface):
        entry1 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        entry2 = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=16,
            protocol="ipv4",
        )
        assert entry1 != entry2

    def test_not_implemented_for_non_routing_entry(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
        )
        result = entry.__eq__("not_a_routing_entry")
        assert result is NotImplemented


class TestRoutingEntryMethods:
    """Tests for RoutingEntry helper methods."""

    def test_get_prefix_returns_slash_prefix(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=24,
            protocol="ipv4",
        )
        assert entry.get_prefix() == "/24"

    def test_str_contains_destination_gateway_interface(self, iface):
        entry = RoutingEntry(
            destination="10.0.0.1",
            interface=iface,
            gateway="10.0.0.1",
            prefix=8,
            protocol="ipv4",
            target_node="Sat3",
        )
        text = str(entry)
        assert "10.0.0.0" in text
        assert "10.0.0.1" in text
        assert iface.name in text
        assert "Sat3" in text
