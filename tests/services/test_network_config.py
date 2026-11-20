"""Tests for the link queue-length settings of NetworkConfig."""

import json
from dataclasses import fields
from pathlib import Path

import pytest

from satgonetem.services.topology_satcom import (
    MAX_LINK_QUEUE_PACKETS,
    NetworkConfig,
    TopologyManager,
)

TEST_TOPOLOGY = "topology_files/test_topology.json"
SAMPLE_TOPOLOGIES = sorted(
    str(path) for path in Path("topology_files").glob("*_topology.json")
)


@pytest.fixture(scope="module")
def topology():
    return TopologyManager.from_file(TEST_TOPOLOGY)


class TestQueueLengthFields:
    def test_defaults_keep_current_queue_length(self):
        config = NetworkConfig()

        assert config.link_netem_limit == 1000
        assert config.link_txqueuelen == 1000

    def test_zero_means_server_default(self):
        config = NetworkConfig(link_netem_limit=0, link_txqueuelen=0)

        assert config.link_netem_limit == 0
        assert config.link_txqueuelen == 0

    def test_accepts_upper_bound(self):
        config = NetworkConfig(
            link_netem_limit=MAX_LINK_QUEUE_PACKETS,
            link_txqueuelen=MAX_LINK_QUEUE_PACKETS,
        )

        assert config.link_netem_limit == MAX_LINK_QUEUE_PACKETS
        assert config.link_txqueuelen == MAX_LINK_QUEUE_PACKETS

    @pytest.mark.parametrize("value", [-1, MAX_LINK_QUEUE_PACKETS + 1])
    def test_rejects_out_of_range_values(self, value):
        with pytest.raises(ValueError, match="link_netem_limit"):
            NetworkConfig(link_netem_limit=value)
        with pytest.raises(ValueError, match="link_txqueuelen"):
            NetworkConfig(link_txqueuelen=value)

    @pytest.mark.parametrize("value", [1.5, "1000", True])
    def test_rejects_non_integer_values(self, value):
        with pytest.raises(TypeError, match="link_netem_limit"):
            NetworkConfig(link_netem_limit=value)
        with pytest.raises(TypeError, match="link_txqueuelen"):
            NetworkConfig(link_txqueuelen=value)


class TestApplyNetworkConfig:
    @pytest.fixture
    def manager(self):
        tm = TopologyManager.__new__(TopologyManager)
        tm.simulation_manager = None
        return tm

    def test_copies_queue_lengths(self, manager):
        manager._apply_network_config(
            NetworkConfig(link_netem_limit=2048, link_txqueuelen=4096)
        )

        assert manager.link_netem_limit == 2048
        assert manager.link_txqueuelen == 4096

    def test_defaults_are_applied(self, manager):
        manager._apply_network_config(NetworkConfig())

        assert manager.link_netem_limit == 1000
        assert manager.link_txqueuelen == 1000


class TestPersistence:
    def test_to_file_persists_queue_lengths(self, tmp_path, topology):
        path = tmp_path / "topology.json"
        topology.to_file(
            str(path),
            network_config=NetworkConfig(
                link_netem_limit=2048, link_txqueuelen=4096
            ),
        )

        payload = json.loads(path.read_text())["network_config"]
        assert payload["link_netem_limit"] == 2048
        assert payload["link_txqueuelen"] == 4096

    def test_roundtrip_restores_queue_lengths(self, tmp_path, topology):
        path = tmp_path / "topology.json"
        topology.to_file(
            str(path),
            network_config=NetworkConfig(
                link_netem_limit=2048, link_txqueuelen=4096
            ),
        )

        reloaded = TopologyManager.from_file(str(path))

        assert reloaded.link_netem_limit == 2048
        assert reloaded.link_txqueuelen == 4096

    def test_legacy_payload_without_queue_lengths_uses_defaults(self):
        payload = {
            "project_name": "Constellation",
            "update_time": 1,
            "gnd_link_capacity": 100_000,
            "isl_link_capacity": 100_000,
            "protocol": "ipv4",
            "routing": "static",
        }

        config = NetworkConfig(**payload)

        assert config.link_netem_limit == 1000
        assert config.link_txqueuelen == 1000


class TestSampleTopologies:
    def test_sample_topologies_are_discovered(self):
        assert SAMPLE_TOPOLOGIES

    @pytest.mark.parametrize("path", SAMPLE_TOPOLOGIES)
    def test_pins_every_network_config_field(self, path):
        payload = json.loads(Path(path).read_text())["network_config"]

        assert set(payload) == {field.name for field in fields(NetworkConfig)}

        config = NetworkConfig(**payload)
        assert 0 <= config.link_netem_limit <= MAX_LINK_QUEUE_PACKETS
        assert 0 <= config.link_txqueuelen <= MAX_LINK_QUEUE_PACKETS
