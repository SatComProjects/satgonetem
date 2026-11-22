"""Tests for the static routing peer interface index."""

import logging
from unittest.mock import MagicMock

import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.satellite import Satellite
from satgonetem.routing.peer_interface_index import PeerInterfaceIndex


def _make_topology(version=None):
    topo = MagicMock()
    if version is not None:
        topo.interface_version = version
    topo.ground_stations = {}
    topo.satellites = {}
    return topo


class TestPeerInterfaceMap:
    def test_maps_peer_ids_from_interface_names(self):
        node = Node("Gnd0")
        iface = Interface(name="Gnd0.7")
        node.interfaces = [iface]

        mapping = PeerInterfaceIndex(_make_topology()).peer_iface_map(node)

        assert mapping == {7: iface}

    def test_skips_names_that_do_not_match_the_expected_pattern(self, caplog):
        node = Node("Gnd0")
        good = Interface(name="Gnd0.1")
        bad = Interface(name="eth0")
        node.interfaces = [good, bad]

        with caplog.at_level(logging.WARNING):
            mapping = PeerInterfaceIndex(_make_topology()).peer_iface_map(node)

        assert mapping == {1: good}
        assert "does not match expected pattern" in caplog.text


class TestPrebuild:
    def test_indexes_ground_stations_and_satellites_by_node_id(self):
        gs = Node("Gnd0")
        gs.id = 3
        gs_iface = Interface(name="Gnd0.4")
        gs.interfaces = [gs_iface]
        sat = Node("Sat1")
        sat.id = 4
        sat_iface = Interface(name="Sat1.3")
        sat.interfaces = [sat_iface]

        topo = _make_topology(version=1)
        topo.ground_stations = {gs.id: gs}
        topo.satellites = {sat.id: sat}

        gs_ifaces, sat_ifaces = PeerInterfaceIndex(topo).prebuild()

        assert gs_ifaces == {3: {4: gs_iface}}
        assert sat_ifaces == {4: {3: sat_iface}}

    def test_reuses_cached_maps_until_interface_version_changes(self):
        gs = Node("Gnd0")
        gs.id = 3
        gs.interfaces = [Interface(name="Gnd0.4")]
        topo = _make_topology(version=1)
        topo.ground_stations = {gs.id: gs}
        index = PeerInterfaceIndex(topo)

        first = index.prebuild()
        second = index.prebuild()

        assert first is second

        topo.interface_version = 2
        third = index.prebuild()

        assert third is not first
        assert third[0][gs.id] is not first[0][gs.id]

    def test_does_not_cache_when_interface_version_is_not_an_int(self):
        topo = _make_topology()
        index = PeerInterfaceIndex(topo)

        first = index.prebuild()
        second = index.prebuild()

        assert second is not first
        assert index.iface_cache is None
        assert index.iface_cache_version is None


class TestResolveInterface:
    def test_prefers_the_prebuilt_map(self):
        node = Node("Gnd0")
        iface = Interface(name="Gnd0.5")
        node.interfaces = [iface]

        resolved = PeerInterfaceIndex(_make_topology()).resolve_interface(
            node, node.id, 5, {5: iface}
        )

        assert resolved is iface

    def test_falls_back_to_a_satellite_name_lookup(self):
        node = Node("Gnd0")
        node.id = 0
        iface = Interface(name="Gnd0.5")
        node.interfaces = [iface]
        peer = Satellite("Sat5")
        peer.id = 5
        topo = _make_topology()
        topo.satellites = {5: peer}

        resolved = PeerInterfaceIndex(topo).resolve_interface(node, 0, 5, None)

        assert resolved is iface

    def test_falls_back_to_a_ground_station_name_lookup(self):
        node = Node("Sat0")
        node.id = 0
        iface = Interface(name="Sat0.7")
        node.interfaces = [iface]
        peer = GroundStation("Gnd7")
        peer.id = 7
        topo = _make_topology()
        topo.ground_stations = {7: peer}

        resolved = PeerInterfaceIndex(topo).resolve_interface(node, 0, 7, None)

        assert resolved is iface

    def test_unexpected_satellite_container_value_raises_type_error(self):
        topo = _make_topology()
        topo.satellites = {5: object()}

        with pytest.raises(TypeError, match="Expected Satellite"):
            PeerInterfaceIndex(topo).resolve_interface(Node("Gnd0"), 0, 5, None)

    def test_unexpected_ground_station_container_value_raises_type_error(self):
        topo = _make_topology()
        topo.ground_stations = {7: object()}

        with pytest.raises(TypeError, match="Expected GroundStation"):
            PeerInterfaceIndex(topo).resolve_interface(Node("Gnd0"), 0, 7, None)

    def test_missing_interface_raises_value_error(self):
        with pytest.raises(ValueError, match="Could not resolve interface"):
            PeerInterfaceIndex(_make_topology()).resolve_interface(
                Node("Gnd0"), 0, 5, None
            )
