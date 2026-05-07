"""Tests for satgonetem.traffic.iperf3_utils."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from satgonetem.traffic.iperf3_utils import (
    FlowStatus,
    Iperf3Config,
    Iperf3Flow,
    Iperf3Results,
    run_iperf3_flow,
)

_TCP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 5,
                "num_streams": 1,
            }
        },
        "intervals": [
            {
                "streams": [
                    {
                        "socket": 5,
                        "start": 0.0,
                        "end": 1.0,
                        "bits_per_second": 100_000_000.0,
                        "bytes": 12_500_000,
                        "retransmits": 2,
                        "rtt": 5000,
                        "rttvar": 200,
                        "snd_cwnd": 512_000,
                        "snd_wnd": 256_000,
                        "pmtu": 1500,
                    }
                ],
                "sum": {
                    "start": 0.0,
                    "end": 1.0,
                    "bits_per_second": 100_000_000.0,
                    "bytes": 12_500_000,
                    "retransmits": 2,
                },
                "sum_sent": {
                    "start": 0.0,
                    "end": 1.0,
                    "bits_per_second": 100_000_000.0,
                    "bytes": 12_500_000,
                    "retransmits": 2,
                },
                "sum_received": {
                    "start": 0.0,
                    "end": 1.0,
                    "bits_per_second": 98_000_000.0,
                    "bytes": 12_250_000,
                },
            }
        ],
        "end": {
            "sum_sent": {
                "bytes": 62_500_000,
                "retransmits": 5,
            },
            "sum_received": {
                "bytes": 61_000_000,
            },
        },
    }
)

_UDP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "UDP",
                "duration": 5,
                "num_streams": 1,
            }
        },
        "intervals": [
            {
                "streams": [
                    {
                        "socket": 5,
                        "start": 0.0,
                        "end": 1.0,
                        "bits_per_second": 10_000_000.0,
                        "bytes": 1_250_000,
                        "packets": 893,
                        "jitter_ms": 0.5,
                        "lost_packets": 2,
                        "lost_percent": 0.22,
                        "out_of_order": 0,
                    }
                ],
                "sum": {
                    "start": 0.0,
                    "end": 1.0,
                    "bits_per_second": 10_000_000.0,
                    "bytes": 1_250_000,
                    "packets": 893,
                    "jitter_ms": 0.5,
                    "lost_packets": 2,
                    "lost_percent": 0.22,
                    "out_of_order": 0,
                },
            }
        ],
        "end": {
            "sum": {
                "bytes": 6_250_000,
                "packets": 4465,
                "jitter_ms": 0.48,
                "lost_packets": 10,
                "lost_percent": 0.22,
                "out_of_order": 1,
            }
        },
    }
)


def _make_node(name: str, ipv4: str) -> MagicMock:
    node = MagicMock()
    node.name = name
    node.loopback = MagicMock()
    node.loopback.ipv4 = ipv4
    return node


class TestIperf3Config:
    """Tests for Iperf3Config.build_client_command."""

    def test_basic_tcp_command_structure(self):
        cfg = Iperf3Config(protocol="TCP", duration=5)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "iperf3" in cmd
        assert "-c 10.0.0.2" in cmd
        assert "-B 10.0.0.1" in cmd
        assert "-t 5" in cmd
        assert "--json" in cmd
        assert "/tmp/out.json" in cmd

    def test_udp_flag_added_for_udp_protocol(self):
        cfg = Iperf3Config(protocol="UDP")
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-u" in cmd

    def test_udp_bandwidth_flag(self):
        cfg = Iperf3Config(protocol="UDP", bandwidth_mbps=10.0)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-b 10.0M" in cmd

    def test_tcp_bandwidth_flag(self):
        cfg = Iperf3Config(protocol="TCP", bandwidth_mbps=100.0)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-b 100.0M" in cmd

    def test_congestion_control_tcp_only(self):
        cfg = Iperf3Config(protocol="TCP", congestion_control="bbr")
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-C bbr" in cmd

    def test_congestion_control_not_in_udp(self):
        cfg = Iperf3Config(protocol="UDP", congestion_control="bbr")
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-C bbr" not in cmd

    def test_parallel_streams(self):
        cfg = Iperf3Config(parallel=4)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-P 4" in cmd

    def test_reverse_flag(self):
        cfg = Iperf3Config(reverse=True)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "--reverse" in cmd

    def test_bidir_flag(self):
        cfg = Iperf3Config(bidir=True)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "--bidir" in cmd

    def test_num_bytes_replaces_duration(self):
        cfg = Iperf3Config(duration=10, num_bytes=1_000_000)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-n 1000000" in cmd
        assert "-t 10" not in cmd

    def test_window_size(self):
        cfg = Iperf3Config(window_size="256K")
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-w 256K" in cmd

    def test_mss(self):
        cfg = Iperf3Config(mss=1400)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-M 1400" in cmd

    def test_no_delay_flag(self):
        cfg = Iperf3Config(no_delay=True)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-N" in cmd

    def test_tos_flag(self):
        cfg = Iperf3Config(tos=0x10)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-S 16" in cmd

    def test_ttl_flag(self):
        cfg = Iperf3Config(ttl=64)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "--ttl 64" in cmd

    def test_omit_flag(self):
        cfg = Iperf3Config(omit=2)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-O 2" in cmd

    def test_omit_zero_not_included(self):
        cfg = Iperf3Config(omit=0)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-O " not in cmd

    def test_affinity_flag(self):
        cfg = Iperf3Config(affinity="0,1")
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "--affinity 0,1" in cmd

    def test_port_flag(self):
        cfg = Iperf3Config(port=9000)
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        assert "-p 9000" in cmd

    def test_command_not_wrapped_in_sh(self):
        cfg = Iperf3Config()
        cmd = cfg.build_client_command("10.0.0.2", "10.0.0.1", "/tmp/out.json")
        # Node.execute_command now wraps commands safely; builders should
        # return the raw command only.
        assert not cmd.startswith('sh -c "')
        assert "iperf3" in cmd


class TestIperf3Results:
    """Tests for Iperf3Results parsing and properties."""

    def test_raises_on_empty_output(self):
        with pytest.raises(ValueError, match="empty output"):
            Iperf3Results(raw_json="", config=Iperf3Config())

    def test_raises_on_invalid_json(self):
        with pytest.raises(ValueError, match="Failed to parse"):
            Iperf3Results(raw_json="not json", config=Iperf3Config())

    def test_protocol_parsed_from_json(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.protocol == "TCP"

    def test_udp_protocol_parsed(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.protocol == "UDP"

    def test_duration_seconds(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.duration_seconds == pytest.approx(5.0)

    def test_num_streams(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.num_streams == 1

    def test_avg_throughput_mbps_tcp(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.avg_throughput_mbps == pytest.approx(100.0, rel=0.1)

    def test_total_retransmits(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.total_retransmits == 5

    def test_total_bytes_sent(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.total_bytes_sent == 62_500_000

    def test_total_bytes_received(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.total_bytes_received == 61_000_000

    def test_avg_rtt_ms_tcp(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.avg_rtt_ms == pytest.approx(5.0)

    def test_avg_cwnd_bytes(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.avg_cwnd_bytes == pytest.approx(512_000.0)

    def test_udp_total_packets(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.total_packets == 4465

    def test_udp_avg_jitter_ms(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.avg_jitter_ms == pytest.approx(0.48)

    def test_udp_total_lost_packets(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.total_lost_packets == 10

    def test_udp_avg_loss_percent(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.avg_loss_percent == pytest.approx(0.22)

    def test_udp_total_out_of_order(self):
        res = Iperf3Results(raw_json=_UDP_JSON, config=Iperf3Config(protocol="UDP"))
        assert res.total_out_of_order == 1

    def test_pmtu_from_stream(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        assert res.pmtu == 1500

    def test_get_interval_dataframe_not_empty(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        df = res.get_interval_dataframe()
        assert not df.empty

    def test_get_summary_dataframe_not_empty(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        df = res.get_summary_dataframe()
        assert not df.empty

    def test_repr_contains_protocol_and_throughput(self):
        res = Iperf3Results(raw_json=_TCP_JSON, config=Iperf3Config())
        r = repr(res)
        assert "TCP" in r
        assert "avg_throughput_mbps" in r


class TestFlowStatus:
    """Tests for FlowStatus enum values."""

    def test_idle_value(self):
        assert FlowStatus.IDLE.value == "idle"

    def test_running_value(self):
        assert FlowStatus.RUNNING.value == "running"

    def test_done_value(self):
        assert FlowStatus.DONE.value == "done"

    def test_error_value(self):
        assert FlowStatus.ERROR.value == "error"


class TestIperf3Flow:
    """Tests for Iperf3Flow lifecycle."""

    def _make_flow(self, client_output: str) -> Iperf3Flow:
        src = _make_node("gs0", "10.0.0.1")
        dst = _make_node("gs1", "10.0.0.2")
        src.execute_command.return_value = client_output
        dst.execute_command.return_value = None
        cfg = Iperf3Config(duration=1)
        return Iperf3Flow(src, dst, cfg)

    def test_initial_status_is_idle(self):
        flow = self._make_flow(_TCP_JSON)
        assert flow.status() == FlowStatus.IDLE

    def test_results_raises_before_start(self):
        flow = self._make_flow(_TCP_JSON)
        with pytest.raises(RuntimeError, match="not been started"):
            flow.results()

    def test_start_transitions_to_done(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            flow = self._make_flow(_TCP_JSON)
            flow.start()
            deadline = time.time() + 3.0
            while flow.status() == FlowStatus.RUNNING and time.time() < deadline:
                time.sleep(0.05)
            assert flow.status() == FlowStatus.DONE

    def test_results_available_after_done(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            flow = self._make_flow(_TCP_JSON)
            flow.start()
            deadline = time.time() + 3.0
            while flow.status() == FlowStatus.RUNNING and time.time() < deadline:
                time.sleep(0.05)
            res = flow.results()
            assert isinstance(res, Iperf3Results)

    def test_start_twice_raises(self):
        flow = self._make_flow(_TCP_JSON)
        flow.start()
        with pytest.raises(RuntimeError, match="already been started"):
            flow.start()

    def test_error_status_on_bad_json(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            src = _make_node("gs0", "10.0.0.1")
            dst = _make_node("gs1", "10.0.0.2")
            src.execute_command.return_value = "not json"
            dst.execute_command.return_value = None
            cfg = Iperf3Config(duration=1)
            flow = Iperf3Flow(src, dst, cfg)
            flow.start()
            deadline = time.time() + 3.0
            while flow.status() == FlowStatus.RUNNING and time.time() < deadline:
                time.sleep(0.05)
            assert flow.status() == FlowStatus.ERROR


class TestRunIperf3Flow:
    """Tests for run_iperf3_flow."""

    def test_returns_iperf3_results(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            src = _make_node("gs0", "10.0.0.1")
            dst = _make_node("gs1", "10.0.0.2")
            src.execute_command.return_value = _TCP_JSON
            dst.execute_command.return_value = None
            cfg = Iperf3Config(duration=1)
            res = run_iperf3_flow(src, dst, cfg)
            assert isinstance(res, Iperf3Results)

    def test_server_started_on_destination(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            src = _make_node("gs0", "10.0.0.1")
            dst = _make_node("gs1", "10.0.0.2")
            src.execute_command.return_value = _TCP_JSON
            dst.execute_command.return_value = None
            cfg = Iperf3Config(duration=1)
            run_iperf3_flow(src, dst, cfg)
            server_calls = [
                c
                for c in dst.execute_command.call_args_list
                if "iperf3 -s" in str(c)
            ]
            assert len(server_calls) >= 1

    def test_client_run_on_source(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            src = _make_node("gs0", "10.0.0.1")
            dst = _make_node("gs1", "10.0.0.2")
            src.execute_command.return_value = _TCP_JSON
            dst.execute_command.return_value = None
            cfg = Iperf3Config(duration=1)
            run_iperf3_flow(src, dst, cfg)
            client_calls = [
                c
                for c in src.execute_command.call_args_list
                if "iperf3 -c" in str(c)
            ]
            assert len(client_calls) >= 1

    def test_raises_when_no_valid_json_returned(self):
        with patch("satgonetem.traffic.iperf3_utils.time.sleep"):
            src = _make_node("gs0", "10.0.0.1")
            dst = _make_node("gs1", "10.0.0.2")
            src.execute_command.return_value = "no json here"
            dst.execute_command.return_value = "no json either"
            cfg = Iperf3Config(duration=1)
            with pytest.raises(RuntimeError, match="Could not retrieve"):
                run_iperf3_flow(src, dst, cfg)
