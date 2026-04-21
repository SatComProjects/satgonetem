"""Tests for satgonetem.traffic.ping_utils."""

import time
from unittest.mock import MagicMock

import pytest

from satgonetem.traffic.ping_utils import (
    PingConfig,
    PingFlow,
    PingResults,
    PingStatus,
    run_ping,
)

_SAMPLE_OUTPUT = (
    "PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n"
    "64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=1.23 ms\n"
    "64 bytes from 10.0.0.2: icmp_seq=2 ttl=64 time=0.98 ms\n"
    "\n"
    "--- 10.0.0.2 ping statistics ---\n"
    "2 packets transmitted, 2 received, 0% packet loss, time 200ms\n"
    "rtt min/avg/max/mdev = 0.980/1.105/1.230/0.125 ms\n"
)

_SAMPLE_WITH_LOSS = (
    "PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n"
    "64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=5.00 ms\n"
    "\n"
    "--- 10.0.0.2 ping statistics ---\n"
    "2 packets transmitted, 1 received, 50% packet loss, time 1000ms\n"
    "rtt min/avg/max/mdev = 5.000/5.000/5.000/0.000 ms\n"
)

_SAMPLE_UNREACHABLE = (
    "PING 10.0.0.99 (10.0.0.99) 56(84) bytes of data.\n"
    "\n"
    "--- 10.0.0.99 ping statistics ---\n"
    "3 packets transmitted, 0 received, 100% packet loss, time 2000ms\n"
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


class TestPingConfig:
    """Tests for PingConfig.build_command."""

    def test_command_contains_dst_ip(self):
        cfg = PingConfig(count=3)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "10.0.0.2" in cmd

    def test_command_contains_bind_ip(self):
        cfg = PingConfig()
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-I 10.0.0.1" in cmd

    def test_count_flag(self):
        cfg = PingConfig(count=7)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-c 7" in cmd

    def test_timeout_flag(self):
        cfg = PingConfig(timeout_sec=1.0)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-W 1.0" in cmd

    def test_interval_flag(self):
        cfg = PingConfig(interval_sec=0.5)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-i 0.5" in cmd

    def test_packet_size_flag(self):
        cfg = PingConfig(packet_size=128)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-s 128" in cmd


class TestPingResults:
    """Tests for PingResults parsing."""

    def test_raises_on_empty_output(self):
        with pytest.raises(ValueError, match="empty output"):
            PingResults(raw_output="", config=PingConfig())

    def test_raises_on_unparseable_output(self):
        with pytest.raises(ValueError, match="Could not parse"):
            PingResults(raw_output="something unexpected", config=PingConfig())

    def test_packets_transmitted(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.packets_transmitted == 2

    def test_packets_received(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.packets_received == 2

    def test_packet_loss_zero(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.packet_loss_percent == 0.0

    def test_packet_loss_fifty_percent(self):
        res = PingResults(raw_output=_SAMPLE_WITH_LOSS, config=PingConfig())
        assert res.packet_loss_percent == 50.0

    def test_reachable_when_packets_received(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.reachable is True

    def test_not_reachable_when_zero_received(self):
        res = PingResults(raw_output=_SAMPLE_UNREACHABLE, config=PingConfig())
        assert res.reachable is False

    def test_rtt_avg_parsed(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.rtt_avg_ms == pytest.approx(1.105)

    def test_rtt_min_parsed(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.rtt_min_ms == pytest.approx(0.980)

    def test_rtt_max_parsed(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.rtt_max_ms == pytest.approx(1.230)

    def test_rtt_mdev_parsed(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        assert res.rtt_mdev_ms == pytest.approx(0.125)

    def test_rtt_zeros_when_unreachable(self):
        res = PingResults(raw_output=_SAMPLE_UNREACHABLE, config=PingConfig())
        assert res.rtt_avg_ms == 0.0
        assert res.rtt_min_ms == 0.0
        assert res.rtt_max_ms == 0.0

    def test_repr_contains_loss_and_rtt(self):
        res = PingResults(raw_output=_SAMPLE_OUTPUT, config=PingConfig())
        r = repr(res)
        assert "loss=" in r
        assert "rtt_avg=" in r


class TestPingStatus:
    """Tests for PingStatus enum values."""

    def test_idle_value(self):
        assert PingStatus.IDLE.value == "idle"

    def test_running_value(self):
        assert PingStatus.RUNNING.value == "running"

    def test_done_value(self):
        assert PingStatus.DONE.value == "done"

    def test_error_value(self):
        assert PingStatus.ERROR.value == "error"


class TestPingFlow:
    """Tests for PingFlow lifecycle."""

    def _make_flow(self, output: str) -> PingFlow:
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = output
        cfg = PingConfig(count=2)
        return PingFlow(src, dst, cfg)

    def test_initial_status_is_idle(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        assert flow.status() == PingStatus.IDLE

    def test_results_raises_before_start(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        with pytest.raises(RuntimeError, match="not been started"):
            flow.results()

    def test_start_transitions_to_done(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == PingStatus.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        assert flow.status() == PingStatus.DONE

    def test_results_available_after_done(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == PingStatus.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        res = flow.results()
        assert isinstance(res, PingResults)

    def test_start_twice_raises(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        with pytest.raises(RuntimeError, match="already been started"):
            flow.start()

    def test_error_status_on_bad_output(self):
        flow = self._make_flow("")
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == PingStatus.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        assert flow.status() == PingStatus.ERROR

    def test_results_raises_on_error_status(self):
        flow = self._make_flow("")
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == PingStatus.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        with pytest.raises(RuntimeError, match="failed"):
            flow.results()


class TestRunPing:
    """Tests for run_ping."""

    def test_returns_ping_results(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = _SAMPLE_OUTPUT
        res = run_ping(src, dst, PingConfig(count=2))
        assert isinstance(res, PingResults)

    def test_command_targets_destination_loopback(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = _SAMPLE_OUTPUT
        run_ping(src, dst, PingConfig(count=2))
        cmd_arg = src.execute_command.call_args[0][0]
        assert "10.0.0.2" in cmd_arg

    def test_raises_when_execute_command_returns_non_string(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = None
        with pytest.raises(RuntimeError, match="returned no output"):
            run_ping(src, dst, PingConfig(count=2))
