"""Tests for interval flattening and aggregation."""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from satgonetem.traffic.iperf3_intervals import Iperf3IntervalData

pytestmark = pytest.mark.unit


def _data(intervals: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"intervals": intervals, **extra}


def _stream(**fields: Any) -> dict[str, Any]:
    return {"socket": 5, **fields}


def test_flattening_skips_omitted_streams_and_summaries() -> None:
    data = _data(
        [
            {
                "streams": [
                    _stream(start=0.0, end=1.0, bytes=10, omitted=True),
                    _stream(start=1.0, end=2.0, bytes=20),
                ],
                "sum": {"start": 0.0, "end": 1.0, "bytes": 10, "omitted": True},
                "sum_sent": {"start": 1.0, "end": 2.0, "bytes": 20},
                "sum_received": {"start": 1.0, "end": 2.0, "bytes": 18},
                "sum_bidir": {"start": 1.0, "end": 2.0, "bytes": 38},
                "sum_bidir_reverse": {"start": 1.0, "end": 2.0, "bytes": 18},
            }
        ]
    )

    intervals = Iperf3IntervalData(data)

    assert len(intervals.df_streams) == 1
    assert intervals.df_streams.iloc[0]["bytes"] == 20
    assert intervals.df_streams.iloc[0]["_t"] == 2.0
    assert intervals.df_streams.iloc[0]["_label"] == "Socket 5"
    assert set(intervals.df_summaries["_label"]) == {
        "sum_sent",
        "sum_received",
        "sum_bidir",
        "sum_bidir_reverse",
    }
    assert list(intervals.df_summaries["_t"]) == [2.0, 2.0, 2.0, 2.0]


def test_flattening_uses_start_when_end_is_absent() -> None:
    intervals = Iperf3IntervalData(
        _data([{"streams": [_stream(start=1.5, bytes=1)], "sum": {"start": 2.5}}])
    )

    assert intervals.df_streams.iloc[0]["_t"] == 1.5
    assert intervals.df_summaries.iloc[0]["_t"] == 2.5


def test_empty_intervals_produce_empty_frames_and_zero_aggregates() -> None:
    intervals = Iperf3IntervalData({})

    assert intervals.df_streams.empty
    assert intervals.df_summaries.empty
    assert intervals.avg_throughput_mbps() == 0.0
    assert intervals.max_throughput_mbps() == 0.0
    assert intervals.min_throughput_mbps() == 0.0
    assert intervals.pmtu() == 0
    assert intervals.avg_rtt_ms() == 0.0
    assert intervals.max_rtt_ms() == 0.0
    assert intervals.avg_rtt_var_us() == 0.0
    assert intervals.avg_cwnd_bytes() == 0.0
    assert intervals.avg_snd_wnd_bytes() == 0.0


def test_dataframe_accessors_return_copies() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [{"streams": [_stream(start=0.0, end=1.0, bytes=10)], "sum": {"bytes": 10}}]
        )
    )

    streams = intervals.interval_dataframe()
    summaries = intervals.summary_dataframe()
    streams.loc[0, "bytes"] = 999
    summaries.loc[0, "bytes"] = 999

    assert intervals.df_streams.iloc[0]["bytes"] == 10
    assert intervals.df_summaries.iloc[0]["bytes"] == 10
    assert isinstance(streams, pd.DataFrame)


def test_avg_throughput_uses_bytes_over_seconds() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {
                    "streams": [],
                    "sum": {
                        "seconds": 1.0,
                        "bytes": 12_500_000,
                        "bits_per_second": 999.0,
                    },
                }
            ]
        )
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(100.0)


def test_avg_throughput_falls_back_to_bits_per_second_without_seconds() -> None:
    intervals = Iperf3IntervalData(
        _data([{"streams": [], "sum": {"bits_per_second": 100_000_000.0}}])
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(100.0)


def test_avg_throughput_falls_back_when_seconds_are_zero() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {
                    "streams": [],
                    "sum": {
                        "seconds": 0.0,
                        "bytes": 1,
                        "bits_per_second": 50_000_000.0,
                    },
                }
            ]
        )
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(50.0)


def test_max_and_min_throughput_use_the_summary_series() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {"streams": [], "sum": {"bits_per_second": 100_000_000.0}},
                {"streams": [], "sum": {"bits_per_second": 80_000_000.0}},
                {"streams": [], "sum": {"bits_per_second": 90_000_000.0}},
            ]
        )
    )

    assert intervals.max_throughput_mbps() == pytest.approx(100.0)
    assert intervals.min_throughput_mbps() == pytest.approx(80.0)


def test_pmtu_uses_the_last_non_null_stream_value() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {"streams": [_stream(pmtu=1500)]},
                {"streams": [_stream(pmtu=None)]},
                {"streams": [_stream(pmtu=1420)]},
            ]
        )
    )

    assert intervals.pmtu() == 1420


def test_tcp_stream_means_keep_their_units() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {
                    "streams": [
                        _stream(
                            rtt=5000, rttvar=200, snd_cwnd=512_000, snd_wnd=256_000
                        ),
                        _stream(
                            rtt=6000, rttvar=300, snd_cwnd=1024_000, snd_wnd=512_000
                        ),
                    ]
                }
            ]
        )
    )

    assert intervals.avg_rtt_ms() == pytest.approx(5.5)
    assert intervals.max_rtt_ms() == pytest.approx(6.0)
    assert intervals.avg_rtt_var_us() == pytest.approx(250.0)
    assert intervals.avg_cwnd_bytes() == pytest.approx(768_000.0)
    assert intervals.avg_snd_wnd_bytes() == pytest.approx(384_000.0)


def _bidir_data(
    forward: list[dict[str, Any]],
    reverse: list[dict[str, Any]],
) -> dict[str, Any]:
    intervals = []
    for index in range(max(len(forward), len(reverse))):
        interval: dict[str, Any] = {"streams": []}
        if index < len(forward):
            interval["sum"] = forward[index]
        if index < len(reverse):
            interval["sum_bidir_reverse"] = reverse[index]
        intervals.append(interval)
    return _data(intervals)


def test_bidir_avg_max_min_sum_the_paired_interval_rates() -> None:
    intervals = Iperf3IntervalData(
        _bidir_data(
            [
                {
                    "seconds": 5.0,
                    "bytes": 125_000_000,
                    "bits_per_second": 200_000_000.0,
                },
                {
                    "seconds": 5.0,
                    "bytes": 125_000_000,
                    "bits_per_second": 200_000_000.0,
                },
            ],
            [
                {
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                },
                {
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                },
            ],
        )
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(600.0)
    assert intervals.max_throughput_mbps() == pytest.approx(600.0)
    assert intervals.min_throughput_mbps() == pytest.approx(600.0)


def test_bidir_rate_series_falls_back_to_bits_per_second() -> None:
    intervals = Iperf3IntervalData(
        _bidir_data(
            [{"bits_per_second": 10_000_000.0}, {"bits_per_second": 20_000_000.0}],
            [{"bits_per_second": 30_000_000.0}, {"bits_per_second": 40_000_000.0}],
        )
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(50.0)
    assert intervals.max_throughput_mbps() == pytest.approx(60.0)
    assert intervals.min_throughput_mbps() == pytest.approx(40.0)


def test_bidir_length_mismatch_falls_back_to_the_primary_frame() -> None:
    intervals = Iperf3IntervalData(
        _bidir_data(
            [
                {"seconds": 1.0, "bytes": 1_250_000, "bits_per_second": 10_000_000.0},
                {"seconds": 1.0, "bytes": 1_250_000, "bits_per_second": 10_000_000.0},
            ],
            [{"seconds": 1.0, "bytes": 2_500_000, "bits_per_second": 20_000_000.0}],
        )
    )

    # Pairing is impossible, so max/min fall back to the busy summary rows
    # (both directions), exactly as before the split.
    assert intervals.max_throughput_mbps() == pytest.approx(20.0)
    assert intervals.min_throughput_mbps() == pytest.approx(10.0)


def test_summary_series_prefers_sum_then_received_then_sent() -> None:
    intervals = Iperf3IntervalData(
        _data(
            [
                {
                    "streams": [],
                    "sum_received": {"seconds": 1.0, "bits_per_second": 20_000_000.0},
                    "sum_sent": {"seconds": 1.0, "bits_per_second": 30_000_000.0},
                }
            ]
        )
    )

    assert intervals.avg_throughput_mbps() == pytest.approx(20.0)
