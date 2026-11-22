"""Regression tests for M10: traffic fan-out flow registration.

Every created flow must be tracked before ``start()`` so a later failure cannot
orphan already-running flows from ``_active_flows`` and stop_gonetem().
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin
from satgonetem.services.mixins.traffic_testing import TrafficTestingMixin
from satgonetem.traffic import Hping3Config, Iperf3Config
from tests.support import DummyTopology

pytestmark = pytest.mark.unit

_MIXIN_MODULE = "satgonetem.services.mixins.traffic_testing"


class _TrafficHost(TrafficTestingMixin, NetworkLifecycleMixin, DummyTopology):
    """Topology host exposing traffic fan-out and flow cleanup."""


class FailingStartFlow:
    """Flow double whose second instance fails to start."""

    instances: list[FailingStartFlow] = []

    def __init__(self, source, destination, config) -> None:
        self.source = source
        self.destination = destination
        self.config = config
        self.start_calls = 0
        self.stop_calls = 0
        self._thread = None
        type(self).instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        if len(type(self).instances) == 2 and self is type(self).instances[1]:
            raise RuntimeError("start failed")

    def status(self):
        return SimpleNamespace(name="RUNNING")

    def stop(self) -> None:
        self.stop_calls += 1


@pytest.fixture(autouse=True)
def _reset_instances():
    FailingStartFlow.instances.clear()
    yield
    FailingStartFlow.instances.clear()


@pytest.fixture
def traffic_host() -> _TrafficHost:
    host = _TrafficHost()
    host.add_satellite("Sat0")
    host.add_satellite("Sat1")
    host.add_ground_station("Gnd5")
    return host


def _invoke(host: _TrafficHost, method_name: str):
    sources = [host.satellites[0], host.satellites[1]]
    destination = host.ground_stations[5]
    if method_name == "ping":
        return host.ping(sources, destination)
    if method_name == "run_hping3":
        return host.run_hping3(sources, destination, Hping3Config(count=1))
    if method_name == "run_iperf3":
        return host.run_iperf3(sources, destination, Iperf3Config(duration=1))
    raise AssertionError(f"unknown method {method_name}")


@pytest.mark.parametrize(
    ("method_name", "flow_class_name"),
    [
        ("ping", "PingFlow"),
        ("run_hping3", "Hping3Flow"),
        ("run_iperf3", "Iperf3Flow"),
    ],
)
def test_failed_start_keeps_every_created_flow_tracked_and_stoppable(
    traffic_host, method_name, flow_class_name
):
    with patch(f"{_MIXIN_MODULE}.{flow_class_name}", FailingStartFlow):
        with pytest.raises(RuntimeError, match="start failed"):
            _invoke(traffic_host, method_name)

    first, second = FailingStartFlow.instances
    assert traffic_host._active_flows == [first, second]
    assert first.start_calls == 1
    assert second.start_calls == 1

    traffic_host._stop_active_flows()

    assert first.stop_calls == 1
    assert second.stop_calls == 1
    assert traffic_host._active_flows == []
