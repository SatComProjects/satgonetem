"""Characterization tests for TrafficTestingMixin flow fan-out."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from satgonetem.services.mixins.traffic_testing import TrafficTestingMixin
from satgonetem.traffic import Hping3Config, Iperf3Config, PingConfig
from tests.support import DummyTopology

pytestmark = pytest.mark.unit

_MIXIN_MODULE = "satgonetem.services.mixins.traffic_testing"


class _TrafficHost(TrafficTestingMixin, DummyTopology):
    """Lightweight topology host exposing TrafficTestingMixin."""


class _RecordingFlow:
    """Flow double recording construction arguments and start() calls."""

    instances: list[_RecordingFlow] = []

    def __init__(self, source, destination, config) -> None:
        self.source = source
        self.destination = destination
        self.config = config
        self.start_count = 0
        type(self).instances.append(self)

    def start(self) -> None:
        self.start_count += 1


@pytest.fixture(autouse=True)
def _reset_recorded_flows():
    _RecordingFlow.instances.clear()
    yield
    _RecordingFlow.instances.clear()


@pytest.fixture
def traffic_host() -> _TrafficHost:
    host = _TrafficHost()
    host.add_satellite("Sat0")
    host.add_satellite("Sat1")
    host.add_ground_station("Gnd5")
    host.add_ground_station("Gnd6")
    host.add_ground_station("Gnd7")
    return host


# ----------------------------------------------------------------------
# ping
# ----------------------------------------------------------------------


def test_ping_single_nodes_uses_default_config_and_starts_once(
    traffic_host: _TrafficHost,
) -> None:
    source = traffic_host.satellites[0]
    destination = traffic_host.ground_stations[5]

    with patch(f"{_MIXIN_MODULE}.PingFlow", _RecordingFlow):
        flows = traffic_host.ping(source, destination)

    assert len(flows) == 1
    flow = flows[0]
    assert isinstance(flow, _RecordingFlow)
    assert flow.source is source
    assert flow.destination is destination
    assert isinstance(flow.config, PingConfig)
    assert (
        flow.config.count,
        flow.config.timeout_sec,
        flow.config.interval_sec,
        flow.config.packet_size,
        flow.config.preload,
    ) == (5, 0.5, 0.2, 56, 2)
    assert flow.start_count == 1
    assert _RecordingFlow.instances == flows
    assert traffic_host._active_flows == flows


def test_ping_lists_create_source_major_cartesian_product(
    traffic_host: _TrafficHost,
) -> None:
    sources = [traffic_host.satellites[0], traffic_host.satellites[1]]
    destinations = [
        traffic_host.ground_stations[5],
        traffic_host.ground_stations[6],
        traffic_host.ground_stations[7],
    ]

    with patch(f"{_MIXIN_MODULE}.PingFlow", _RecordingFlow):
        flows = traffic_host.ping(sources, destinations)

    assert len(flows) == 6
    assert [(flow.source, flow.destination) for flow in flows] == [
        (source, destination) for source in sources for destination in destinations
    ]
    assert all(flow.start_count == 1 for flow in flows)
    assert all(flow.config is flows[0].config for flow in flows)
    assert _RecordingFlow.instances == flows
    assert traffic_host._active_flows == flows


def test_ping_reuses_the_explicit_config_for_every_flow(
    traffic_host: _TrafficHost,
) -> None:
    config = PingConfig(count=2, interval_sec=0.5)

    with patch(f"{_MIXIN_MODULE}.PingFlow", _RecordingFlow):
        flows = traffic_host.ping(
            [traffic_host.satellites[0], traffic_host.satellites[1]],
            traffic_host.ground_stations[5],
            config=config,
        )

    assert len(flows) == 2
    assert all(flow.config is config for flow in flows)
    assert all(flow.start_count == 1 for flow in flows)


# ----------------------------------------------------------------------
# hping3
# ----------------------------------------------------------------------


def test_run_hping3_single_nodes_uses_default_config(
    traffic_host: _TrafficHost,
) -> None:
    source = traffic_host.satellites[0]
    destination = traffic_host.ground_stations[5]

    with patch(f"{_MIXIN_MODULE}.Hping3Flow", _RecordingFlow):
        flows = traffic_host.run_hping3(source, destination)

    assert len(flows) == 1
    flow = flows[0]
    assert isinstance(flow, _RecordingFlow)
    assert (flow.source, flow.destination) == (source, destination)
    assert isinstance(flow.config, Hping3Config)
    assert (
        flow.config.proto,
        flow.config.dport,
        flow.config.count,
        flow.config.size,
        flow.config.rate_type,
    ) == ("tcp", 0, 100, 0, "interval")
    assert flow.config.flags == []
    assert flow.start_count == 1
    assert traffic_host._active_flows == flows


def test_run_hping3_lists_create_cartesian_product_with_explicit_config(
    traffic_host: _TrafficHost,
) -> None:
    sources = [traffic_host.satellites[0], traffic_host.satellites[1]]
    destinations = [
        traffic_host.ground_stations[5],
        traffic_host.ground_stations[6],
    ]
    config = Hping3Config(proto="udp", dport=53, count=3)

    with patch(f"{_MIXIN_MODULE}.Hping3Flow", _RecordingFlow):
        flows = traffic_host.run_hping3(sources, destinations, config)

    assert len(flows) == 4
    assert [(flow.source, flow.destination) for flow in flows] == [
        (source, destination) for source in sources for destination in destinations
    ]
    assert all(flow.config is config for flow in flows)
    assert all(flow.start_count == 1 for flow in flows)
    assert _RecordingFlow.instances == flows
    assert traffic_host._active_flows == flows


# ----------------------------------------------------------------------
# iperf3
# ----------------------------------------------------------------------


def test_run_iperf3_lists_create_cartesian_product_with_explicit_config(
    traffic_host: _TrafficHost,
) -> None:
    source = traffic_host.satellites[0]
    destinations = [
        traffic_host.ground_stations[5],
        traffic_host.ground_stations[6],
    ]
    config = Iperf3Config(protocol="UDP", duration=3, bandwidth_mbps=10.0)

    with patch(f"{_MIXIN_MODULE}.Iperf3Flow", _RecordingFlow):
        flows = traffic_host.run_iperf3(source, destinations, config)

    assert len(flows) == 2
    assert [flow.destination for flow in flows] == destinations
    assert all(flow.source is source for flow in flows)
    assert all(flow.config is config for flow in flows)
    assert all(flow.start_count == 1 for flow in flows)
    assert _RecordingFlow.instances == flows
    assert traffic_host._active_flows == flows


def test_run_iperf3_requires_an_explicit_config(
    traffic_host: _TrafficHost,
) -> None:
    with pytest.raises(TypeError):
        traffic_host.run_iperf3(
            traffic_host.satellites[0], traffic_host.ground_stations[5]
        )


# ----------------------------------------------------------------------
# Accumulation
# ----------------------------------------------------------------------


def test_active_flows_accumulate_across_traffic_calls(
    traffic_host: _TrafficHost,
) -> None:
    source = traffic_host.satellites[0]
    destination = traffic_host.ground_stations[5]

    with patch(f"{_MIXIN_MODULE}.PingFlow", _RecordingFlow):
        ping_flows = traffic_host.ping(source, destination)
    assert len(traffic_host._active_flows) == 1

    with patch(f"{_MIXIN_MODULE}.Hping3Flow", _RecordingFlow):
        hping_flows = traffic_host.run_hping3(source, destination)
    assert len(traffic_host._active_flows) == 2

    with patch(f"{_MIXIN_MODULE}.Iperf3Flow", _RecordingFlow):
        iperf_flows = traffic_host.run_iperf3(
            source, destination, Iperf3Config(duration=1)
        )
    assert len(traffic_host._active_flows) == 3

    assert traffic_host._active_flows == ping_flows + hping_flows + iperf_flows
    assert traffic_host._active_flows[0] is ping_flows[0]
    assert traffic_host._active_flows[1] is hping_flows[0]
    assert traffic_host._active_flows[2] is iperf_flows[0]
