"""Tests for default link-impairment settings in NetworkConfig."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)


def test_defaults_are_zero():
    config = NetworkConfig()
    assert config.link_loss_percent == 0.0
    assert config.link_jitter_ms == 0


def test_valid_values_are_accepted():
    config = NetworkConfig(link_loss_percent=2.5, link_jitter_ms=10)
    assert config.link_loss_percent == 2.5
    assert config.link_jitter_ms == 10


@pytest.mark.parametrize("value", [-0.1, 100.1, float("nan"), float("inf")])
def test_invalid_loss_is_rejected(value):
    with pytest.raises(ValueError, match="link_loss_percent"):
        NetworkConfig(link_loss_percent=value)


@pytest.mark.parametrize("value", [True, "1.0", None])
def test_non_numeric_loss_is_rejected(value):
    with pytest.raises(TypeError, match="link_loss_percent"):
        NetworkConfig(link_loss_percent=value)


@pytest.mark.parametrize("value", [-1, 1.5, True, "1"])
def test_invalid_jitter_is_rejected(value):
    with pytest.raises((TypeError, ValueError), match="link_jitter_ms"):
        NetworkConfig(link_jitter_ms=value)


def test_from_mapping_merges_impairment_defaults():
    config = NetworkConfig.from_mapping(
        {"link_loss_percent": 1.25},
        base=NetworkConfig(link_jitter_ms=7),
    )
    assert config.link_loss_percent == 1.25
    assert config.link_jitter_ms == 7


def test_to_file_persists_impairment_defaults(tmp_path):
    topology = TopologyManager.from_file(TEST_TOPOLOGY)
    path = tmp_path / "topology.json"
    topology.to_file(
        str(path),
        NetworkConfig(link_loss_percent=3.0, link_jitter_ms=12),
    )

    payload = json.loads(path.read_text())["network_config"]
    assert payload["link_loss_percent"] == 3.0
    assert payload["link_jitter_ms"] == 12


def test_every_sample_topology_pins_impairment_fields():
    for path in (Path(TEST_TOPOLOGY).parent).glob("*_topology.json"):
        payload = json.loads(path.read_text())["network_config"]
        known = {field.name for field in fields(NetworkConfig)}
        assert set(payload) == known
