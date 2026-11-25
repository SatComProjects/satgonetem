"""Tests for the P4 graph edge export."""

from __future__ import annotations

import pytest

from satgonetem.models.interface import Interface
from satgonetem.routing.p4_daemon import build_edge_from_link, mac_to_int
from tests.support import make_ground_station, make_link, make_satellite

pytestmark = pytest.mark.unit


def _attach_link_interfaces(link):
    source_iface = Interface(name=f"{link.source.name}.{link.target.id}")
    target_iface = Interface(name=f"{link.target.name}.{link.source.id}")
    link.peer_interfaces = [source_iface, target_iface]
    return source_iface, target_iface


def test_build_edge_uses_numeric_ids_ports_and_macs():
    source = make_satellite("Sat1")
    target = make_ground_station("Gnd2")
    link = make_link(source, target)

    source_iface, target_iface = _attach_link_interfaces(link)
    source_iface.set_ipv4("10.0.0.4")
    target_iface.set_ipv4("10.0.0.5")
    source_iface.set_mac_address("aa:bb:cc:dd:ee:01")
    target_iface.set_mac_address("aa:bb:cc:dd:ee:02")

    edge = build_edge_from_link(link)

    assert edge == {
        "weight": 1,
        "src_name": 1,
        "dst_name": 2,
        "src_ip": "10.0.0.4",
        "dst_ip": "10.0.0.5",
        "src_mac": 0xAABBCCDDEE01,
        "dst_mac": 0xAABBCCDDEE02,
        "src_port": 2,
        "dst_port": 1,
        "flag_gateway": 2,
    }


def test_mac_to_int_handles_missing_and_malformed_values():
    assert mac_to_int(None) == 0
    assert mac_to_int("") == 0
    assert mac_to_int("not-a-mac") == 0
    assert mac_to_int("aa:bb:cc:dd:ee:ff") == 0xAABBCCDDEEFF
    assert mac_to_int("AA-BB-CC-DD-EE-FF") == 0xAABBCCDDEEFF


def test_build_edge_defaults_missing_addresses_and_ports_to_zero():
    source = make_satellite("Sat0")
    target = make_satellite("Sat3")
    link = make_link(source, target)
    source_iface, target_iface = _attach_link_interfaces(link)

    # Explicitly empty MACs avoid the Node command fallback.
    source_iface.set_mac_address("")
    target_iface.set_mac_address("")

    edge = build_edge_from_link(link)

    assert edge["src_name"] == 0
    assert edge["dst_name"] == 3
    assert edge["src_ip"] == "0.0.0.0"
    assert edge["dst_ip"] == "0.0.0.0"
    assert edge["src_mac"] == 0
    assert edge["dst_mac"] == 0
    assert edge["src_port"] == 3
    assert edge["dst_port"] == 0
    assert edge["flag_gateway"] == 0


def test_build_edge_gateway_flags_cover_node_type_combinations():
    cases = [
        (make_satellite("Sat0"), make_satellite("Sat1"), 0),
        (make_ground_station("Gnd0"), make_satellite("Sat1"), 1),
        (make_satellite("Sat0"), make_ground_station("Gnd1"), 2),
        (make_ground_station("Gnd0"), make_ground_station("Gnd1"), 3),
    ]

    for source, target, expected in cases:
        link = make_link(source, target)
        _attach_link_interfaces(link)
        for iface in link.peer_interfaces:
            iface.set_mac_address("00:00:00:00:00:00")

        assert build_edge_from_link(link)["flag_gateway"] == expected


def test_reversed_peer_interfaces_match_endpoint_names():
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link

    sat0 = make_satellite("Sat0")
    sat1 = make_satellite("Sat1")
    iface0 = Interface("Sat0.1")
    iface0.set_ipv4("10.128.0.2")
    iface0.set_mac_address("00:00:00:00:00:01")
    iface1 = Interface("Sat1.0")
    iface1.set_ipv4("10.128.0.3")
    iface1.set_mac_address("00:00:00:00:00:02")
    link = Link(sat0, sat1, distance=100.0, type="InterSatelliteLink")
    # Deliberately reversed order in peer_interfaces.
    link.peer_interfaces = [iface1, iface0]

    edge = build_edge_from_link(link)

    assert edge["src_ip"] == "10.128.0.2"
    assert edge["dst_ip"] == "10.128.0.3"
    assert edge["src_port"] == 1
    assert edge["dst_port"] == 0
