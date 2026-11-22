"""Tests for the stateless iperf3 result presenters."""

from __future__ import annotations

import json

import pytest

from satgonetem.traffic.iperf3_report import (
    format_repr,
    format_summary,
    summary_dict,
)
from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Results

pytestmark = pytest.mark.unit

_TCP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "TCP", "duration": 5, "num_streams": 1}},
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
                    "seconds": 1.0,
                    "bits_per_second": 100_000_000.0,
                    "bytes": 12_500_000,
                },
            }
        ],
        "end": {
            "sum_sent": {"bytes": 62_500_000, "retransmits": 5},
            "sum_received": {"bytes": 61_000_000},
        },
    }
)

_UDP_JSON = json.dumps(
    {
        "start": {"test_start": {"protocol": "UDP", "duration": 5, "num_streams": 1}},
        "intervals": [
            {
                "streams": [],
                "sum": {
                    "start": 0.0,
                    "end": 1.0,
                    "seconds": 1.0,
                    "bits_per_second": 10_000_000.0,
                    "bytes": 1_250_000,
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
                "out_of_order": 0,
            }
        },
    }
)


def test_format_summary_tcp_is_unchanged() -> None:
    result = Iperf3Results(_TCP_JSON, Iperf3Config(protocol="TCP"))

    assert format_summary(result) == (
        "============================================================\n"
        "          TCP IPERF3 TEST RESULTS\n"
        "============================================================\n"
        "Protocol:              TCP\n"
        "Duration:              5.0 s\n"
        "Parallel streams:      1\n"
        "Avg Throughput:        100.00 Mbps\n"
        "Max Throughput:        100.00 Mbps\n"
        "Min Throughput:        100.00 Mbps\n"
        "Total Bytes Sent:      62.50 MB\n"
        "Total Bytes Received:  61.00 MB\n"
        "Total Retransmits:     5\n"
        "Avg RTT:               5.00 ms\n"
        "Max RTT:               5.00 ms\n"
        "Avg Send CWnd:         512.00 KB\n"
        "Path MTU:              1500 bytes\n"
        "============================================================\n"
    )


def test_format_summary_udp_is_unchanged() -> None:
    result = Iperf3Results(_UDP_JSON, Iperf3Config(protocol="UDP"))

    assert format_summary(result) == (
        "============================================================\n"
        "          UDP IPERF3 TEST RESULTS\n"
        "============================================================\n"
        "Protocol:              UDP\n"
        "Duration:              5.0 s\n"
        "Parallel streams:      1\n"
        "Avg Throughput:        10.00 Mbps\n"
        "Max Throughput:        10.00 Mbps\n"
        "Min Throughput:        10.00 Mbps\n"
        "Total Bytes:           6.25 MB\n"
        "Total Packets:         4465\n"
        "Avg Jitter:            0.480 ms\n"
        "Lost Packets:          10\n"
        "Avg Loss:              0.22 %\n"
        "Out of Order:          0\n"
        "============================================================\n"
    )


def test_format_repr_is_unchanged() -> None:
    result = Iperf3Results(_TCP_JSON, Iperf3Config())

    assert format_repr(result) == (
        "Iperf3Results(protocol='TCP', avg_throughput_mbps=100.00)"
    )


def test_summary_dict_returns_the_same_summary_as_to_json() -> None:
    result = Iperf3Results(_TCP_JSON, Iperf3Config())

    payload = summary_dict(result, result._config, result._data)

    assert payload == result.to_json()
    assert payload["raw"] is result._data


def test_summary_dict_includes_only_protocol_specific_keys() -> None:
    tcp = Iperf3Results(_TCP_JSON, Iperf3Config(protocol="TCP"))
    udp = Iperf3Results(_UDP_JSON, Iperf3Config(protocol="UDP"))

    tcp_summary = summary_dict(tcp, tcp._config, tcp._data)["summary"]
    udp_summary = summary_dict(udp, udp._config, udp._data)["summary"]

    assert "total_retransmits" in tcp_summary
    assert "total_packets" not in tcp_summary
    assert "total_packets" in udp_summary
    assert "total_retransmits" not in udp_summary
