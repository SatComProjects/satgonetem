"""Stateless presentation helpers for parsed iperf3 results."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Protocol

from satgonetem.traffic.iperf3_units import BYTES_TO_KB, BYTES_TO_MB

if TYPE_CHECKING:
    from satgonetem.traffic.iperf3_config import Iperf3Config


class _SummarySource(Protocol):
    """Structural view of the computed metrics a presenter consumes."""

    @property
    def protocol(self) -> str: ...

    @property
    def duration_seconds(self) -> float: ...

    @property
    def num_streams(self) -> int: ...

    @property
    def avg_throughput_mbps(self) -> float: ...

    @property
    def max_throughput_mbps(self) -> float: ...

    @property
    def min_throughput_mbps(self) -> float: ...

    @property
    def pmtu(self) -> int: ...

    @property
    def total_bytes_sent(self) -> int: ...

    @property
    def total_bytes_received(self) -> int: ...

    @property
    def total_retransmits(self) -> int: ...

    @property
    def avg_rtt_ms(self) -> float: ...

    @property
    def max_rtt_ms(self) -> float: ...

    @property
    def avg_rtt_var_us(self) -> float: ...

    @property
    def avg_cwnd_bytes(self) -> float: ...

    @property
    def avg_snd_wnd_bytes(self) -> float: ...

    @property
    def total_bytes(self) -> int: ...

    @property
    def total_packets(self) -> int: ...

    @property
    def avg_jitter_ms(self) -> float: ...

    @property
    def total_lost_packets(self) -> int: ...

    @property
    def avg_loss_percent(self) -> float: ...

    @property
    def total_out_of_order(self) -> int: ...


def summary_dict(
    source: _SummarySource,
    config: Iperf3Config,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Build the JSON-serializable result document.

    Args:
        source: Object exposing the computed scalar metrics.
        config: Configuration used to produce the result.
        raw: The parsed iperf3 JSON. Returned by reference, not copied.

    Returns:
        A dict with 'config', 'summary', and 'raw' keys.
    """
    summary: dict[str, Any] = {
        "protocol": source.protocol,
        "duration_seconds": source.duration_seconds,
        "num_streams": source.num_streams,
        "avg_throughput_mbps": source.avg_throughput_mbps,
        "max_throughput_mbps": source.max_throughput_mbps,
        "min_throughput_mbps": source.min_throughput_mbps,
        "pmtu": source.pmtu,
    }
    if source.protocol == "TCP":
        summary.update(
            {
                "total_bytes_sent": source.total_bytes_sent,
                "total_bytes_received": source.total_bytes_received,
                "total_retransmits": source.total_retransmits,
                "avg_rtt_ms": source.avg_rtt_ms,
                "max_rtt_ms": source.max_rtt_ms,
                "avg_rtt_var_us": source.avg_rtt_var_us,
                "avg_cwnd_bytes": source.avg_cwnd_bytes,
                "avg_snd_wnd_bytes": source.avg_snd_wnd_bytes,
            }
        )
    elif source.protocol == "UDP":
        summary.update(
            {
                "total_bytes": source.total_bytes,
                "total_packets": source.total_packets,
                "avg_jitter_ms": source.avg_jitter_ms,
                "total_lost_packets": source.total_lost_packets,
                "avg_loss_percent": source.avg_loss_percent,
                "total_out_of_order": source.total_out_of_order,
            }
        )
    return {
        "config": {
            "protocol": config.protocol,
            "duration": config.duration,
            "bandwidth_mbps": config.bandwidth_mbps,
            "parallel": config.parallel,
            "interval": config.interval,
            "port": config.port,
        },
        "summary": summary,
        "raw": raw,
    }


def format_summary(source: _SummarySource) -> str:
    """Format the human-readable test summary.

    Returns:
        The complete summary text, including a trailing newline.
    """
    proto = source.protocol
    out = io.StringIO()
    print("=" * 60, file=out)
    print(f"          {proto} IPERF3 TEST RESULTS", file=out)
    print("=" * 60, file=out)
    print(f"Protocol:              {proto}", file=out)
    print(f"Duration:              {source.duration_seconds:.1f} s", file=out)
    print(f"Parallel streams:      {source.num_streams}", file=out)
    print(f"Avg Throughput:        {source.avg_throughput_mbps:.2f} Mbps", file=out)
    print(f"Max Throughput:        {source.max_throughput_mbps:.2f} Mbps", file=out)
    print(f"Min Throughput:        {source.min_throughput_mbps:.2f} Mbps", file=out)
    if proto == "TCP":
        print(
            f"Total Bytes Sent:      {source.total_bytes_sent / BYTES_TO_MB:.2f} MB",
            file=out,
        )
        print(
            f"Total Bytes Received:  {source.total_bytes_received / BYTES_TO_MB:.2f} MB",
            file=out,
        )
        print(f"Total Retransmits:     {source.total_retransmits}", file=out)
        print(f"Avg RTT:               {source.avg_rtt_ms:.2f} ms", file=out)
        print(f"Max RTT:               {source.max_rtt_ms:.2f} ms", file=out)
        print(
            f"Avg Send CWnd:         {source.avg_cwnd_bytes / BYTES_TO_KB:.2f} KB",
            file=out,
        )
    elif proto == "UDP":
        print(
            f"Total Bytes:           {source.total_bytes / BYTES_TO_MB:.2f} MB",
            file=out,
        )
        print(f"Total Packets:         {source.total_packets}", file=out)
        print(f"Avg Jitter:            {source.avg_jitter_ms:.3f} ms", file=out)
        print(f"Lost Packets:          {source.total_lost_packets}", file=out)
        print(f"Avg Loss:              {source.avg_loss_percent:.2f} %", file=out)
        print(f"Out of Order:          {source.total_out_of_order}", file=out)
    if source.pmtu:
        print(f"Path MTU:              {source.pmtu} bytes", file=out)
    print("=" * 60, file=out)
    return out.getvalue()


def format_repr(source: _SummarySource) -> str:
    """Format the compact object representation."""
    return (
        f"Iperf3Results(protocol={source.protocol!r}, "
        f"avg_throughput_mbps={source.avg_throughput_mbps:.2f})"
    )
