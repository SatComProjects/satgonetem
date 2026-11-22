"""Regression tests for TRAFFIC-12: unique iperf3 ports per flow.

Concurrent flows to the same destination used to share config.port (5201),
causing server bind collisions and clients attaching to the wrong listener.
These tests exercise the allocator and verify that every flow builds its
server/client commands from its own reserved port without mutating the
caller-supplied config.
"""

import json
import re
import threading
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic import iperf3_flow, iperf3_ports
from satgonetem.traffic.iperf3_utils import (
    FlowStatus,
    Iperf3Config,
    Iperf3Flow,
    run_iperf3_flow,
)

_TCP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 1,
                "num_streams": 1,
            }
        },
        "intervals": [],
        "end": {},
    }
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


@pytest.fixture(autouse=True)
def _fresh_port_allocator(monkeypatch):
    """Give every test an isolated process-wide allocator."""
    monkeypatch.setattr(
        iperf3_ports,
        "_IPERF3_PORT_ALLOCATOR",
        iperf3_ports._Iperf3PortAllocator(),
    )


def _commands_containing(node: MagicMock, marker: str) -> list[str]:
    return [
        call.args[0]
        for call in node.execute_command.call_args_list
        if call.args and marker in call.args[0]
    ]


class TestPortAllocator:
    """Tests for the thread-safe per-flow port allocator."""

    def test_allocator_returns_unique_ports_under_concurrent_allocation(self):
        allocator = iperf3_ports._Iperf3PortAllocator(base_port=5201, max_port=5220)
        allocated: list[int] = []
        allocated_lock = threading.Lock()
        barrier = threading.Barrier(16)

        def allocate_one() -> None:
            barrier.wait()
            port = allocator.allocate()
            with allocated_lock:
                allocated.append(port)

        threads = [threading.Thread(target=allocate_one) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        assert len(allocated) == 16
        assert len(set(allocated)) == 16
        assert all(5201 <= port <= 5220 for port in allocated)

    def test_allocator_reuses_released_ports(self):
        allocator = iperf3_ports._Iperf3PortAllocator(base_port=5201, max_port=5202)

        first = allocator.allocate()
        second = allocator.allocate()
        assert {first, second} == {5201, 5202}

        allocator.release(first)
        assert allocator.allocate() == first

        with pytest.raises(RuntimeError, match="no free iperf3 port"):
            allocator.allocate()


class TestUniqueFlowPorts:
    """Two flows to one destination must never share a server port."""

    def test_two_flows_to_same_destination_get_distinct_ports(self):
        destination = _make_node("gnd0", "10.0.0.2")
        config = Iperf3Config(duration=1)

        flow_a = Iperf3Flow(_make_node("sat0", "10.0.0.1"), destination, config)
        flow_b = Iperf3Flow(_make_node("sat1", "10.0.0.3"), destination, config)
        port_a = flow_a._reserve_port()
        port_b = flow_b._reserve_port()

        assert port_a != port_b
        assert config.port == 5201
        assert 5201 <= port_a <= 5300
        assert 5201 <= port_b <= 5300

    @patch(
        "satgonetem.traffic.iperf3_flow._wait_for_iperf3_server",
        return_value=True,
    )
    @patch("satgonetem.traffic.iperf3_flow.time.sleep")
    def test_flow_commands_use_own_port_and_do_not_mutate_config(
        self, _mock_sleep, _mock_ready
    ):
        destination = _make_node("gnd0", "10.0.0.2")
        destination.execute_command.return_value = None
        src_a = _make_node("sat0", "10.0.0.1")
        src_b = _make_node("sat1", "10.0.0.3")
        src_a.execute_command.return_value = _TCP_JSON
        src_b.execute_command.return_value = _TCP_JSON
        config = Iperf3Config(duration=1)

        flow_a = Iperf3Flow(src_a, destination, config)
        flow_b = Iperf3Flow(src_b, destination, config)
        # Run synchronously to avoid thread scheduling noise.
        flow_a._run()
        flow_b._run()

        assert flow_a.status() == FlowStatus.DONE
        assert flow_b.status() == FlowStatus.DONE
        # Shared config is untouched by both flows.
        assert config.port == 5201

        server_cmds = _commands_containing(destination, "iperf3 -s")
        assert len(server_cmds) == 2
        client_a_cmds = _commands_containing(src_a, "iperf3 -c")
        client_b_cmds = _commands_containing(src_b, "iperf3 -c")
        assert len(client_a_cmds) == 1
        assert len(client_b_cmds) == 1

        # The reservation is released after completion, so read the effective
        # port from the commands/results the flow produced.
        port_a = flow_a.results().to_json()["config"]["port"]
        port_b = flow_b.results().to_json()["config"]["port"]
        assert port_a != port_b
        assert any(f"-p {port_a} " in cmd for cmd in server_cmds)
        assert any(f"-p {port_b} " in cmd for cmd in server_cmds)
        assert f"-p {port_a} " in client_a_cmds[0]
        assert f"-p {port_b} " in client_b_cmds[0]

    @patch("satgonetem.traffic.iperf3_flow.time.sleep")
    def test_stop_pkill_uses_flow_port(self, _mock_sleep):
        src = _make_node("sat0", "10.0.0.1")
        dst = _make_node("gnd0", "10.0.0.2")
        flow = Iperf3Flow(src, dst, Iperf3Config())
        flow._reserve_port()
        flow._status = FlowStatus.RUNNING
        flow._thread = MagicMock()

        flow.stop()

        for node in (src, dst):
            pkill_cmds = _commands_containing(node, "pkill")
            assert len(pkill_cmds) == 1
            assert f"-p {flow._port} " in pkill_cmds[0]


class TestRunIperf3FlowPorts:
    """The blocking entry point must allocate too."""

    @patch(
        "satgonetem.traffic.iperf3_runner._wait_for_iperf3_server",
        return_value=True,
    )
    @patch(
        "satgonetem.traffic.iperf3_flow._wait_for_iperf3_server",
        return_value=True,
    )
    @patch("satgonetem.traffic.iperf3_flow.time.sleep")
    def test_blocking_flow_uses_one_allocated_port_for_both_sides(
        self, _mock_sleep, _mock_ready, _mock_runner_ready
    ):
        src = _make_node("sat0", "10.0.0.1")
        dst = _make_node("gnd0", "10.0.0.2")
        src.execute_command.return_value = _TCP_JSON
        dst.execute_command.return_value = None
        config = Iperf3Config(duration=1)

        # Occupy the legacy default port so the run must pick another one.
        assert iperf3_ports._IPERF3_PORT_ALLOCATOR.allocate() == 5201

        result = run_iperf3_flow(src, dst, config)

        server_cmd = _commands_containing(dst, "iperf3 -s")[0]
        client_cmd = _commands_containing(src, "iperf3 -c")[0]
        server_port = int(re.search(r"-p (\d+)", server_cmd).group(1))
        client_port = int(re.search(r"-p (\d+)", client_cmd).group(1))

        assert server_port == client_port
        assert server_port != 5201
        assert result.to_json()["config"]["port"] == server_port
        assert config.port == 5201


class TestServerReadinessWait:
    """Bounded readiness polling must be mockable and time-bounded."""

    def test_returns_true_when_listener_appears(self):
        node = MagicMock()
        node.execute_command.side_effect = [
            "",
            "LISTEN 0 128 10.0.0.2:5201",
        ]

        with patch("satgonetem.traffic.iperf3_flow.time.sleep") as mock_sleep:
            ready = iperf3_flow._wait_for_iperf3_server(node, "10.0.0.2", 5201)

        assert ready is True
        assert mock_sleep.call_count == 1

    def test_keeps_fallback_when_probe_unavailable(self):
        """An unavailable probe must not fail flows that the grace period covers."""
        node = MagicMock()
        node.execute_command.return_value = None

        with patch("satgonetem.traffic.iperf3_flow.time.sleep") as mock_sleep:
            ready = iperf3_flow._wait_for_iperf3_server(node, "10.0.0.2", 5201)

        assert ready is True
        mock_sleep.assert_called_once_with(0.5)

    @patch("satgonetem.traffic.iperf3_flow.time.sleep")
    def test_flow_proceeds_when_readiness_probe_unavailable(self, _mock_sleep):
        """Reduced execute_command fakes must keep the legacy grace period."""
        source = _make_node("sat0", "10.0.0.1")
        destination = _make_node("gnd0", "10.0.0.2")
        source.execute_command.side_effect = lambda command, **_kwargs: (
            _TCP_JSON if command.startswith("cat ") else ""
        )
        # No listener output: the probe cannot tell whether the server started.
        destination.execute_command.return_value = None

        flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
        flow._run()

        assert flow.status() == FlowStatus.DONE
        assert flow.results().protocol == "TCP"

    @patch(
        "satgonetem.traffic.iperf3_flow._wait_for_iperf3_server",
        return_value=False,
    )
    def test_flow_fails_when_probe_reports_not_listening(self, _mock_ready):
        """An explicit not-listening result after the timeout must still fail."""
        source = _make_node("sat0", "10.0.0.1")
        destination = _make_node("gnd0", "10.0.0.2")
        destination.execute_command.return_value = ""

        flow = Iperf3Flow(source, destination, Iperf3Config(duration=1))
        flow._run()

        assert flow.status() == FlowStatus.ERROR
        assert "iperf3 server did not start" in str(flow._error)

    def test_wait_is_bounded(self):
        node = MagicMock()
        node.execute_command.return_value = ""

        with patch("satgonetem.traffic.iperf3_flow.time.sleep") as mock_sleep:
            ready = iperf3_flow._wait_for_iperf3_server(
                node, "10.0.0.2", 5201, timeout=0.0
            )

        assert ready is False
        mock_sleep.assert_not_called()
