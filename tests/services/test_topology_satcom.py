"""Tests for TopologyManager public methods."""

import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.link_budget.config import AntennaConfig, LinkBudgetConfig
from satgonetem.models.node import Node
from satgonetem.models.link import Link


@pytest.fixture
def mock_tm():
    """Return a TopologyManager with a mocked __init__ and basic state."""
    with patch.object(TopologyManager, "__init__", lambda self, **kw: None):
        tm = TopologyManager.__new__(TopologyManager)
        tm.links = {}
        tm.link_budget_config = None
        return tm


@pytest.fixture
def sat_node():
    node = Node("Sat0")
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 550.0}
    return node


@pytest.fixture
def gnd_node():
    node = Node("Gnd0")
    node.position = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    return node


@pytest.fixture
def topology_manager():
    """Return a fully initialised TopologyManager from the test topology."""
    return TopologyManager.from_file("topology_files/test_topology.json")


class TestSetLinkBudgetConfig:
    def test_sets_config_on_manager(self, mock_tm):
        cfg = LinkBudgetConfig(downlink_freq_ghz=20.0, uplink_freq_ghz=15.0)
        mock_tm.set_link_budget_config(cfg)
        assert mock_tm.link_budget_config is cfg

    def test_propagates_to_existing_links(self, mock_tm, sat_node, gnd_node):
        link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="GroundStationLink",
            default_capacity_kbps=500,
            use_budget=False,
        )
        mock_tm.links[frozenset(["Sat0", "Gnd0"])] = link

        cfg = LinkBudgetConfig(downlink_freq_ghz=20.0, uplink_freq_ghz=15.0)
        mock_tm.set_link_budget_config(cfg)

        assert link.link_budget_config is cfg


class TestSetAntenna:
    def test_creates_antenna_on_all_nodes(self, mock_tm, sat_node, gnd_node):
        cfg = AntennaConfig(diameter=1.0, efficiency=0.6, sspa_output_power_db=40.0)
        mock_tm.set_antenna([sat_node, gnd_node], cfg)

        assert sat_node.antenna is not None
        assert gnd_node.antenna is not None
        assert sat_node.antenna.diameter == 1.0
        assert gnd_node.antenna.diameter == 1.0
        assert sat_node.antenna.efficiency == 0.6
        assert sat_node.antenna.sspa_output_power_db == 40.0

    def test_nodes_share_same_antenna_instance(self, mock_tm, sat_node, gnd_node):
        cfg = AntennaConfig(diameter=2.0)
        mock_tm.set_antenna([sat_node, gnd_node], cfg)

        # to_antenna creates a single instance which is assigned to all nodes
        assert sat_node.antenna is gnd_node.antenna


class TestSimulateLinkFailure:
    """Tests for TopologyManager.simulate_link_failure()."""

    TEST_TOPOLOGY_PATH = "topology_files/test_topology.json"

    def _links_by_type(self, topology_manager):
        """Return available ISL and ground-station links from the manager."""
        links = list(topology_manager.links.values())
        isl = [
            link
            for link in links
            if getattr(link.satcom_object, "type", None) == "InterSatelliteLink"
        ]
        gsl = [
            link
            for link in links
            if getattr(link.satcom_object, "type", None) == "GroundStationLink"
        ]
        return isl, gsl

    def test_removes_random_links_from_multiple_topology_managers(
        self, topology_manager
    ):
        """Create several TopologyManager instances and remove random links."""
        random.seed(42)

        managers = [
            topology_manager,
            TopologyManager.from_file(self.TEST_TOPOLOGY_PATH),
            TopologyManager.from_file(self.TEST_TOPOLOGY_PATH),
        ]

        for manager in managers:
            isl_links, gsl_links = self._links_by_type(manager)
            assert isl_links, "Expected at least one inter-satellite link"
            assert gsl_links, "Expected at least one ground-station link"

            links_to_fail = []
            links_to_fail.extend(random.sample(isl_links, min(3, len(isl_links))))
            links_to_fail.extend(random.sample(gsl_links, min(2, len(gsl_links))))

            before = manager.simulation_manager.get_all_links()
            manager.simulate_link_failure(links_to_fail)
            after = manager.simulation_manager.get_all_links()

            assert len(after) == len(before) - len(links_to_fail)
            for link in links_to_fail:
                assert link.satcom_object not in after

    def test_accepts_single_link(self, topology_manager):
        """simulate_link_failure should accept a single Link as well as a list."""
        isl_links, _ = self._links_by_type(topology_manager)
        assert isl_links

        link = random.choice(isl_links)
        before = len(topology_manager.simulation_manager.get_all_links())

        topology_manager.simulate_link_failure(link)

        after = topology_manager.simulation_manager.get_all_links()
        assert len(after) == before - 1
        assert link.satcom_object not in after

    def test_raises_on_invalid_link_type(self, mock_tm, sat_node, gnd_node):
        """A link whose satcom_object has an unsupported type must raise."""
        bad_link = Link(
            source=sat_node,
            target=gnd_node,
            distance=550_000.0,
            type="UnknownLink",
        )
        bad_link.satcom_object = SimpleNamespace(type="UnsupportedLink")

        with pytest.raises(AttributeError):
            mock_tm.simulate_link_failure(bad_link)

    def test_logs_and_skips_user_terminal_link_when_available(self, topology_manager):
        """If user-terminal links exist, they are removed via the correct API."""
        links = list(topology_manager.links.values())
        utl = [
            link
            for link in links
            if getattr(link.satcom_object, "type", None) == "UserTerminalLink"
        ]

        if not utl:
            pytest.skip("No user-terminal links in the test topology")

        link = utl[0]
        before = len(topology_manager.simulation_manager.get_all_links())
        topology_manager.simulate_link_failure(link)
        after = topology_manager.simulation_manager.get_all_links()

        assert len(after) == before - 1
        assert link.satcom_object not in after


class TestGetLinks:
    """Tests for TopologyManager.get_links()."""

    def test_returns_all_links(self, topology_manager):
        """get_links() returns the same objects stored in self.links."""
        links = topology_manager.get_links()

        assert isinstance(links, list)
        assert len(links) == len(topology_manager.links)
        assert links == list(topology_manager.links.values())
        assert all(isinstance(link, Link) for link in links)

    def test_returns_empty_list_when_no_links(self, mock_tm):
        """get_links() returns an empty list when the manager has no links."""
        assert mock_tm.get_links() == []
