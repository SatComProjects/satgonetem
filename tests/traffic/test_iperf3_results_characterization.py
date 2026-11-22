"""Characterization tests for the Iperf3Results facade.

These pin the public JSON contract across the parser/presenter split:
the ``summary`` values for TCP, UDP and bidirectional runs, plus the
documented ``raw`` identity (``to_json()["raw"]`` is the parsed JSON dict
itself, not a copy).
"""

from __future__ import annotations

import json

import pytest

from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Results

pytestmark = pytest.mark.unit

_TCP_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 10,
                "num_streams": 2,
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
                    "seconds": 1.0,
                    "bits_per_second": 100_000_000.0,
                    "bytes": 12_500_000,
                    "retransmits": 2,
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
                    }
                ],
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

_TCP_BIDIR_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 10,
                "num_streams": 1,
            }
        },
        "intervals": [
            {
                "streams": [],
                "sum": {
                    "start": 0.0,
                    "end": 5.0,
                    "seconds": 5.0,
                    "bytes": 125_000_000,
                    "bits_per_second": 200_000_000.0,
                },
                "sum_bidir_reverse": {
                    "start": 0.0,
                    "end": 5.0,
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                },
            },
            {
                "streams": [],
                "sum": {
                    "start": 5.0,
                    "end": 10.0,
                    "seconds": 5.0,
                    "bytes": 125_000_000,
                    "bits_per_second": 200_000_000.0,
                },
                "sum_bidir_reverse": {
                    "start": 5.0,
                    "end": 10.0,
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                },
            },
        ],
        "end": {
            "sum_sent": {"bytes": 250_000_000, "retransmits": 10},
            "sum_received": {"bytes": 240_000_000},
            "sum_sent_bidir_reverse": {"bytes": 500_000_000, "retransmits": 4},
            "sum_received_bidir_reverse": {"bytes": 480_000_000},
        },
    }
)


def _assert_float_values(summary: dict, **expected: float) -> None:
    for key, value in expected.items():
        assert summary[key] == pytest.approx(value), key


def test_to_json_summary_tcp_is_pinned() -> None:
    result = Iperf3Results(_TCP_JSON, Iperf3Config(protocol="TCP", duration=10))

    payload = result.to_json()

    assert list(payload) == ["config", "summary", "raw"]
    assert list(payload["summary"]) == [
        "protocol",
        "duration_seconds",
        "num_streams",
        "avg_throughput_mbps",
        "max_throughput_mbps",
        "min_throughput_mbps",
        "pmtu",
        "total_bytes_sent",
        "total_bytes_received",
        "total_retransmits",
        "avg_rtt_ms",
        "max_rtt_ms",
        "avg_rtt_var_us",
        "avg_cwnd_bytes",
        "avg_snd_wnd_bytes",
    ]
    assert payload["summary"]["protocol"] == "TCP"
    assert payload["summary"]["num_streams"] == 2
    assert payload["summary"]["pmtu"] == 1500
    assert payload["summary"]["total_bytes_sent"] == 62_500_000
    assert payload["summary"]["total_bytes_received"] == 61_000_000
    assert payload["summary"]["total_retransmits"] == 5
    _assert_float_values(
        payload["summary"],
        duration_seconds=10.0,
        avg_throughput_mbps=100.0,
        max_throughput_mbps=100.0,
        min_throughput_mbps=100.0,
        avg_rtt_ms=5.0,
        max_rtt_ms=5.0,
        avg_rtt_var_us=200.0,
        avg_cwnd_bytes=512_000.0,
        avg_snd_wnd_bytes=256_000.0,
    )
    assert payload["config"] == {
        "protocol": "TCP",
        "duration": 10,
        "bandwidth_mbps": 0.0,
        "parallel": 1,
        "interval": 1.0,
        "port": 5201,
    }


def test_to_json_summary_udp_is_pinned() -> None:
    result = Iperf3Results(
        _UDP_JSON, Iperf3Config(protocol="UDP", duration=5, bandwidth_mbps=10.0)
    )

    payload = result.to_json()

    assert list(payload["summary"]) == [
        "protocol",
        "duration_seconds",
        "num_streams",
        "avg_throughput_mbps",
        "max_throughput_mbps",
        "min_throughput_mbps",
        "pmtu",
        "total_bytes",
        "total_packets",
        "avg_jitter_ms",
        "total_lost_packets",
        "avg_loss_percent",
        "total_out_of_order",
    ]
    assert payload["summary"]["protocol"] == "UDP"
    assert payload["summary"]["total_bytes"] == 6_250_000
    assert payload["summary"]["total_packets"] == 4465
    assert payload["summary"]["total_lost_packets"] == 10
    assert payload["summary"]["total_out_of_order"] == 0
    assert payload["summary"]["pmtu"] == 0
    _assert_float_values(
        payload["summary"],
        duration_seconds=5.0,
        avg_throughput_mbps=10.0,
        max_throughput_mbps=10.0,
        min_throughput_mbps=10.0,
        avg_jitter_ms=0.48,
        avg_loss_percent=0.22,
    )


def test_to_json_summary_bidir_is_pinned() -> None:
    result = Iperf3Results(_TCP_BIDIR_JSON, Iperf3Config(protocol="TCP", duration=10))

    summary = result.to_json()["summary"]

    assert summary["protocol"] == "TCP"
    assert summary["total_bytes_sent"] == 750_000_000
    assert summary["total_bytes_received"] == 720_000_000
    assert summary["total_retransmits"] == 14
    assert summary["pmtu"] == 0
    _assert_float_values(
        summary,
        duration_seconds=10.0,
        avg_throughput_mbps=600.0,
        max_throughput_mbps=600.0,
        min_throughput_mbps=600.0,
        avg_rtt_ms=0.0,
        max_rtt_ms=0.0,
        avg_rtt_var_us=0.0,
        avg_cwnd_bytes=0.0,
        avg_snd_wnd_bytes=0.0,
    )


def test_to_json_raw_is_the_same_dict_by_reference() -> None:
    result = Iperf3Results(_TCP_JSON, Iperf3Config())

    first = result.to_json()
    second = result.to_json()

    assert first["raw"] is second["raw"]
    assert first["raw"] is result._data
    # The raw payload is shared, not snapshotted: callers can inspect/mutate
    # the parsed JSON through the returned reference.
    first["raw"]["marker"] = "shared"
    assert result._data["marker"] == "shared"
    assert second["raw"]["marker"] == "shared"
