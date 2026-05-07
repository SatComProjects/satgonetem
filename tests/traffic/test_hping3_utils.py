"""Tests for satgonetem.traffic.hping3_utils."""

import time
from unittest.mock import MagicMock

import pytest

from satgonetem.traffic.hping3_utils import (
    Hping3Config,
    Hping3Flow,
    Hping3Results,
    Hping3Status,
    run_hping3,
)

_SAMPLE_OUTPUT = (
    "HPING gs2 (eth0 10.1.0.2): S set, 40 headers + 0 data bytes\n"
    "len=46 ip=10.1.0.2 ttl=64 DF id=0 sport=0 flags=RA seq=0 win=0 rtt=1.4 ms\n"
    "len=46 ip=10.1.0.2 ttl=64 DF id=0 sport=0 flags=RA seq=1 win=0 rtt=0.9 ms\n"
    "len=46 ip=10.1.0.2 ttl=64 DF id=0 sport=0 flags=RA seq=2 win=0 rtt=1.1 ms\n"
    "\n"
    "--- gs2 hping statistic ---\n"
    "3 packets transmitted, 3 packets received, 0% packet loss\n"
    "round-trip min/avg/max = 0.9/1.1/1.4 ms\n"
)

_SAMPLE_WITH_PAYLOAD = (
    "HPING gs2 (eth0 10.1.0.2): tcp mode set, 40 headers + 64 data bytes\n"
    "len=110 ip=10.1.0.2 ttl=64 DF id=0 sport=80 flags=SA seq=0 win=65535 rtt=2.0 ms\n"
    "\n"
    "--- gs2 hping statistic ---\n"
    "1 packets transmitted, 1 packets received, 0% packet loss\n"
)

_SAMPLE_WITH_LOSS = (
    "HPING gs2 (eth0 10.1.0.2): S set, 40 headers + 0 data bytes\n"
    "len=46 ip=10.1.0.2 ttl=64 DF id=0 sport=0 flags=RA seq=0 win=0 rtt=5.0 ms\n"
    "\n"
    "--- gs2 hping statistic ---\n"
    "2 packets transmitted, 1 packets received, 50% packet loss\n"
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


class TestHping3Config:
    """Tests for Hping3Config.build_command."""

    def test_default_tcp_command(self):
        cfg = Hping3Config(count=10)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "hping3" in cmd
        assert "10.0.0.2" in cmd
        assert "-c 10" in cmd
        assert "-a 10.0.0.1" in cmd
        assert "--udp" not in cmd
        assert "--icmp" not in cmd

    def test_udp_protocol(self):
        cfg = Hping3Config(proto="udp", dport=53)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--udp" in cmd
        assert "-p 53" in cmd

    def test_icmp_protocol_no_dport(self):
        cfg = Hping3Config(proto="icmp", dport=80)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--icmp" in cmd
        assert "-p 80" not in cmd

    def test_sport_included_when_set(self):
        cfg = Hping3Config(sport=1024)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-s 1024" in cmd

    def test_sport_absent_when_none(self):
        cfg = Hping3Config(sport=None)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert " -s " not in cmd

    def test_size_flag(self):
        cfg = Hping3Config(size=64)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-d 64" in cmd

    def test_ttl_flag(self):
        cfg = Hping3Config(ttl=32)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--ttl 32" in cmd

    def test_tcp_flags_syn(self):
        cfg = Hping3Config(proto="tcp", flags=["S"])
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-S" in cmd

    def test_tcp_flags_multiple(self):
        cfg = Hping3Config(proto="tcp", flags=["S", "A"])
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-S" in cmd
        assert "-A" in cmd

    def test_tcp_flags_not_applied_for_udp(self):
        cfg = Hping3Config(proto="udp", flags=["S"])
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-S" not in cmd

    def test_rate_type_interval(self):
        cfg = Hping3Config(rate_type="interval", interval="u10000")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-i u10000" in cmd

    def test_rate_type_fast(self):
        cfg = Hping3Config(rate_type="fast")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--fast" in cmd

    def test_rate_type_faster(self):
        cfg = Hping3Config(rate_type="faster")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--faster" in cmd

    def test_rate_type_flood(self):
        cfg = Hping3Config(rate_type="flood")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "--flood" in cmd

    def test_interval_not_added_when_rate_type_is_not_interval(self):
        cfg = Hping3Config(rate_type="fast", interval="u10000")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-i u10000" not in cmd

    def test_dport_not_added_when_zero(self):
        cfg = Hping3Config(proto="tcp", dport=0)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-p 0" not in cmd

    def test_spoof_src_overrides_bind_ip(self):
        cfg = Hping3Config(spoof_src="192.168.1.5")
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-a 192.168.1.5" in cmd
        assert "-a 10.0.0.1" not in cmd

    def test_spoof_src_none_falls_back_to_bind_ip(self):
        cfg = Hping3Config(spoof_src=None)
        cmd = cfg.build_command("10.0.0.2", "10.0.0.1")
        assert "-a 10.0.0.1" in cmd


class TestHping3Results:
    """Tests for Hping3Results parsing."""

    def test_raises_on_empty_output(self):
        cfg = Hping3Config()
        with pytest.raises(ValueError, match="empty output"):
            Hping3Results(raw_output="", config=cfg)

    def test_raises_on_whitespace_output(self):
        cfg = Hping3Config()
        with pytest.raises(ValueError, match="empty output"):
            Hping3Results(raw_output="   \n  ", config=cfg)

    def test_rtt_values_parsed(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.rtt_ms == pytest.approx([1.4, 0.9, 1.1])

    def test_seq_matches_rtt_count(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.seq == [1, 2, 3]

    def test_packets_transmitted(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.packets_transmitted == 3

    def test_packets_received(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.packets_received == 3

    def test_packet_loss_zero(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.packet_loss_percent == 0.0

    def test_packet_loss_fifty_percent(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_WITH_LOSS, config=cfg)
        assert res.packet_loss_percent == 50.0

    def test_reachable_when_replies_received(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.reachable is True

    def test_rtt_min(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.rtt_min_ms == pytest.approx(0.9)

    def test_rtt_max(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.rtt_max_ms == pytest.approx(1.4)

    def test_rtt_avg(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.rtt_avg_ms == pytest.approx((1.4 + 0.9 + 1.1) / 3)

    def test_payload_bytes_from_header(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_WITH_PAYLOAD, config=cfg)
        assert res.payload_bytes == 64

    def test_payload_bytes_zero_when_header_reports_zero(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert res.payload_bytes == 0

    def test_cumulative_mbit_length_matches_seq(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert len(res.cumulative_mbit) == len(res.seq)

    def test_repr_contains_received_count(self):
        cfg = Hping3Config()
        res = Hping3Results(raw_output=_SAMPLE_OUTPUT, config=cfg)
        assert "received=3" in repr(res)


class TestHping3Status:
    """Tests for Hping3Status enum values."""

    def test_idle_value(self):
        assert Hping3Status.IDLE.value == "idle"

    def test_running_value(self):
        assert Hping3Status.RUNNING.value == "running"

    def test_done_value(self):
        assert Hping3Status.DONE.value == "done"

    def test_error_value(self):
        assert Hping3Status.ERROR.value == "error"


class TestHping3Flow:
    """Tests for Hping3Flow lifecycle."""

    def _make_flow(self, output: str) -> Hping3Flow:
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = output
        cfg = Hping3Config(count=3)
        return Hping3Flow(src, dst, cfg)

    def test_initial_status_is_idle(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        assert flow.status() == Hping3Status.IDLE

    def test_results_raises_before_start(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        with pytest.raises(RuntimeError, match="not been started"):
            flow.results()

    def test_start_transitions_to_done(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == Hping3Status.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        assert flow.status() == Hping3Status.DONE

    def test_results_available_after_done(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == Hping3Status.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        res = flow.results()
        assert isinstance(res, Hping3Results)

    def test_start_twice_raises(self):
        flow = self._make_flow(_SAMPLE_OUTPUT)
        flow.start()
        with pytest.raises(RuntimeError, match="already been started"):
            flow.start()

    def test_error_status_on_bad_output(self):
        flow = self._make_flow("")
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == Hping3Status.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        assert flow.status() == Hping3Status.ERROR

    def test_results_raises_on_error_status(self):
        flow = self._make_flow("")
        flow.start()
        deadline = time.time() + 2.0
        while flow.status() == Hping3Status.RUNNING and time.time() < deadline:
            time.sleep(0.05)
        with pytest.raises(RuntimeError, match="failed"):
            flow.results()


class TestRunHping3:
    """Tests for run_hping3."""

    def test_returns_hping3_results(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = _SAMPLE_OUTPUT
        cfg = Hping3Config(count=3)
        res = run_hping3(src, dst, cfg)
        assert isinstance(res, Hping3Results)

    def test_command_targets_destination_loopback(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = _SAMPLE_OUTPUT
        cfg = Hping3Config(count=3)
        run_hping3(src, dst, cfg)
        cmd_arg = src.execute_command.call_args[0][0]
        assert "10.0.0.2" in cmd_arg
        assert "-a 10.0.0.1" in cmd_arg

    def test_raises_when_execute_command_returns_non_string(self):
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = None
        cfg = Hping3Config(count=3)
        with pytest.raises(RuntimeError, match="returned no output"):
            run_hping3(src, dst, cfg)
