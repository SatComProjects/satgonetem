"""Tests for the Interface model."""

import ipaddress

import pytest

from satgonetem.models.interface import Interface


class TestInterfaceInit:
    """Tests for Interface.__init__."""

    def test_default_name_and_type(self):
        iface = Interface()
        assert iface.name == "Default"
        assert iface.type == ""

    def test_custom_name_and_type(self):
        iface = Interface(name="Sat1.5", iface_type="ISL")
        assert iface.name == "Sat1.5"
        assert iface.type == "ISL"

    def test_initial_ip_fields_empty(self):
        iface = Interface()
        assert iface.ipv4 == ""

    def test_initial_flags(self):
        iface = Interface()
        assert iface.is_monitored is False
        assert iface.is_active is False
        assert iface.previously_active is True
        assert iface.delay == 0


class TestInterfaceSetIp:
    """Tests for Interface.set_ip."""

    def test_set_ip(self):
        iface = Interface(name="Sat0.1")
        iface.set_ip("10.0.0.1")
        assert iface.ipv4 == "10.0.0.1"

    def test_set_ip_overwrites_previous(self):
        iface = Interface(name="Sat0.1")
        iface.set_ip("10.0.0.1")
        iface.set_ip("10.0.0.2")
        assert iface.ipv4 == "10.0.0.2"


class TestInterfaceSetIpv4:
    """Tests for prefix-aware address assignment."""

    def test_link_defaults_to_slash31(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv4("10.128.0.4")
        assert iface.ipv4 == "10.128.0.4"
        assert iface.prefix_len == 31
        assert iface.network == ipaddress.ip_network("10.128.0.4/31")

    def test_loopback_defaults_to_slash32(self):
        iface = Interface(name="lo_Sat0", iface_type="lo")
        iface.set_ipv4("10.128.0.1")
        assert iface.prefix_len == 32
        assert iface.network == ipaddress.ip_network("10.128.0.1/32")

    def test_explicit_prefix_is_stored(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv4("10.128.0.4", 20)
        assert iface.prefix_len == 20
        assert iface.network == ipaddress.ip_network("10.128.0.0/20")

    def test_invalid_address_raises(self):
        iface = Interface(name="Sat0.1")
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            iface.set_ipv4("not-an-address")

    def test_invalid_prefix_raises(self):
        iface = Interface(name="Sat0.1")
        with pytest.raises(ValueError, match="Invalid IPv4 prefix"):
            iface.set_ipv4("10.128.0.4", 33)

    def test_network_is_none_without_address(self):
        assert Interface(name="Sat0.1").network is None


class TestInterfaceSetIpv6:
    """Tests for the independent IPv6 address fields."""

    def test_initial_ipv6_fields(self):
        assert Interface(name="Sat0.1").ipv6 == ""
        assert Interface(name="Sat0.1").ipv6_prefix_len == 127

    def test_loopback_defaults_to_slash128(self):
        iface = Interface(name="lo_Sat0", iface_type="lo")
        assert iface.ipv6_prefix_len == 128

    def test_link_defaults_to_slash127(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv6("fd00:2::4")

        assert iface.ipv6 == "fd00:2::4"
        assert iface.ipv6_prefix_len == 127
        assert iface.network6 == ipaddress.ip_network("fd00:2::4/127")

    def test_loopback_set_ipv6_uses_slash128(self):
        iface = Interface(name="lo_Sat0", iface_type="lo")
        iface.set_ipv6("fd00:2::1")

        assert iface.ipv6_prefix_len == 128
        assert iface.network6 == ipaddress.ip_network("fd00:2::1/128")

    def test_explicit_prefix_is_stored(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv6("fd00:2::4", 64)

        assert iface.ipv6_prefix_len == 64
        assert iface.network6 == ipaddress.ip_network("fd00:2::/64")

    def test_invalid_address_raises(self):
        iface = Interface(name="Sat0.1")
        with pytest.raises(ValueError, match="Invalid IPv6 address"):
            iface.set_ipv6("10.128.0.4")

    def test_invalid_prefix_raises(self):
        iface = Interface(name="Sat0.1")
        with pytest.raises(ValueError, match="Invalid IPv6 prefix"):
            iface.set_ipv6("fd00:2::4", 129)

    def test_network6_is_none_without_address(self):
        assert Interface(name="Sat0.1").network6 is None

    def test_ipv4_and_ipv6_are_independent(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv4("10.128.0.4", 31)
        iface.set_ipv6("fd00:2::4", 127)

        assert (iface.ipv4, iface.prefix_len) == ("10.128.0.4", 31)
        assert (iface.ipv6, iface.ipv6_prefix_len) == ("fd00:2::4", 127)

    def test_set_ipv6_leaves_ipv4_and_prefix_len_intact(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv4("10.128.0.4", 31)

        iface.set_ipv6("fd00:2::4", 128)

        assert (iface.ipv4, iface.prefix_len) == ("10.128.0.4", 31)
        assert (iface.ipv6, iface.ipv6_prefix_len) == ("fd00:2::4", 128)

    def test_set_ipv4_leaves_ipv6_and_its_prefix_intact(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv6("fd00:2::4", 127)

        iface.set_ipv4("10.128.0.4", 32)

        assert (iface.ipv6, iface.ipv6_prefix_len) == ("fd00:2::4", 127)
        assert (iface.ipv4, iface.prefix_len) == ("10.128.0.4", 32)


class TestInterfaceGetIname:
    """Tests for Interface.get_iname."""

    def test_loopback_type_returns_lo(self):
        iface = Interface(name="lo_Sat1", iface_type="lo")
        assert iface.get_iname() == "lo"

    def test_regular_returns_eth_with_suffix(self):
        iface = Interface(name="Sat1.5")
        assert iface.get_iname() == "eth5"

    def test_regular_zero_suffix(self):
        iface = Interface(name="Sat0.0")
        assert iface.get_iname() == "eth0"

    def test_regular_multi_digit_suffix(self):
        iface = Interface(name="Sat0.12")
        assert iface.get_iname() == "eth12"


class TestInterfaceEquality:
    """Tests for Interface.__eq__."""

    def test_equal_same_name(self):
        iface1 = Interface(name="Sat1.5")
        iface2 = Interface(name="Sat1.5")
        assert iface1 == iface2

    def test_not_equal_different_name(self):
        iface1 = Interface(name="Sat1.5")
        iface2 = Interface(name="Sat1.6")
        assert iface1 != iface2

    def test_returns_not_implemented_for_non_interface(self):
        iface = Interface(name="Sat1.5")
        result = iface.__eq__("not_an_interface")
        assert result is NotImplemented

    def test_equal_ignores_ip_difference(self):
        iface1 = Interface(name="Sat1.5")
        iface2 = Interface(name="Sat1.5")
        iface1.set_ip("10.0.0.1")
        iface2.set_ip("10.0.0.2")
        assert iface1 == iface2


class TestInterfacePeerInitialization:
    """The peer is optional until the interface is wired to its link peer."""

    def test_peer_is_none_by_default(self):
        assert Interface(name="Sat0.1").peer is None

    def test_peer_can_be_assigned(self):
        source = Interface(name="Sat0.1")
        target = Interface(name="Gnd0.0", iface_type="GndLink")
        source.peer = target
        assert source.peer is target


class TestInterfaceGetInameValidation:
    """Plain names cannot be mapped to a container interface name."""

    def test_plain_name_raises_value_error(self):
        iface = Interface(name="iface0")
        with pytest.raises(ValueError, match="has no '.id' suffix"):
            iface.get_iname()

    def test_missing_suffix_message_names_the_interface(self):
        iface = Interface(name="loopback")
        with pytest.raises(ValueError, match="loopback"):
            iface.get_iname()


class TestInterfaceHashing:
    """Interfaces are hashable consistently with name-based equality."""

    def test_equal_interfaces_have_equal_hash(self):
        first = Interface(name="Sat1.5")
        second = Interface(name="Sat1.5")
        assert first == second
        assert hash(first) == hash(second)

    def test_interface_can_be_used_in_a_set(self):
        first = Interface(name="Sat1.5")
        second = Interface(name="Sat1.5")
        third = Interface(name="Sat1.6")
        assert {first, second, third} == {first, third}

    def test_hash_does_not_depend_on_ip_or_type(self):
        first = Interface(name="Sat1.5")
        second = Interface(name="Sat1.5", iface_type="ISL")
        second.set_ip("10.0.0.1")
        assert hash(first) == hash(second)
