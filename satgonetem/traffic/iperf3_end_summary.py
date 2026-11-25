"""Computed metrics over the ``end`` section of an iperf3 JSON document."""

from __future__ import annotations

from typing import Any


class Iperf3EndSummary:
    """Pure value object over the aggregated iperf3 ``end`` summaries.

    iperf3 appends ``_bidir_reverse`` to a summary key for the reverse channel
    of a ``--bidir`` run (for example ``sum_sent_bidir_reverse``). Pair
    accessors sum the forward and reverse values when that counterpart exists;
    non-bidirectional output has no such key, so only the forward value is
    used.

    Args:
        data: The complete parsed iperf3 JSON document. The ``end`` section is
            read on every access, so callers sharing the raw dict (for example
            through ``Iperf3Results.to_json()["raw"]``) observe updates.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def _end(self) -> dict[str, Any]:
        return self._data.get("end", {})

    def value(self, key: str, field: str) -> float:
        """Return a numeric field from an end summary, defaulting to 0.0."""
        value = self._end().get(key, {}).get(field, 0)
        return float(value or 0.0)

    def pair_value(self, key: str, field: str) -> float:
        """Sum a field over a forward summary and its reverse counterpart."""
        value = self.value(key, field)
        reverse_key = f"{key}_bidir_reverse"
        if reverse_key in self._end():
            value += self.value(reverse_key, field)
        return value

    def is_bidir(self) -> bool:
        """Return True when the end section carries a reverse summary."""
        return any(key.endswith("_bidir_reverse") for key in self._end())

    @property
    def total_bytes_sent(self) -> int:
        """Total bytes sent, across both directions for --bidir runs."""
        return int(self.pair_value("sum_sent", "bytes"))

    @property
    def total_bytes_received(self) -> int:
        """Total bytes received, across both directions for --bidir runs."""
        return int(self.pair_value("sum_received", "bytes"))

    @property
    def total_retransmits(self) -> int:
        """Total TCP retransmissions across the entire test."""
        return int(self.pair_value("sum_sent", "retransmits"))

    @property
    def total_bytes(self) -> int:
        """Total UDP payload bytes, preferring the receiver summary."""
        for key in ("sum_received", "sum_sent", "sum"):
            value = self.pair_value(key, "bytes")
            if value:
                return int(value)
        return 0

    def _bidir_udp_keys(self) -> tuple[str, ...]:
        end = self._end()
        if "sum_bidir_reverse" in end:
            return ("sum_received", "sum")
        return ("sum_received",)

    @property
    def total_packets(self) -> int:
        """Total UDP packets transmitted, across both directions for --bidir.

        For ``--bidir`` runs the receiver summary reports the sender's
        transmitted count in ``packets``; ``lost_packets`` is a subset and must
        not be added again. ``lost_packets`` is only a fallback when the sender
        count is unavailable. For non-bidirectional output iperf3's
        ``sum.packets`` already reports the sender's transmitted count.
        """
        if self.is_bidir():
            for key in self._bidir_udp_keys():
                packets = self.pair_value(key, "packets")
                lost = self.pair_value(key, "lost_packets")
                if packets or lost:
                    return int(packets or lost)
        return int(self.value("sum", "packets"))

    @property
    def avg_jitter_ms(self) -> float:
        """Mean one-way jitter in milliseconds (UDP)."""
        if self.is_bidir():
            for key in self._bidir_udp_keys():
                rev_key = f"{key}_bidir_reverse"
                forward_packets = self.value(key, "packets")
                forward_lost = self.value(key, "lost_packets")
                reverse_packets = self.value(rev_key, "packets")
                reverse_lost = self.value(rev_key, "lost_packets")
                forward_received = max(forward_packets - forward_lost, 0)
                reverse_received = max(reverse_packets - reverse_lost, 0)
                weighted_packets = forward_received + reverse_received
                if weighted_packets > 0:
                    weighted = (
                        self.value(key, "jitter_ms") * forward_received
                        + self.value(rev_key, "jitter_ms") * reverse_received
                    )
                    return weighted / weighted_packets
        return self.value("sum", "jitter_ms")

    @property
    def total_lost_packets(self) -> int:
        """Total lost UDP packets, across both directions for --bidir."""
        if self.is_bidir():
            for key in self._bidir_udp_keys():
                received = self.pair_value(key, "packets")
                lost = self.pair_value(key, "lost_packets")
                if received or lost:
                    return int(lost)
        return int(self.value("sum", "lost_packets"))

    @property
    def avg_loss_percent(self) -> float:
        """Average UDP packet loss percentage, iperf3-style.

        iperf3 defines ``lost_percent`` as ``100 * lost / packets`` where
        ``packets`` is the sender's transmitted count; ``lost_packets`` is
        already included in that count and is not added to the denominator.
        """
        if self.is_bidir():
            for key in self._bidir_udp_keys():
                packets = self.pair_value(key, "packets")
                lost = self.pair_value(key, "lost_packets")
                transmitted = packets or lost
                if transmitted > 0:
                    return 100.0 * lost / transmitted
        return self.value("sum", "lost_percent")

    @property
    def total_out_of_order(self) -> int:
        """Total out-of-order UDP packets, aggregated across end streams."""
        end = self._end()
        streams = list(end.get("streams", [])) + list(
            end.get("streams_bidir_reverse", [])
        )
        total = sum(
            int(stream.get("udp", {}).get("out_of_order", 0) or 0) for stream in streams
        )
        if total:
            return total
        return int(self.pair_value("sum", "out_of_order"))


__all__ = ["Iperf3EndSummary"]
