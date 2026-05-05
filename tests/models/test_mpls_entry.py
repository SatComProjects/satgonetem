"""Tests for MPLS data models: MPLSConfig, SRNodeSIDEntry, SRForwardEntry, SRLabelStackEntry."""

import pytest

from satgonetem.models.interface import Interface
from satgonetem.models.mpls_entry import (
    MPLS_LABEL_MAX,
    MPLS_LABEL_MIN,
    MPLSConfig,
    SRForwardEntry,
    SRLabelStackEntry,
    SRNodeSIDEntry,
)


class TestMPLSConfig:
    """Tests for MPLSConfig dataclass."""

    def test_default_values(self):
        config = MPLSConfig()
        assert config.enabled is False
        assert config.label_range_start == MPLS_LABEL_MIN
        assert config.label_range_end == MPLS_LABEL_MAX
        assert config.use_ldp is False
        assert config.use_php is False
        assert config.ttl == 64
        assert config.use_sr is True
        assert config.sr_node_sid_base == 16000

    def test_custom_values(self):
        config = MPLSConfig(enabled=True, ttl=128, sr_node_sid_base=20000)
        assert config.enabled is True
        assert config.ttl == 128
        assert config.sr_node_sid_base == 20000

    def test_label_range_start_clamped_to_minimum(self):
        config = MPLSConfig(label_range_start=0)
        assert config.label_range_start == MPLS_LABEL_MIN

    def test_label_range_end_clamped_to_maximum(self):
        config = MPLSConfig(label_range_end=9_999_999)
        assert config.label_range_end == MPLS_LABEL_MAX

    def test_start_equal_to_end_raises_value_error(self):
        with pytest.raises(ValueError):
            MPLSConfig(label_range_start=100, label_range_end=100)

    def test_start_greater_than_end_raises_value_error(self):
        with pytest.raises(ValueError):
            MPLSConfig(label_range_start=200, label_range_end=100)

    def test_valid_custom_range(self):
        config = MPLSConfig(label_range_start=100, label_range_end=200)
        assert config.label_range_start == 100
        assert config.label_range_end == 200


class TestSRNodeSIDEntry:
    """Tests for SRNodeSIDEntry dataclass."""

    def test_to_iproute2_command(self):
        entry = SRNodeSIDEntry(node_sid=16001, node_name="Sat1")
        cmd = entry.to_iproute2_command()
        assert cmd == "ip -f mpls route replace 16001 dev lo"

    def test_to_iproute2_command_different_sid(self):
        entry = SRNodeSIDEntry(node_sid=16050)
        cmd = entry.to_iproute2_command()
        assert "16050" in cmd
        assert "dev lo" in cmd

    def test_to_iproute2_batch_line(self):
        entry = SRNodeSIDEntry(node_sid=16001, node_name="Sat1")
        line = entry.to_iproute2_batch_line()
        assert line == "-f mpls route replace 16001 dev lo"
        assert not line.startswith("ip ")

    def test_str_contains_node_name_and_sid(self):
        entry = SRNodeSIDEntry(node_sid=16001, node_name="Sat1")
        text = str(entry)
        assert "Sat1" in text
        assert "16001" in text
        assert "pop to local" in text

    def test_default_node_name_empty(self):
        entry = SRNodeSIDEntry(node_sid=16001)
        assert entry.node_name == ""


class TestSRForwardEntry:
    """Tests for SRForwardEntry dataclass."""

    @pytest.fixture
    def iface(self):
        return Interface(name="Sat0.5")

    def test_to_iproute2_command(self, iface):
        entry = SRForwardEntry(
            target_sid=16003,
            next_hop="10.0.0.2",
            interface=iface,
            target_name="Sat3",
        )
        cmd = entry.to_iproute2_command()
        assert cmd == "ip -f mpls route replace 16003 via inet 10.0.0.2 dev eth5"

    def test_to_iproute2_command_uses_interface_iname(self, iface):
        entry = SRForwardEntry(
            target_sid=16007,
            next_hop="10.0.0.8",
            interface=iface,
        )
        cmd = entry.to_iproute2_command()
        assert "eth5" in cmd

    def test_to_iproute2_batch_line(self, iface):
        entry = SRForwardEntry(
            target_sid=16003,
            next_hop="10.0.0.2",
            interface=iface,
            target_name="Sat3",
        )
        line = entry.to_iproute2_batch_line()
        assert line == "-f mpls route replace 16003 via inet 10.0.0.2 dev eth5"
        assert not line.startswith("ip ")

    def test_str_contains_sid_name_and_hop(self, iface):
        entry = SRForwardEntry(
            target_sid=16003,
            next_hop="10.0.0.2",
            interface=iface,
            target_name="Sat3",
        )
        text = str(entry)
        assert "16003" in text
        assert "Sat3" in text
        assert "10.0.0.2" in text


class TestSRLabelStackEntry:
    """Tests for SRLabelStackEntry dataclass."""

    @pytest.fixture
    def iface(self):
        return Interface(name="Gnd0.3")

    def test_to_iproute2_command_with_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[16003, 16007],
            next_hop="10.1.0.1",
            interface=iface,
            fec_prefix=32,
        )
        cmd = entry.to_iproute2_command()
        assert "encap mpls 16003/16007" in cmd
        assert "10.0.0.1/32" in cmd
        assert "10.1.0.1" in cmd

    def test_to_iproute2_command_without_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[],
            next_hop="10.1.0.1",
            interface=iface,
            fec_prefix=32,
        )
        cmd = entry.to_iproute2_command()
        assert "encap" not in cmd
        assert "10.0.0.1/32" in cmd
        assert "10.1.0.1" in cmd

    def test_to_iproute2_command_single_label(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.5",
            label_stack=[16003],
            next_hop="10.1.0.1",
            interface=iface,
        )
        cmd = entry.to_iproute2_command()
        assert "encap mpls 16003" in cmd
        assert "/" not in cmd.split("mpls ")[1].split(" ")[0]

    def test_to_iproute2_batch_line_with_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[16003, 16007],
            next_hop="10.1.0.1",
            interface=iface,
            fec_prefix=32,
        )
        line = entry.to_iproute2_batch_line()
        assert line == "route replace 10.0.0.1/32 encap mpls 16003/16007 via 10.1.0.1 dev eth3"
        assert not line.startswith("ip ")

    def test_to_iproute2_batch_line_without_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[],
            next_hop="10.1.0.1",
            interface=iface,
            fec_prefix=32,
        )
        line = entry.to_iproute2_batch_line()
        assert line == "route replace 10.0.0.1/32 via 10.1.0.1 dev eth3"
        assert not line.startswith("ip ")

    def test_to_iproute2_batch_line_single_label(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.5",
            label_stack=[16003],
            next_hop="10.1.0.1",
            interface=iface,
        )
        line = entry.to_iproute2_batch_line()
        assert "encap mpls 16003" in line
        assert not line.startswith("ip ")
        assert "/" not in line.split("mpls ")[1].split(" ")[0]

    def test_str_with_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[16003, 16007],
            next_hop="10.1.0.1",
            interface=iface,
            fec_prefix=32,
        )
        text = str(entry)
        assert "10.0.0.1/32" in text
        assert "16003/16007" in text
        assert "10.1.0.1" in text

    def test_str_without_labels(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[],
            next_hop="10.1.0.1",
            interface=iface,
        )
        text = str(entry)
        assert "(empty)" in text

    def test_default_fec_prefix_is_32(self, iface):
        entry = SRLabelStackEntry(
            destination="10.0.0.1",
            label_stack=[16003],
            next_hop="10.1.0.1",
            interface=iface,
        )
        assert entry.fec_prefix == 32
