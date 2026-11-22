"""Lazy port-range configuration and atomic iperf3 pool reconfiguration.

A malformed SATGONETEM_IPERF3_PORT_RANGE must not break importing the iperf3
modules; the ValueError belongs at first use. Replacing the pool must also be
atomic with respect to allocation: a port handed out from the old pool can
never also be handed out by the new one.
"""

from __future__ import annotations

import importlib.util
import threading
import time
from types import ModuleType
from unittest.mock import MagicMock

import pytest

import satgonetem.traffic.iperf3_ports as iperf3_ports
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_flow import FlowStatus, Iperf3Flow
from satgonetem.traffic.iperf3_ports import (
    _Iperf3PortAllocator,
    get_iperf3_port_range,
    set_iperf3_port_range,
)

pytestmark = pytest.mark.unit

_ENV = "SATGONETEM_IPERF3_PORT_RANGE"


def _load_isolated_module(monkeypatch, env_value: str) -> ModuleType:
    monkeypatch.setenv(_ENV, env_value)
    spec = importlib.util.spec_from_file_location(
        f"_iperf3_ports_{id(env_value)}", iperf3_ports.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_malformed_env_does_not_fail_import_and_fails_on_use(monkeypatch) -> None:
    module = _load_isolated_module(monkeypatch, "lots")

    with pytest.raises(ValueError, match="first-last"):
        module._IPERF3_PORT_ALLOCATOR.allocate()


def test_environment_range_is_read_when_first_used(monkeypatch) -> None:
    module = _load_isolated_module(monkeypatch, "8000-8000")

    assert module._IPERF3_PORT_ALLOCATOR.allocate() == 8000


class _HoldingAllocator(_Iperf3PortAllocator):
    """Allocator that blocks while its in-use snapshot is being read."""

    def __init__(self) -> None:
        super().__init__(base_port=5201, max_port=5201)
        self.checked = threading.Event()
        self.proceed = threading.Event()

    @property
    def in_use_ports(self) -> set[int]:
        self.checked.set()
        assert self.proceed.wait(2.0), "test did not release the allocator"
        return super().in_use_ports


def test_reconfiguration_cannot_race_with_allocation(monkeypatch) -> None:
    pool = iperf3_ports._Iperf3PortPool()
    holding = _HoldingAllocator()
    pool._allocator = holding
    monkeypatch.setattr(iperf3_ports, "_IPERF3_PORT_ALLOCATOR", pool)

    reconfigured = threading.Event()

    def reconfigure() -> None:
        set_iperf3_port_range(7000, 7000)
        reconfigured.set()

    reconfig_thread = threading.Thread(target=reconfigure)
    reconfig_thread.start()
    assert holding.checked.wait(2.0), "reconfiguration did not inspect the pool"

    allocated: list[int] = []
    allocation_started = threading.Event()

    def allocate() -> None:
        allocation_started.set()
        allocated.append(pool.allocate())

    alloc_thread = threading.Thread(target=allocate)
    alloc_thread.start()
    assert allocation_started.wait(2.0)
    time.sleep(0.05)
    assert alloc_thread.is_alive(), "allocation ran while reconfiguration was active"

    holding.proceed.set()
    reconfig_thread.join(timeout=2.0)
    alloc_thread.join(timeout=2.0)

    assert reconfigured.is_set()
    assert allocated == [7000]
    assert get_iperf3_port_range() == (7000, 7000)


def test_stop_during_delay_does_not_leave_a_port_reserved(monkeypatch) -> None:
    """A flow cancelled before its delay elapses must not exhaust the pool."""
    monkeypatch.setattr(
        iperf3_ports,
        "_IPERF3_PORT_ALLOCATOR",
        _Iperf3PortAllocator(base_port=5201, max_port=5201),
    )
    node = MagicMock()
    node.loopback.ipv4 = "10.0.0.1"
    flow = Iperf3Flow(node, node, Iperf3Config(duration=1), delay=30.0)

    flow.start()
    assert flow._port is None
    assert iperf3_ports._IPERF3_PORT_ALLOCATOR.in_use_ports == set()

    flow.stop()

    assert flow.status() == FlowStatus.ERROR
    assert iperf3_ports._IPERF3_PORT_ALLOCATOR.in_use_ports == set()
    # A leaked reservation would make this reconfiguration fail.
    set_iperf3_port_range(7000, 7001)
    assert get_iperf3_port_range() == (7000, 7001)
