"""Direct contract tests for the extracted satcom link factory."""

from types import SimpleNamespace

import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.node import Node
from satgonetem.models.satellite import Satellite
from satgonetem.services.satcom_link_factory import (
    create_link_from_satcom_link,
    create_test_link,
    refresh_link_distances,
)


def _manager(satellites=None, ground_stations=None, **attributes):
    values = {
        "satellites": satellites or {},
        "ground_stations": ground_stations or {},
        "links": {},
        "use_budget": False,
        "link_budget_config": None,
        "isl_link_capacity": 1000,
        "gnd_link_capacity": 500,
        "ground_object_link_capacity": 250,
    }
    values.update(attributes)
    return SimpleNamespace(**values)


def _satcom_link(link_type="GroundStationLink", *, source_uid=0, destination_uid=1):
    return SimpleNamespace(
        source=SimpleNamespace(topology_uniq_id=source_uid),
        destination=SimpleNamespace(topology_uniq_id=destination_uid),
        type=link_type,
        disabled=False,
    )


def _positioned_node(node, altitude):
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": altitude}
    return node


class TestCreateLinkFromSatcomLink:
    def test_resolves_nodes_and_capacities(self):
        sat = _positioned_node(Satellite("Sat0"), 550.0)
        gnd = _positioned_node(GroundStation("Gnd0"), 0.0)
        manager = _manager({0: sat}, {1: gnd})

        link = create_link_from_satcom_link(manager, _satcom_link())

        assert link.source is sat
        assert link.target is gnd
        assert link.type == "GroundStationLink"
        assert link.default_capacity_kbps == 500
        assert link.is_active is True
        assert link.distance == pytest.approx(550_000.0, abs=1.0)

    def test_isl_uses_isl_capacity(self):
        sat_a = _positioned_node(Satellite("Sat0"), 550.0)
        sat_b = _positioned_node(Satellite("Sat1"), 550.0)
        manager = _manager({0: sat_a, 1: sat_b})

        link = create_link_from_satcom_link(manager, _satcom_link("InterSatelliteLink"))

        assert link.default_capacity_kbps == 1000

    def test_ground_object_link_uses_its_own_capacity(self):
        sat_a = _positioned_node(Satellite("Sat0"), 550.0)
        sat_b = _positioned_node(Satellite("Sat1"), 550.0)
        manager = _manager({0: sat_a, 1: sat_b})

        link = create_link_from_satcom_link(manager, _satcom_link("GroundObjectLink"))

        assert link.default_capacity_kbps == 250

    def test_raises_when_the_source_is_unknown(self):
        manager = _manager()

        with pytest.raises(ValueError, match="Link source not found"):
            create_link_from_satcom_link(manager, _satcom_link())

    def test_raises_when_a_satellite_slot_holds_another_type(self):
        manager = _manager({0: object()}, {1: GroundStation("Gnd0")})

        with pytest.raises(TypeError, match="Expected Satellite"):
            create_link_from_satcom_link(manager, _satcom_link())


class TestCreateTestLink:
    def test_creates_and_registers_a_test_link(self):
        sat = _positioned_node(Satellite("Sat0"), 550.0)
        gnd = _positioned_node(GroundStation("Gnd0"), 0.0)
        manager = _manager()

        link = create_test_link(manager, sat, gnd)

        assert link is not None
        assert link.to_add is True
        assert link.type == "GroundStationLink"
        assert manager.links[frozenset(("Sat0", "Gnd0"))] is link

    def test_returns_none_for_a_self_link(self):
        sat = _positioned_node(Satellite("Sat0"), 550.0)
        manager = _manager()

        assert create_test_link(manager, sat, sat) is None
        assert manager.links == {}

    def test_returns_none_when_the_link_already_exists(self):
        sat = _positioned_node(Satellite("Sat0"), 550.0)
        gnd = _positioned_node(GroundStation("Gnd0"), 0.0)
        manager = _manager(links={frozenset(("Sat0", "Gnd0")): object()})

        assert create_test_link(manager, sat, gnd) is None


class TestRefreshLinkDistances:
    def test_empty_list_is_a_noop(self):
        assert refresh_link_distances([]) == 0

    def test_updates_a_real_link_distance(self):
        source = _positioned_node(Node("Sat0"), 550.0)
        target = Node("Gnd1")
        target.position = {"latitude": 10.0, "longitude": 0.0, "altitude": 0.0}
        link = Link(
            source=source, target=target, distance=1.0, type="GroundStationLink"
        )

        errors = refresh_link_distances([link])

        assert errors == 0
        assert link.distance == pytest.approx(1_281_509.0, abs=1.0)

    def test_malformed_positions_fall_back_per_link(self):
        source = Node("Sat0")
        source.position = {"latitude": 0.0, "longitude": 0.0}
        target = _positioned_node(Node("Gnd1"), 0.0)
        link = Link(
            source=source, target=target, distance=1.0, type="GroundStationLink"
        )

        assert refresh_link_distances([link]) == 1
