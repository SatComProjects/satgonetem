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

    @property
    def total_packets(self) -> int:
        """Total UDP packets, preferring receiver summaries for --bidir."""
        if self.is_bidir():
            received = self.pair_value("sum_received", "packets")
            if received:
                return int(received)
        return int(self.value("sum", "packets"))

    @property
    def avg_jitter_ms(self) -> float:
        """Mean one-way jitter in milliseconds (UDP)."""
        if self.is_bidir():
            forward_packets = self.value("sum_received", "packets")
            reverse_packets = self.value("sum_received_bidir_reverse", "packets")
            total_packets = forward_packets + reverse_packets
            if total_packets > 0:
                weighted = (
                    self.value("sum_received", "jitter_ms") * forward_packets
                    + self.value("sum_received_bidir_reverse", "jitter_ms")
                    * reverse_packets
                )
                return weighted / total_packets
        return self.value("sum", "jitter_ms")

    @property
    def total_lost_packets(self) -> int:
        """Total lost UDP packets."""
        if self.is_bidir():
            received = self.pair_value("sum_received", "packets")
            if received:
                return int(self.pair_value("sum_received", "lost_packets"))
        return int(self.value("sum", "lost_packets"))

    @property
    def avg_loss_percent(self) -> float:
        """Average UDP packet loss percentage."""
        if self.is_bidir():
            received = self.pair_value("sum_received", "packets")
            if received > 0:
                lost = self.pair_value("sum_received", "lost_packets")
                return 100.0 * lost / received
        return self.value("sum", "lost_percent")

    @property
    def total_out_of_order(self) -> int:
        """Total out-of-order UDP packets, aggregated across end streams."""
        end = self._end()
        total = sum(
            int(stream.get("udp", {}).get("out_of_order", 0) or 0)
            for stream in end.get("streams", [])
        )
        if total:
            return total
        return int(end.get("sum", {}).get("out_of_order", 0) or 0)


__all__ = ["Iperf3EndSummary"]
