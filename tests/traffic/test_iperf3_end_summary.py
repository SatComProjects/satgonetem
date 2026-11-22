"""Tests for the pure iperf3 end-section value object."""

from __future__ import annotations

from typing import Any

import pytest

from satgonetem.traffic.iperf3_end_summary import Iperf3EndSummary

pytestmark = pytest.mark.unit


def _summary(end: dict[str, Any]) -> Iperf3EndSummary:
    return Iperf3EndSummary({"end": end})


def test_missing_end_section_defaults_to_zero() -> None:
    summary = Iperf3EndSummary({})

    assert summary.value("sum_sent", "bytes") == 0.0
    assert summary.pair_value("sum_sent", "bytes") == 0.0
    assert summary.total_bytes_sent == 0
    assert summary.total_bytes_received == 0
    assert summary.total_retransmits == 0
    assert summary.total_bytes == 0
    assert summary.total_packets == 0
    assert summary.avg_jitter_ms == 0.0
    assert summary.total_lost_packets == 0
    assert summary.avg_loss_percent == 0.0
    assert summary.total_out_of_order == 0


def test_null_fields_default_to_zero() -> None:
    summary = _summary(
        {
            "sum_sent": {"bytes": None, "retransmits": None},
            "sum_received": {"bytes": 0},
            "sum": {"packets": None, "jitter_ms": None, "lost_packets": None},
        }
    )

    assert summary.total_bytes_sent == 0
    assert summary.total_retransmits == 0
    assert summary.total_bytes == 0
    assert summary.total_packets == 0
    assert summary.avg_jitter_ms == 0.0
    assert summary.total_lost_packets == 0


def test_non_bidir_uses_forward_sender_and_receiver_values() -> None:
    summary = _summary(
        {
            "sum_sent": {"bytes": 62_500_000, "retransmits": 5},
            "sum_received": {"bytes": 61_000_000},
        }
    )

    assert not summary.is_bidir()
    assert summary.total_bytes_sent == 62_500_000
    assert summary.total_bytes_received == 61_000_000
    assert summary.total_retransmits == 5
    assert summary.pair_value("sum_sent", "bytes") == 62_500_000.0


def test_bidir_detection_is_structural() -> None:
    assert _summary({"anything_bidir_reverse": {}}).is_bidir()
    assert _summary({"sum": {}, "sum_sent_bidir_reverse": {}}).is_bidir()
    assert not _summary({"sum": {}, "sum_sent": {}}).is_bidir()


def test_pair_value_adds_the_reverse_summary_for_bidir() -> None:
    summary = _summary(
        {
            "sum_sent": {"bytes": 250_000_000, "retransmits": 10},
            "sum_sent_bidir_reverse": {"bytes": 500_000_000, "retransmits": 4},
            "sum_received": {"bytes": 240_000_000},
            "sum_received_bidir_reverse": {"bytes": 480_000_000},
        }
    )

    assert summary.total_bytes_sent == 750_000_000
    assert summary.total_bytes_received == 720_000_000
    assert summary.total_retransmits == 14


def test_total_bytes_prefers_receiver_then_sender_then_sum() -> None:
    receiver = _summary(
        {
            "sum_received": {"bytes": 100},
            "sum_sent": {"bytes": 200},
            "sum": {"bytes": 300},
        }
    )
    sender = _summary({"sum_received": {"bytes": 0}, "sum_sent": {"bytes": 200}})
    aggregate = _summary({"sum": {"bytes": 300}})

    assert receiver.total_bytes == 100
    assert sender.total_bytes == 200
    assert aggregate.total_bytes == 300


def test_udp_packets_use_sender_totals_for_bidir() -> None:
    summary = _summary(
        {
            "sum": {"packets": 1786},
            "sum_received": {"packets": 1720, "lost_packets": 20},
            "sum_received_bidir_reverse": {"packets": 3410, "lost_packets": 10},
        }
    )

    # iperf3's receiver summary reports the sender's transmitted count in
    # ``packets``; ``lost_packets`` is already a subset and must not be added.
    assert summary.total_packets == 5130


def test_bidir_udp_live_shape_does_not_double_count_lost_packets() -> None:
    summary = _summary(
        {
            "sum_received": {
                "packets": 121_379,
                "lost_packets": 18_416,
                "jitter_ms": 0.5,
            },
            "sum_received_bidir_reverse": {
                "packets": 118_091,
                "lost_packets": 23_400,
                "jitter_ms": 0.2,
            },
        }
    )

    assert summary.total_packets == 239_470
    assert summary.total_lost_packets == 41_816
    assert summary.avg_loss_percent == pytest.approx(41_816 / 239_470 * 100)
    received = (121_379 - 18_416) + (118_091 - 23_400)
    assert summary.avg_jitter_ms == pytest.approx(
        (0.5 * (121_379 - 18_416) + 0.2 * (118_091 - 23_400)) / received
    )


def test_udp_packets_fall_back_to_sum_when_receiver_pair_is_zero() -> None:
    summary = _summary(
        {
            "sum": {"packets": 1786},
            "sum_received": {"packets": 0},
            "sum_received_bidir_reverse": {"packets": 0},
        }
    )

    assert summary.total_packets == 1786


def test_udp_jitter_is_packet_weighted_for_bidir() -> None:
    summary = _summary(
        {
            "sum": {"jitter_ms": 9.0},
            "sum_received": {"packets": 1700, "jitter_ms": 0.5},
            "sum_received_bidir_reverse": {"packets": 3400, "jitter_ms": 0.2},
        }
    )

    assert summary.avg_jitter_ms == pytest.approx((0.5 * 1700 + 0.2 * 3400) / 5100)


def test_udp_jitter_falls_back_to_sum_without_packets() -> None:
    summary = _summary(
        {
            "sum": {"jitter_ms": 0.48},
            "sum_received": {"packets": 0, "jitter_ms": 0.5},
            "sum_received_bidir_reverse": {"packets": 0, "jitter_ms": 0.2},
        }
    )

    assert summary.avg_jitter_ms == pytest.approx(0.48)


def test_udp_losses_and_percent_recombine_for_bidir() -> None:
    summary = _summary(
        {
            "sum": {"lost_packets": 0, "lost_percent": 0.0},
            "sum_received": {"packets": 1720, "lost_packets": 20},
            "sum_received_bidir_reverse": {"packets": 3410, "lost_packets": 10},
        }
    )

    # iperf3 defines lost_percent over transmitted packets:
    # 100 * lost / packets, here 100 * 30 / 5130.
    assert summary.total_lost_packets == 30
    assert summary.total_packets == 5130
    assert summary.avg_loss_percent == pytest.approx(30 / 5130 * 100)


def test_all_received_packets_lost_reports_full_loss() -> None:
    """Transmitted packets can consist entirely of lost packets."""
    summary = _summary(
        {
            "sum": {"lost_packets": 7, "lost_percent": 1.5},
            "sum_received": {"packets": 100, "lost_packets": 100},
            "sum_received_bidir_reverse": {"packets": 100, "lost_packets": 100},
        }
    )

    assert summary.total_lost_packets == 200
    assert summary.total_packets == 200
    assert summary.avg_loss_percent == pytest.approx(100.0)


def test_udp_losses_fall_back_to_sum_when_receiver_summaries_are_empty() -> None:
    summary = _summary(
        {
            "sum": {"lost_packets": 7, "lost_percent": 1.5},
            "sum_received": {"packets": 0, "lost_packets": 0},
            "sum_received_bidir_reverse": {"packets": 0, "lost_packets": 0},
        }
    )

    assert summary.total_lost_packets == 7
    assert summary.avg_loss_percent == pytest.approx(1.5)


def test_out_of_order_sums_streams_then_falls_back_to_sum() -> None:
    streams = _summary(
        {
            "sum": {"out_of_order": 99},
            "streams": [
                {"udp": {"out_of_order": 2}},
                {"udp": {"out_of_order": None}},
                {"udp": {"out_of_order": 3}},
            ],
        }
    )
    no_stream_totals = _summary(
        {"sum": {"out_of_order": 4}, "streams": [{"udp": {"out_of_order": 0}}]}
    )

    assert streams.total_out_of_order == 5
    assert no_stream_totals.total_out_of_order == 4


def test_end_section_is_read_dynamically_from_the_shared_document() -> None:
    data: dict[str, Any] = {"end": {"sum_sent": {"bytes": 100}}}
    summary = Iperf3EndSummary(data)

    data["end"]["sum_sent"]["bytes"] = 500

    assert summary.total_bytes_sent == 500
