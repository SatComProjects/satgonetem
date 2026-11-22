"""Iperf3 process probes and kills must not match their own shell.

``pkill -f``/``pgrep -f`` run through ``sh -c``, whose command line contains
the pattern itself. An unbracketed ``iperf3 ...`` pattern therefore matches
(and signals) the wrapper shell. Bracketing the first character
(``[i]perf3``) matches the real process but not the wrapper's literal text.

A server that never becomes ready must also fail clearly instead of running
the client against nothing.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_flow import FlowStatus, Iperf3Flow
from satgonetem.traffic.iperf3_io import _is_iperf3_server_listening
from satgonetem.traffic.iperf3_runner import run_iperf3_flow

pytestmark = pytest.mark.unit

_TCP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 1, "num_streams": 1}},
        "intervals": [],
        "end": {},
    }
)


@pytest.fixture(autouse=True)
def _fresh_port_allocator(monkeypatch):
    monkeypatch.setattr(
        iperf3_ports,
        "_IPERF3_PORT_ALLOCATOR",
        iperf3_ports._Iperf3PortAllocator(),
    )


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


def _commands(node: MagicMock, marker: str) -> list[str]:
    return [
        call.args[0]
        for call in node.execute_command.call_args_list
        if call.args and marker in call.args[0]
    ]


def test_flow_stop_kill_pattern_is_bracketed() -> None:
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
    port = flow._reserve_port()
    flow._status = FlowStatus.RUNNING
    flow._thread = MagicMock()

    flow.stop()

    for node in (source, destination):
        kills = _commands(node, "pkill")
        assert len(kills) == 1
        assert f"[i]perf3 .*-p {port} " in kills[0]
        assert "iperf3 .*-p" not in kills[0]


@pytest.mark.parametrize("dst_ip", ["10.0.0.2", None])
def test_error_cleanup_kill_pattern_is_bracketed(dst_ip: str | None) -> None:
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
    port = flow._reserve_port()

    flow._kill_server(dst_ip)

    kills = _commands(destination, "pkill")
    assert len(kills) == 1
    assert f"[i]perf3 -s -1 " in kills[0]
    assert f"-p {port}" in kills[0]
    assert "iperf3 -s -1" not in kills[0]


def test_kill_server_never_targets_the_unreserved_config_port() -> None:
    """Cleanup must use this flow's reserved port, never config.port."""
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1, port=6000))
    port = flow._reserve_port()
    assert port != flow.config.port

    flow._kill_server("10.0.0.2")

    kills = _commands(destination, "pkill")
    assert len(kills) == 1
    assert f"-p {port}" in kills[0]
    assert f"-p {flow.config.port}" not in kills[0]


def test_failed_allocation_does_not_kill_another_flows_server(monkeypatch) -> None:
    """No port reserved: cleanup must not pkill whatever holds config.port."""
    monkeypatch.setattr(
        iperf3_ports,
        "_IPERF3_PORT_ALLOCATOR",
        iperf3_ports._Iperf3PortAllocator(base_port=5201, max_port=5201),
    )
    # Another concurrent flow owns the only port in the pool.
    assert iperf3_ports._IPERF3_PORT_ALLOCATOR.allocate() == 5201

    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    source.execute_command.return_value = ""
    destination.execute_command.return_value = ""
    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))

    flow.start()
    flow.join(timeout=5.0)

    assert flow.status() == FlowStatus.ERROR
    assert "no free iperf3 port" in str(flow._error)
    assert _commands(destination, "pkill") == []


def test_pre_reservation_failure_does_not_kill_a_server(monkeypatch) -> None:
    """A failure before _reserve_port must leave every process untouched."""

    class _NoLoopback:
        @property
        def ipv4(self) -> str:
            raise RuntimeError("loopback unavailable")

    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    destination.loopback = _NoLoopback()

    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
    flow.start()
    flow.join(timeout=5.0)

    assert flow.status() == FlowStatus.ERROR
    assert "loopback unavailable" in str(flow._error)
    assert _commands(destination, "pkill") == []


def test_server_probe_pgrep_pattern_is_bracketed() -> None:
    node = _make_node("gs1", "10.0.0.2")
    node.execute_command.return_value = ""

    _is_iperf3_server_listening(node, "10.0.0.2", 5201)

    command = node.execute_command.call_args.args[0]
    assert "[i]perf3 -s -1 -B 10.0.0.2 -p 5201" in command
    assert "iperf3 -s -1" not in command


@patch(
    "satgonetem.traffic.iperf3_flow._wait_for_iperf3_server",
    return_value=False,
)
@patch("satgonetem.traffic.iperf3_flow.time.sleep")
def test_flow_fails_when_server_did_not_start(_sleep, _wait) -> None:
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    source.execute_command.return_value = _TCP_JSON
    destination.execute_command.return_value = ""

    flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
    flow.start()
    flow.join(timeout=5.0)

    assert flow.status() == FlowStatus.ERROR
    assert "server did not start" in str(flow._error)
    assert not _commands(source, "iperf3 -c"), "client must not run"


@patch(
    "satgonetem.traffic.iperf3_runner._wait_for_iperf3_server",
    return_value=False,
)
def test_runner_fails_when_server_did_not_start(_wait) -> None:
    source = _make_node("gs0", "10.0.0.1")
    destination = _make_node("gs1", "10.0.0.2")
    source.execute_command.return_value = _TCP_JSON
    destination.execute_command.return_value = ""

    with pytest.raises(RuntimeError, match="server did not start"):
        run_iperf3_flow(source, destination, Iperf3Config(duration=1))

    assert not _commands(source, "iperf3 -c"), "client must not run"
