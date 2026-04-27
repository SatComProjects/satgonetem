"""Tests for TopologyManager public methods."""

import pytest
from unittest.mock import MagicMock, patch

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
