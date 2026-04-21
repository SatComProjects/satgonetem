"""Tests for the Interface model."""

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
        assert iface.ipv6 == ""

    def test_initial_flags(self):
        iface = Interface()
        assert iface.is_monitored is False
        assert iface.is_active is False
        assert iface.previously_active is True
        assert iface.delay == 0


class TestInterfaceSetIp:
    """Tests for Interface.set_ip and set_ipv6."""

    def test_set_ip(self):
        iface = Interface(name="Sat0.1")
        iface.set_ip("10.0.0.1")
        assert iface.ipv4 == "10.0.0.1"

    def test_set_ipv6(self):
        iface = Interface(name="Sat0.1")
        iface.set_ipv6("2001:db8::1")
        assert iface.ipv6 == "2001:db8::1"

    def test_set_ip_overwrites_previous(self):
        iface = Interface(name="Sat0.1")
        iface.set_ip("10.0.0.1")
        iface.set_ip("10.0.0.2")
        assert iface.ipv4 == "10.0.0.2"


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
