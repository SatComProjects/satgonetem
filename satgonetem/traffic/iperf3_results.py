from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_end_summary import Iperf3EndSummary
from satgonetem.traffic.iperf3_intervals import BIDIR_REVERSE_LABELS, Iperf3IntervalData
from satgonetem.traffic.iperf3_plotting import Iperf3Plotter
from satgonetem.traffic.iperf3_report import format_repr, format_summary, summary_dict
from satgonetem.traffic.iperf3_units import (
    BPS_TO_MBPS,
    BYTES_TO_KB,
    BYTES_TO_MB,
    USEC_TO_MS,
)

if TYPE_CHECKING:
    import pandas as pd


class Iperf3Results:
    """Parsed results from an iperf3 JSON output.

    Provides typed property getters for all TCP and UDP metrics, interval-
    level DataFrames for time-series analysis, and methods to plot
    individual metrics to files.

    All summary metrics are derived from the 'end' section of the iperf3
    JSON (the most reliable aggregated values). Time-series properties pull
    from the flattened interval DataFrames.

    Bidirectional (``--bidir``) output is aggregated across both directions:
    the forward summary and its ``*_bidir_reverse`` counterpart are combined
    so throughput, byte, packet, retransmit, loss, and jitter figures are not
    silently reduced to one direction's share.
    """

    # Backward-compatible aliases; canonical values live in iperf3_units and
    # iperf3_intervals.
    _BPS_TO_MBPS: float = BPS_TO_MBPS
    _BYTES_TO_MB: float = BYTES_TO_MB
    _BYTES_TO_KB: float = BYTES_TO_KB
    _USEC_TO_MS: float = USEC_TO_MS
    _BIDIR_REVERSE_LABELS: dict[str, str] = BIDIR_REVERSE_LABELS

    def __init__(self, raw_json: str, config: Iperf3Config) -> None:
        """
        Args:
            raw_json: Raw JSON string from iperf3 --json client output.
            config: The Iperf3Config instance used to produce this result.

        Raises:
            ValueError: If raw_json is empty or cannot be parsed as JSON.
        """
        if not raw_json or not raw_json.strip():
            raise ValueError("iperf3 returned empty output; the flow may have failed.")
        try:
            self._data: dict = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse iperf3 JSON output: {exc}") from exc

        self._config = config
        self._protocol: str = str(
            self._data.get("start", {})
            .get("test_start", {})
            .get("protocol", config.protocol)
        ).upper()
        self._intervals = Iperf3IntervalData(self._data)
        self._df_streams = self._intervals.df_streams
        self._df_summaries = self._intervals.df_summaries
        self._end_summary = Iperf3EndSummary(self._data)

        # Set by Iperf3Flow: which side's JSON this is ("client" or "server")
        # and what iperf3 printed around the client's JSON (its warnings).
        self.json_side: str | None = None
        self.client_notes: str | None = None

    # ---- Metadata ----

    @property
    def protocol(self) -> str:
        """Transport protocol used in the test: 'TCP' or 'UDP'."""
        return self._protocol

    @property
    def duration_seconds(self) -> float:
        """Nominal test duration reported by iperf3 in seconds."""
        return float(
            self._data.get("start", {})
            .get("test_start", {})
            .get("duration", self._config.duration)
        )

    @property
    def num_streams(self) -> int:
        """Number of parallel streams used in the test."""
        return int(
            self._data.get("start", {})
            .get("test_start", {})
            .get("num_streams", self._config.parallel)
        )

    # ---- Shared throughput metrics (TCP + UDP) ----

    @property
    def avg_throughput_mbps(self) -> float:
        """Mean throughput in Mbps, aggregating both directions for --bidir."""
        return self._intervals.avg_throughput_mbps()

    @property
    def max_throughput_mbps(self) -> float:
        """Peak interval throughput in Mbps."""
        return self._intervals.max_throughput_mbps()

    @property
    def min_throughput_mbps(self) -> float:
        """Minimum interval throughput in Mbps."""
        return self._intervals.min_throughput_mbps()

    @property
    def pmtu(self) -> int:
        """Path MTU in bytes taken from the last stream interval."""
        return self._intervals.pmtu()

    # ---- TCP-specific metrics ----

    @property
    def total_bytes_sent(self) -> int:
        """Total bytes sent (TCP), across both directions for --bidir runs."""
        return self._end_summary.total_bytes_sent

    @property
    def total_bytes_received(self) -> int:
        """Total bytes received (TCP), across both directions for --bidir runs."""
        return self._end_summary.total_bytes_received

    @property
    def total_retransmits(self) -> int:
        """Total TCP retransmissions across the entire test."""
        return self._end_summary.total_retransmits

    @property
    def avg_rtt_ms(self) -> float:
        """Mean TCP round-trip time across all stream intervals in ms."""
        return self._intervals.avg_rtt_ms()

    @property
    def max_rtt_ms(self) -> float:
        """Maximum TCP round-trip time across all stream intervals in ms."""
        return self._intervals.max_rtt_ms()

    @property
    def avg_rtt_var_us(self) -> float:
        """Mean TCP RTT variance in microseconds."""
        return self._intervals.avg_rtt_var_us()

    @property
    def avg_cwnd_bytes(self) -> float:
        """Mean TCP send congestion window size in bytes."""
        return self._intervals.avg_cwnd_bytes()

    @property
    def avg_snd_wnd_bytes(self) -> float:
        """Mean TCP send window size in bytes."""
        return self._intervals.avg_snd_wnd_bytes()

    # ---- UDP-specific metrics ----

    @property
    def total_bytes(self) -> int:
        """Total UDP payload bytes, preferring the receiver summary."""
        return self._end_summary.total_bytes

    @property
    def total_packets(self) -> int:
        """Total UDP packets, preferring the receiver summaries."""
        return self._end_summary.total_packets

    @property
    def avg_jitter_ms(self) -> float:
        """Mean one-way jitter in milliseconds (UDP)."""
        return self._end_summary.avg_jitter_ms

    @property
    def total_lost_packets(self) -> int:
        """Total lost UDP packets."""
        return self._end_summary.total_lost_packets

    @property
    def avg_loss_percent(self) -> float:
        """Average UDP packet loss percentage."""
        return self._end_summary.avg_loss_percent

    @property
    def total_out_of_order(self) -> int:
        """Total out-of-order UDP packets, aggregated across end streams."""
        return self._end_summary.total_out_of_order

    # ---- DataFrame accessors ----

    def get_interval_dataframe(self) -> pd.DataFrame:
        """Return per-stream interval data as a copy of the internal DataFrame."""
        return self._intervals.interval_dataframe()

    def get_summary_dataframe(self) -> pd.DataFrame:
        """Return per-interval summary data as a copy of the internal DataFrame."""
        return self._intervals.summary_dataframe()

    # ---- Plotting ----

    def plot_metric(
        self,
        metric: str,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        use_summary: bool = True,
        fmt: str = "png",
    ) -> str | None:
        """Plot one metric; see :class:`Iperf3Plotter` for details."""
        return Iperf3Plotter(
            self._protocol, self._df_streams, self._df_summaries
        ).plot_metric(metric, output_dir, file_stem, use_summary, fmt)

    def plot_all(
        self,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        fmt: str = "png",
    ) -> list[str]:
        """Plot every metric available for the protocol."""
        return Iperf3Plotter(
            self._protocol, self._df_streams, self._df_summaries
        ).plot_all(output_dir, file_stem, fmt)

    def print_summary(self) -> None:
        """Print a human-readable summary of all test results to stdout."""
        print(format_summary(self), end="")

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of iperf3 results and configuration.

        The 'summary' key holds the computed scalar metrics derived from the
        iperf3 'end' section. Protocol-specific keys are included only for the
        relevant protocol. The 'raw' key holds the complete iperf3 JSON output
        as the parsed dict, by reference.

        Returns:
            A dict with keys 'config', 'summary', and 'raw'. Suitable for
            passing directly to json.dumps().
        """
        return summary_dict(self, self._config, self._data)

    def __repr__(self) -> str:
        return format_repr(self)
