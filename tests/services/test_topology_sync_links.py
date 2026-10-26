"""Regression tests for unsupported UT/PoP links in topology sync."""

from types import SimpleNamespace

from satgonetem.services.mixins.topology_sync import TopologySyncMixin


class DummySync(TopologySyncMixin):
    def __init__(self, links_data, links=None):
        self.simulation_manager = SimpleNamespace(
            get_all_links=lambda: links_data
        )
        self.links = links or {}
        self.initial_sync = False

    def _build_link_key(self, source, target):
        if source == "UT0":
            raise ValueError("Source and target must be Satellite or GroundStation")
        return frozenset((source, target))

    def _create_link_from_satcom_link(self, sat_com_link):
        raise AssertionError("unsupported links must not be converted")


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


def test_main_sync_survives_unsupported_links():
    unsupported = SimpleNamespace(source="UT0", destination="Sat1")
    dummy = DummySync([unsupported])

    dummy._sync_links()

    assert dummy.initial_sync is True
    assert dummy.links == {}
