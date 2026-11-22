"""Regression tests for unsupported UT/PoP links in topology sync."""

from types import SimpleNamespace

import pytest

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.node import Node
from satgonetem.models.satellite import Satellite
from satgonetem.services.mixins.topology_sync import TopologySyncMixin


class DummySync(TopologySyncMixin):
    def __init__(self, links_data, links=None):
        self.simulation_manager = SimpleNamespace(get_all_links=lambda: links_data)
        self.links = links or {}
        self.initial_sync = False

    def _build_link_key(self, source, target):
        if source == "UT0":
            raise ValueError("Source and target must be Satellite or GroundStation")
        return frozenset((source, target))

    def _create_link_from_satcom_link(self, sat_com_link):
        from unittest.mock import MagicMock

        return SimpleNamespace(
            is_active=not getattr(sat_com_link, "disabled", False),
            to_add=False,
            to_remove=False,
            satcom_object=sat_com_link,
            sync_distance_from_satcom_and_delay=MagicMock(),
        )


def test_delete_sync_skips_unsupported_links_and_keeps_supported_ones():
    supported = SimpleNamespace(source="Sat0", destination="Gnd0")
    unsupported = SimpleNamespace(source="UT0", destination="Sat1")
    endpoint_less = SimpleNamespace(source=None, destination=None)

    supported_link = SimpleNamespace(to_remove=False)
    stale_link = SimpleNamespace(to_remove=False)
    dummy = DummySync(
        [supported, unsupported, endpoint_less],
        links={
            frozenset(("Sat0", "Gnd0")): supported_link,
            frozenset(("Sat9", "Gnd9")): stale_link,
        },
    )

    dummy._sync_links_to_delete()

    assert supported_link.to_remove is False
    assert stale_link.to_remove is True


def test_main_sync_clears_stale_removal_for_active_reappeared_link():
    from unittest.mock import MagicMock

    existing = SimpleNamespace(
        satcom_object=SimpleNamespace(type="GroundStationLink"),
        is_active=True,
        to_add=False,
        to_remove=True,
        to_delete=True,
        sync_distance_from_satcom_and_delay=MagicMock(),
    )
    satcom_link = SimpleNamespace(source="Sat0", destination="Gnd0", disabled=False)
    dummy = DummySync(
        [satcom_link],
        links={frozenset(("Sat0", "Gnd0")): existing},
    )

    changes = dummy._sync_links()

    assert existing.to_remove is False
    assert existing.to_delete is False
    assert changes["links_to_remove"] == []


def test_main_sync_keeps_removal_for_inactive_link():
    from unittest.mock import MagicMock

    existing = SimpleNamespace(
        satcom_object=SimpleNamespace(type="GroundStationLink"),
        is_active=False,
        to_add=False,
        to_remove=True,
        to_delete=True,
        sync_distance_from_satcom_and_delay=MagicMock(),
    )
    satcom_link = SimpleNamespace(source="Sat0", destination="Gnd0", disabled=True)
    dummy = DummySync(
        [satcom_link],
        links={frozenset(("Sat0", "Gnd0")): existing},
    )

    changes = dummy._sync_links()

    assert existing.to_remove is True
    assert changes["links_to_remove"] == [existing]


def test_main_sync_refreshes_existing_satcom_object():
    from unittest.mock import MagicMock

    old_satcom_link = SimpleNamespace(type="GroundStationLink")
    new_satcom_link = SimpleNamespace(
        source="Sat0",
        destination="Gnd0",
        disabled=False,
    )
    existing = SimpleNamespace(
        satcom_object=old_satcom_link,
        is_active=True,
        to_add=False,
        to_remove=False,
        sync_distance_from_satcom_and_delay=MagicMock(),
    )
    dummy = DummySync(
        [new_satcom_link],
        links={frozenset(("Sat0", "Gnd0")): existing},
    )

    dummy._sync_links()

    assert existing.satcom_object is new_satcom_link


def test_main_sync_marks_link_removed_then_readded_on_active_transition():
    from unittest.mock import MagicMock

    existing = SimpleNamespace(
        satcom_object=SimpleNamespace(type="GroundStationLink"),
        is_active=True,
        to_add=False,
        to_remove=False,
        sync_distance_from_satcom_and_delay=MagicMock(),
    )
    dummy = DummySync(
        [
            SimpleNamespace(
                source="Sat0",
                destination="Gnd0",
                disabled=True,
            )
        ],
        links={frozenset(("Sat0", "Gnd0")): existing},
    )

    dummy._sync_links()
    assert existing.is_active is False
    assert existing.to_remove is True
    assert existing.to_add is False

    dummy.simulation_manager = SimpleNamespace(
        get_all_links=lambda: [
            SimpleNamespace(
                source="Sat0",
                destination="Gnd0",
                disabled=False,
            )
        ]
    )
    dummy._sync_links()
    assert existing.is_active is True
    assert existing.to_remove is False
    assert existing.to_add is True


def test_new_inactive_link_is_not_added_during_initial_sync():
    dummy = DummySync(
        [
            SimpleNamespace(
                source="Sat0",
                destination="Gnd0",
                disabled=True,
            )
        ]
    )
    dummy.initial_sync = True

    dummy._sync_links()

    created = dummy.links[frozenset(("Sat0", "Gnd0"))]
    assert created.is_active is False
    assert created.to_add is False


def test_main_sync_survives_unsupported_links():
    unsupported = SimpleNamespace(source="UT0", destination="Sat1")
    dummy = DummySync([unsupported])

    dummy._sync_links()

    assert dummy.initial_sync is True
    assert dummy.links == {}


def test_main_sync_vectorizes_real_link_distance_and_returns_changes():
    source = Node("Sat0")
    target = Node("Gnd1")
    source.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    target.position = {"latitude": 10.0, "longitude": 0.0, "altitude": 0.0}

    link = Link(
        source=source,
        target=target,
        distance=1.0,
        type="GroundStationLink",
    )
    satcom_link = SimpleNamespace(
        source="Sat0",
        destination="Gnd1",
        disabled=False,
    )
    dummy = DummySync(
        [satcom_link],
        links={frozenset(("Sat0", "Gnd1")): link},
    )

    changes = dummy._sync_links()

    assert link.distance == pytest.approx(1_281_509.0, abs=1.0)
    assert link.to_update is True
    assert changes["links_to_update"] == [link]
    assert changes["links_to_add"] == []
    assert changes["links_to_remove"] == []


class _LinkFactorySync(TopologySyncMixin):
    """Real Link factory harness with the manager attributes it reads."""

    def __init__(self, satellites, ground_stations, **attributes):
        self.satellites = satellites
        self.ground_stations = ground_stations
        self.links = {}
        self.use_budget = False
        self.link_budget_config = None
        self.isl_link_capacity = 1000
        self.gnd_link_capacity = 500
        self.ground_object_link_capacity = 500
        for name, value in attributes.items():
            setattr(self, name, value)


def _satcom_link(link_type="GroundStationLink"):
    return SimpleNamespace(
        source=SimpleNamespace(topology_uniq_id=0),
        destination=SimpleNamespace(topology_uniq_id=1),
        type=link_type,
        disabled=False,
    )


def test_create_link_from_satcom_uses_loss_and_jitter_defaults():
    sat = Satellite("Sat0")
    gnd = GroundStation("Gnd0")
    sat.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    gnd.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    sync = _LinkFactorySync({0: sat}, {1: gnd})

    link = sync._create_link_from_satcom_link(_satcom_link())

    assert link.loss == 0.0
    assert link.jitter == 0


def test_create_link_from_satcom_forwards_configured_impairments():
    sat = Satellite("Sat0")
    gnd = GroundStation("Gnd0")
    sat.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    gnd.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    sync = _LinkFactorySync(
        {0: sat},
        {1: gnd},
        link_loss_percent=2.5,
        link_jitter_ms=15,
    )

    link = sync._create_link_from_satcom_link(_satcom_link())

    assert link.loss == 2.5
    assert link.jitter == 15


def test_create_test_link_forwards_configured_impairments():
    sat = Satellite("Sat0")
    gnd = GroundStation("Gnd0")
    sat.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    gnd.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    sync = _LinkFactorySync(
        {0: sat},
        {1: gnd},
        link_loss_percent=1.25,
        link_jitter_ms=7,
    )

    link = sync._create_test_link(sat, gnd)

    assert link is not None
    assert link.loss == 1.25
    assert link.jitter == 7


def test_create_test_link_uses_zero_impairments_when_unconfigured():
    sat = Satellite("Sat0")
    gnd = GroundStation("Gnd0")
    sat.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    gnd.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    sync = _LinkFactorySync({0: sat}, {1: gnd})

    link = sync._create_test_link(sat, gnd)

    assert link is not None
    assert link.loss == 0.0
    assert link.jitter == 0
