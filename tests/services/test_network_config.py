"""Tests for the link queue-length settings of NetworkConfig."""

import json
from dataclasses import fields
from pathlib import Path

import pytest

from satgonetem.addressing import AddressPlanConfigurationError
from satgonetem.services.topology_satcom import (
    MAX_LINK_QUEUE_PACKETS,
    NetworkConfig,
    TopologyManager,
)

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)
SAMPLE_TOPOLOGIES = sorted(
    str(path)
    for path in (Path(__file__).resolve().parents[2] / "topology_files").glob(
        "*_topology.json"
    )
)

IPV6_CONFIG_FIELDS = {
    "ipv6_enabled",
    "ipv6_ground_station_pool",
    "ipv6_satellite_pool",
    "ipv6_user_terminal_pool",
    "ipv6_ground_station_prefix_len",
    "ipv6_satellite_prefix_len",
    "ipv6_user_terminal_prefix_len",
}


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


class TestUpdateTime:
    @pytest.mark.parametrize("value", [0, -1, -60])
    def test_rejects_non_positive_update_time(self, value):
        with pytest.raises(ValueError, match="update_time"):
            NetworkConfig(update_time=value)

    def test_accepts_positive_update_time(self):
        assert NetworkConfig(update_time=1).update_time == 1


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
            network_config=NetworkConfig(link_netem_limit=2048, link_txqueuelen=4096),
        )

        payload = json.loads(path.read_text())["network_config"]
        assert payload["link_netem_limit"] == 2048
        assert payload["link_txqueuelen"] == 4096

    def test_roundtrip_restores_queue_lengths(self, tmp_path, topology):
        path = tmp_path / "topology.json"
        topology.to_file(
            str(path),
            network_config=NetworkConfig(link_netem_limit=2048, link_txqueuelen=4096),
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


class TestControllerInterfacePattern:
    def test_default_pattern_selects_controller_interface_index(self):
        config = NetworkConfig()

        assert config.controller_node_iface_pattern == "eth{node_id}"

    def test_accepts_custom_pattern_with_node_id_placeholder(self):
        config = NetworkConfig(controller_node_iface_pattern="eth5{node_id}")

        assert config.controller_node_iface_pattern.format(node_id=50007) == "eth550007"

    @pytest.mark.parametrize(
        "pattern",
        ["eth0", "mgmt{iface}", "mgmt{node_id}", "mgmt{node_id:d}"],
    )
    def test_rejects_invalid_patterns(self, pattern):
        with pytest.raises(ValueError, match="controller_node_iface_pattern"):
            NetworkConfig(controller_node_iface_pattern=pattern)


class TestSampleTopologies:
    def test_sample_topologies_are_discovered(self):
        assert SAMPLE_TOPOLOGIES

    @pytest.mark.parametrize("path", SAMPLE_TOPOLOGIES)
    def test_pins_every_network_config_field(self, path):
        payload = json.loads(Path(path).read_text())["network_config"]
        known_fields = {field.name for field in fields(NetworkConfig)}

        # Sample payloads may predate additive fields; from_mapping rejects
        # unknown keys while field defaults fill the gaps.
        assert set(payload) <= known_fields

        config = NetworkConfig.from_mapping(payload)
        assert isinstance(config.ipv6_enabled, bool)
        assert 0 <= config.link_netem_limit <= MAX_LINK_QUEUE_PACKETS
        assert 0 <= config.link_txqueuelen <= MAX_LINK_QUEUE_PACKETS

    def test_legacy_payload_without_ipv6_fields_defaults_disabled(self):
        current = NetworkConfig()
        payload = {
            field.name: getattr(current, field.name)
            for field in fields(NetworkConfig)
            if field.name not in IPV6_CONFIG_FIELDS
        }

        config = NetworkConfig.from_mapping(payload)

        assert config.ipv6_enabled is False
        assert config.ipv6_ground_station_pool == "fd00:1::/40"
        assert config.ipv6_satellite_pool == "fd00:2::/40"
        assert config.ipv6_user_terminal_pool == "fd00:3::/40"


class TestNetworkConfigSingleSource:
    def test_custom_controller_fields_survive_roundtrip(self, topology, tmp_path):
        config = NetworkConfig(
            project_name="custom",
            controller_veth_host="mgmt0",
            controller_veth_peer="mgmt1",
            controller_host_ip="10.255.0.1",
            controller_subnet_prefix=24,
            controller_bridge_name="MgmtBridge",
            controller_node_iface_pattern="eth5{node_id}",
        )
        path = tmp_path / "custom.json"

        topology.to_file(str(path), config)
        reloaded = TopologyManager.from_file(str(path))

        assert reloaded.controller_veth_host == "mgmt0"
        assert reloaded.controller_veth_peer == "mgmt1"
        assert reloaded.controller_host_ip == "10.255.0.1"
        assert reloaded.controller_subnet_prefix == 24
        assert reloaded.controller_bridge_name == "MgmtBridge"
        assert reloaded.controller_node_iface_pattern == "eth5{node_id}"


class TestIpv6Config:
    def test_defaults_disable_ipv6_and_use_ula_pools(self):
        config = NetworkConfig()

        assert config.ipv6_enabled is False
        assert config.ipv6_ground_station_pool == "fd00:1::/40"
        assert config.ipv6_satellite_pool == "fd00:2::/40"
        assert config.ipv6_user_terminal_pool == "fd00:3::/40"
        assert config.ipv6_ground_station_prefix_len == 64
        assert config.ipv6_satellite_prefix_len == 64
        assert config.ipv6_user_terminal_prefix_len == 64

    def test_enabled_config_accepts_valid_pools(self):
        config = NetworkConfig(ipv6_enabled=True)

        assert config.ipv6_enabled is True

    @pytest.mark.parametrize("value", [-1, 129])
    def test_rejects_out_of_range_prefix_lengths(self, value):
        for name in (
            "ipv6_ground_station_prefix_len",
            "ipv6_satellite_prefix_len",
            "ipv6_user_terminal_prefix_len",
        ):
            with pytest.raises(ValueError, match=name):
                NetworkConfig(**{name: value})

    @pytest.mark.parametrize("value", [1.5, "64", True])
    def test_rejects_non_integer_prefix_lengths(self, value):
        with pytest.raises(TypeError, match="ipv6_satellite_prefix_len"):
            NetworkConfig(ipv6_satellite_prefix_len=value)

    @pytest.mark.parametrize("value", [1, "yes", None])
    def test_rejects_non_boolean_enabled_flag(self, value):
        with pytest.raises(TypeError, match="ipv6_enabled"):
            NetworkConfig(ipv6_enabled=value)

    @pytest.mark.parametrize(
        "bad_pool",
        [
            "10.0.0.0/8",  # IPv4
            "ff00::/8",  # multicast
            "2001:4860::/32",  # globally routable
        ],
    )
    def test_enabled_config_rejects_invalid_pools(self, bad_pool):
        with pytest.raises(AddressPlanConfigurationError):
            NetworkConfig(ipv6_enabled=True, ipv6_satellite_pool=bad_pool)

    def test_enabled_config_rejects_overlapping_pools(self):
        with pytest.raises(AddressPlanConfigurationError, match="overlaps"):
            NetworkConfig(
                ipv6_enabled=True,
                ipv6_ground_station_pool="fd00:2::/39",
                ipv6_satellite_pool="fd00:2::/40",
            )


class TestNetworkConfigFromMapping:
    def test_merges_values_over_a_base_config(self):
        base = NetworkConfig(
            ipv6_enabled=True,
            link_netem_limit=2048,
            satellite_pool="10.128.0.0/9",
        )

        config = NetworkConfig.from_mapping(
            {"link_netem_limit": 4096, "ipv6_satellite_pool": "fd00:9::/40"},
            base=base,
        )

        assert config.link_netem_limit == 4096
        assert config.satellite_pool == "10.128.0.0/9"
        assert config.ipv6_satellite_pool == "fd00:9::/40"
        assert config.ipv6_enabled is True
        # The base object is not mutated.
        assert base.link_netem_limit == 2048
        assert base.ipv6_satellite_pool == "fd00:2::/40"

    def test_without_base_uses_defaults(self):
        config = NetworkConfig.from_mapping({"ipv6_enabled": True})

        assert config.ipv6_enabled is True
        assert config.satellite_pool == "10.128.0.0/9"

    def test_rejects_unknown_fields(self):
        with pytest.raises(ValueError, match="unknown_field"):
            NetworkConfig.from_mapping({"unknown_field": 1})
        with pytest.raises(ValueError, match="unknown_field"):
            NetworkConfig.from_mapping({"unknown_field": 1}, base=NetworkConfig())
        # Mixed key types must still produce a readable error, not a TypeError.
        with pytest.raises(ValueError, match="1"):
            NetworkConfig.from_mapping({1: True})

    def test_rejects_non_mapping_values(self):
        with pytest.raises(TypeError, match="mapping"):
            NetworkConfig.from_mapping([("ipv6_enabled", True)])  # type: ignore[arg-type]


class TestIpv6Persistence:
    def test_to_file_persists_ipv6_fields(self, tmp_path, topology):
        path = tmp_path / "ipv6.json"
        config = NetworkConfig(
            ipv6_enabled=True,
            ipv6_ground_station_pool="fd00:11::/40",
            ipv6_satellite_prefix_len=64,
        )

        topology.to_file(str(path), network_config=config)

        payload = json.loads(path.read_text())["network_config"]
        assert payload["ipv6_enabled"] is True
        assert payload["ipv6_ground_station_pool"] == "fd00:11::/40"
        assert payload["ipv6_satellite_prefix_len"] == 64

    def test_roundtrip_restores_ipv6_and_builds_plan(self, tmp_path, topology):
        path = tmp_path / "ipv6.json"
        topology.to_file(str(path), network_config=NetworkConfig(ipv6_enabled=True))

        reloaded = TopologyManager.from_file(str(path))

        assert reloaded.ipv6_enabled is True
        assert reloaded.ipv6_satellite_pool == "fd00:2::/40"
        assert reloaded.address_plan_v6 is not None
        for node in list(reloaded.get_satellites()) + list(
            reloaded.get_ground_stations()
        ):
            assert node.address_block_v6 is not None
            assert node.address_block_v6.prefixlen == 64
            assert node.loopback.ipv6_prefix_len == 128

    def test_from_file_without_override_keeps_persisted_setting(
        self, tmp_path, topology
    ):
        path = tmp_path / "ipv6_disabled.json"
        topology.to_file(str(path), network_config=NetworkConfig(ipv6_enabled=False))

        reloaded = TopologyManager.from_file(str(path))

        assert reloaded.ipv6_enabled is False
        assert reloaded.address_plan_v6 is None

    def test_from_file_override_enables_ipv6_before_building_the_plan(
        self, tmp_path, topology
    ):
        path = tmp_path / "ipv6_disabled.json"
        topology.to_file(str(path), network_config=NetworkConfig(ipv6_enabled=False))

        reloaded = TopologyManager.from_file(
            str(path), network_config=NetworkConfig(ipv6_enabled=True)
        )

        assert reloaded.ipv6_enabled is True
        assert reloaded.address_plan_v6 is not None
        assert all(
            node.address_block_v6 is not None
            for node in list(reloaded.get_satellites())
            + list(reloaded.get_ground_stations())
        )

    def test_from_file_override_wins_wholesale_over_the_persisted_config(
        self, tmp_path, topology
    ):
        path = tmp_path / "persisted.json"
        topology.to_file(
            str(path),
            network_config=NetworkConfig(
                ipv6_enabled=False,
                link_netem_limit=4096,
                satellite_pool="10.128.0.0/9",
            ),
        )

        reloaded = TopologyManager.from_file(
            str(path), network_config=NetworkConfig(ipv6_enabled=True)
        )

        # The explicit config replaces the persisted one, including fields it
        # does not explicitly mention.
        assert reloaded.ipv6_enabled is True
        assert reloaded.link_netem_limit == 1000
        assert reloaded.address_plan_v6 is not None

    def test_legacy_persisted_payload_defaults_to_ipv6_disabled(self, topology):
        # The module fixture loads a legacy file with no IPv6 keys.
        assert topology.ipv6_enabled is False
        assert topology.address_plan_v6 is None
