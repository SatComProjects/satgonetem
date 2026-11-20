"""Tests for the size of the process-wide iperf3 server port pool."""

from unittest.mock import MagicMock

import pytest

import satgonetem.traffic.iperf3_utils as iperf3_utils
from satgonetem.traffic import (
    Iperf3Config,
    Iperf3Flow,
    get_iperf3_port_range,
    set_iperf3_port_range,
)


@pytest.fixture(autouse=True)
def fresh_pool():
    """Each test gets a new default pool; the process-wide one is put back after."""
    allocator = iperf3_utils._IPERF3_PORT_ALLOCATOR
    iperf3_utils._IPERF3_PORT_ALLOCATOR = iperf3_utils._Iperf3PortAllocator(
        iperf3_utils._IPERF3_PORT_RANGE_START, iperf3_utils._IPERF3_PORT_RANGE_END
    )
    yield
    iperf3_utils._IPERF3_PORT_ALLOCATOR = allocator


def _flow() -> Iperf3Flow:
    node = MagicMock()
    node.loopback.ipv4 = "10.0.0.1"
    return Iperf3Flow(node, node, Iperf3Config(duration=1))


def test_default_pool_holds_a_congested_run():
    first, last = get_iperf3_port_range()
    assert (first, last) == (5201, 6200)
    flows = [_flow() for _ in range(250)]
    ports = {flow._reserve_port() for flow in flows}
    assert len(ports) == 250


def test_set_iperf3_port_range_replaces_the_pool():
    set_iperf3_port_range(7000, 7009)
    assert get_iperf3_port_range() == (7000, 7009)
    flows = [_flow() for _ in range(10)]
    assert sorted(flow._reserve_port() for flow in flows) == list(range(7000, 7010))
    with pytest.raises(RuntimeError, match="no free iperf3 port in range 7000-7009"):
        _flow()._reserve_port()


def test_set_iperf3_port_range_refuses_while_ports_are_held():
    flow = _flow()
    flow._reserve_port()
    with pytest.raises(RuntimeError, match="hold ports"):
        set_iperf3_port_range(7000, 7999)
    iperf3_utils._IPERF3_PORT_ALLOCATOR.release(flow._port)
    set_iperf3_port_range(7000, 7999)
    assert get_iperf3_port_range() == (7000, 7999)


def test_set_iperf3_port_range_rejects_an_invalid_range():
    with pytest.raises(ValueError):
        set_iperf3_port_range(6000, 5000)


def test_range_from_environment(monkeypatch):
    # read once, when the pool is created at import time
    monkeypatch.setenv("SATGONETEM_IPERF3_PORT_RANGE", "8000-8499")
    assert iperf3_utils._port_range_from_env() == (8000, 8499)
    monkeypatch.delenv("SATGONETEM_IPERF3_PORT_RANGE")
    assert iperf3_utils._port_range_from_env() == (5201, 6200)


def test_malformed_environment_range_is_reported(monkeypatch):
    monkeypatch.setenv("SATGONETEM_IPERF3_PORT_RANGE", "lots")
    with pytest.raises(ValueError, match="first-last"):
        iperf3_utils._port_range_from_env()
