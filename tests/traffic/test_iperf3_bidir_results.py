"""Regression tests for bidirectional (``--bidir``) iperf3 result aggregation.

iperf3 emits the reverse channel of a ``--bidir`` test under
``sum_bidir_reverse`` / ``sum_sent_bidir_reverse`` /
``sum_received_bidir_reverse``. Ignoring those fields reported roughly half of
the bidirectional throughput and only one direction's byte, packet, loss and
jitter figures.

These tests use synthetic JSON shaped like iperf3 3.20 client output; no real
iperf3 run is required.
"""

import json

import pytest

from satgonetem.traffic.iperf3_utils import Iperf3Config, Iperf3Results

# Forward: 125 MB per 5 s interval = 200 Mbps.
# Reverse: 250 MB per 5 s interval = 400 Mbps.
# Aggregate over both intervals: (250 MB + 500 MB) * 8 / 10 s = 600 Mbps.
_TCP_BIDIR_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "TCP",
                "duration": 10,
                "num_streams": 1,
                "bidir": 1,
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
                    "retransmits": 5,
                    "sender": True,
                },
                "sum_bidir_reverse": {
                    "start": 0.0,
                    "end": 5.0,
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                    "sender": False,
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
                    "retransmits": 5,
                    "sender": True,
                },
                "sum_bidir_reverse": {
                    "start": 5.0,
                    "end": 10.0,
                    "seconds": 5.0,
                    "bytes": 250_000_000,
                    "bits_per_second": 400_000_000.0,
                    "sender": False,
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

# Forward sender: 1.25 MB per 1 s interval = 10 Mbps (no loss/jitter).
# Reverse receiver: 2.5 MB per 1 s interval = 20 Mbps.
# Aggregate throughput: (2.5 MB + 5 MB) * 8 / 2 s = 30 Mbps.
# Receiver summaries across both directions: 1720 + 3410 transmitted packets,
# 20 + 10 lost packets, 0.5 ms and 0.2 ms jitter. iperf3 reports the sender's
# transmitted count in ``packets``; ``lost_packets`` is a subset.
_UDP_BIDIR_JSON = json.dumps(
    {
        "start": {
            "test_start": {
                "protocol": "UDP",
                "duration": 2,
                "num_streams": 1,
                "bidir": 1,
            }
        },
        "intervals": [
            {
                "streams": [],
                "sum": {
                    "start": 0.0,
                    "end": 1.0,
                    "seconds": 1.0,
                    "bytes": 1_250_000,
                    "bits_per_second": 10_000_000.0,
                    "packets": 893,
                    "sender": True,
                },
                "sum_bidir_reverse": {
                    "start": 0.0,
                    "end": 1.0,
                    "seconds": 1.0,
                    "bytes": 2_500_000,
                    "bits_per_second": 20_000_000.0,
                    "packets": 1786,
                    "jitter_ms": 0.4,
                    "lost_packets": 10,
                    "lost_percent": 0.56,
                    "sender": False,
                },
            },
            {
                "streams": [],
                "sum": {
                    "start": 1.0,
                    "end": 2.0,
                    "seconds": 1.0,
                    "bytes": 1_250_000,
                    "bits_per_second": 10_000_000.0,
                    "packets": 893,
                    "sender": True,
                },
                "sum_bidir_reverse": {
                    "start": 1.0,
                    "end": 2.0,
                    "seconds": 1.0,
                    "bytes": 2_500_000,
                    "bits_per_second": 20_000_000.0,
                    "packets": 1786,
                    "jitter_ms": 0.4,
                    "lost_packets": 10,
                    "lost_percent": 0.56,
                    "sender": False,
                },
            },
        ],
        "end": {
            "sum": {
                "bytes": 2_500_000,
                "packets": 1786,
                "jitter_ms": 0.0,
                "lost_packets": 0,
                "lost_percent": 0.0,
            },
            "sum_sent": {
                "bytes": 2_500_000,
                "packets": 1786,
                "jitter_ms": 0.0,
                "lost_packets": 0,
                "lost_percent": 0.0,
            },
            "sum_received": {
                "bytes": 2_400_000,
                "packets": 1720,
                "jitter_ms": 0.5,
                "lost_packets": 20,
                "lost_percent": 1.16,
            },
            "sum_bidir_reverse": {
                "bytes": 4_900_000,
                "packets": 3500,
                "jitter_ms": 0.2,
                "lost_packets": 10,
                "lost_percent": 0.29,
            },
            "sum_sent_bidir_reverse": {
                "bytes": 4_900_000,
                "packets": 3500,
                "jitter_ms": 0.0,
                "lost_packets": 0,
                "lost_percent": 0.0,
            },
            "sum_received_bidir_reverse": {
                "bytes": 4_800_000,
                "packets": 3410,
                "jitter_ms": 0.2,
                "lost_packets": 10,
                "lost_percent": 0.29,
            },
        },
    }
)


def _tcp_bidir_results() -> Iperf3Results:
    return Iperf3Results(raw_json=_TCP_BIDIR_JSON, config=Iperf3Config(bidir=True))


def _udp_bidir_results() -> Iperf3Results:
    return Iperf3Results(
        raw_json=_UDP_BIDIR_JSON,
        config=Iperf3Config(protocol="UDP", bidir=True),
    )


class TestTcpBidirAggregation:
    """TCP bidirectional runs aggregate sender and reverse-sender totals."""

    def test_avg_throughput_aggregates_both_directions(self):
        res = _tcp_bidir_results()

        # Forward-only aggregation would report 200 Mbps.
        assert res.avg_throughput_mbps == pytest.approx(600.0)

    def test_total_bytes_sent_aggregates_both_directions(self):
        res = _tcp_bidir_results()

        assert res.total_bytes_sent == 750_000_000

    def test_total_bytes_received_aggregates_both_directions(self):
        res = _tcp_bidir_results()

        assert res.total_bytes_received == 720_000_000

    def test_total_retransmits_aggregates_both_directions(self):
        res = _tcp_bidir_results()

        assert res.total_retransmits == 14

    def test_max_and_min_throughput_aggregate_both_directions(self):
        res = _tcp_bidir_results()

        # Each interval carries 200 Mbps forward + 400 Mbps reverse.
        assert res.max_throughput_mbps == pytest.approx(600.0)
        assert res.min_throughput_mbps == pytest.approx(600.0)
        assert res.max_throughput_mbps >= res.avg_throughput_mbps


class TestUdpBidirAggregation:
    """UDP bidirectional runs aggregate receiver metrics from both directions."""

    def test_avg_throughput_aggregates_both_directions(self):
        res = _udp_bidir_results()

        # Forward-only aggregation would report 10 Mbps.
        assert res.avg_throughput_mbps == pytest.approx(30.0)

    def test_total_bytes_aggregates_both_receivers(self):
        res = _udp_bidir_results()

        assert res.total_bytes == 7_200_000

    def test_total_packets_counts_transmitted_packets_across_both_receivers(self):
        res = _udp_bidir_results()

        # iperf3's receiver summary reports transmitted packets (1720 + 3410);
        # lost packets are a subset and are not added again.
        assert res.total_packets == 5130

    def test_total_lost_packets_aggregates_both_receivers(self):
        res = _udp_bidir_results()

        assert res.total_lost_packets == 30

    def test_avg_loss_percent_is_recombined_from_both_directions(self):
        res = _udp_bidir_results()

        # iperf3 defines loss over transmitted packets: 30 lost out of 5130
        # transmitted, not 1.16 % + 0.29 % and not 30 / 5100.
        assert res.avg_loss_percent == pytest.approx(30 / 5130 * 100)
        assert res.avg_loss_percent == pytest.approx(
            100 * res.total_lost_packets / res.total_packets
        )

    def test_avg_jitter_is_packet_weighted_across_directions(self):
        res = _udp_bidir_results()

        # (0.5 ms * 1700 received + 0.2 ms * 3400 received) / 5100 = 0.3 ms.
        assert res.avg_jitter_ms == pytest.approx(0.3)


class TestNonBidirUnchanged:
    """JSON without reverse summaries keeps the one-direction semantics."""

    def test_without_reverse_fields_only_forward_metrics_are_used(self):
        payload = json.loads(_TCP_BIDIR_JSON)
        for key in ("sum_sent_bidir_reverse", "sum_received_bidir_reverse"):
            del payload["end"][key]
        for interval in payload["intervals"]:
            del interval["sum_bidir_reverse"]

        res = Iperf3Results(raw_json=json.dumps(payload), config=Iperf3Config())

        assert res.avg_throughput_mbps == pytest.approx(200.0)
        assert res.total_bytes_sent == 250_000_000
        assert res.total_bytes_received == 240_000_000
        assert res.total_retransmits == 10
        assert res.max_throughput_mbps == pytest.approx(200.0)
        assert res.min_throughput_mbps == pytest.approx(200.0)
