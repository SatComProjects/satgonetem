from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import matplotlib
import pandas as pd

from satgonetem.traffic.iperf3_config import Iperf3Config

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

if TYPE_CHECKING:
    pass


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

    _BPS_TO_MBPS: float = 1_000_000.0
    _BYTES_TO_MB: float = 1_000_000.0
    _BYTES_TO_KB: float = 1_000.0
    _USEC_TO_MS: float = 1_000.0

    # iperf3 names the reverse channel of a --bidir test by appending this
    # suffix to the forward summary label. The mapping is used to pair the
    # interval summaries of both directions.
    _BIDIR_REVERSE_LABELS: dict[str, str] = {
        "sum": "sum_bidir_reverse",
        "sum_sent": "sum_sent_bidir_reverse",
        "sum_received": "sum_received_bidir_reverse",
    }

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
        self._df_streams, self._df_summaries = self._flatten_intervals()

        # Set by Iperf3Flow: which side's JSON this is ("client" or "server")
        # and what iperf3 printed around the client's JSON (its warnings).
        self.json_side: str | None = None
        self.client_notes: str | None = None

    def _flatten_intervals(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Flatten iperf3 intervals into per-stream and per-summary DataFrames.

        Returns:
            A 2-tuple (df_streams, df_summaries). Each row in df_streams
            represents one stream measurement for one interval, annotated
            with _t (end timestamp) and _label (socket identifier). Rows in
            df_summaries represent the aggregated sum/sum_sent/sum_received
            entries per interval.
        """
        intervals = self._data.get("intervals", [])
        stream_rows: list[dict] = []
        sum_rows: list[dict] = []

        for it in intervals:
            for s in it.get("streams", []):
                if s.get("omitted", False):
                    continue
                row = dict(s)
                row["_t"] = s.get("end", s.get("start"))
                row["_label"] = f"Socket {s.get('socket', '?')}"
                stream_rows.append(row)
            for sum_key in (
                "sum",
                "sum_sent",
                "sum_received",
                "sum_bidir",
                "sum_bidir_reverse",
            ):
                if sum_key in it:
                    entry = dict(it[sum_key])
                    if entry.get("omitted", False):
                        continue
                    entry["_t"] = entry.get("end", entry.get("start"))
                    entry["_label"] = sum_key
                    sum_rows.append(entry)

        df_streams = pd.DataFrame(stream_rows) if stream_rows else pd.DataFrame()
        df_summaries = pd.DataFrame(sum_rows) if sum_rows else pd.DataFrame()
        return df_streams, df_summaries

    def _end(self) -> dict:
        """Return the 'end' section of the iperf3 JSON result.

        Returns:
            The end summary dict, or an empty dict if absent.
        """
        return self._data.get("end", {})

    def _end_value(self, key: str, field: str) -> float:
        """Return a numeric field from an 'end' summary, defaulting to 0.0.

        Args:
            key: Summary key in the end section (e.g. 'sum_sent').
            field: Field within that summary (e.g. 'bytes').

        Returns:
            The field as a float, or 0.0 when the key/field is absent or null.
        """
        value = self._end().get(key, {}).get(field, 0)
        return float(value or 0.0)

    def _end_pair_value(self, key: str, field: str) -> float:
        """Sum a field over a forward summary and its reverse counterpart.

        iperf3 appends ``_bidir_reverse`` to the summary key for the reverse
        channel of a ``--bidir`` run (e.g. ``sum_sent_bidir_reverse``).
        Non-bidirectional output has no such key, so only the forward value
        is returned.

        Args:
            key: Forward summary key (e.g. 'sum_sent').
            field: Field within the summary (e.g. 'bytes').

        Returns:
            Forward plus reverse field value as a float.
        """
        value = self._end_value(key, field)
        reverse_key = f"{key}_bidir_reverse"
        if reverse_key in self._end():
            value += self._end_value(reverse_key, field)
        return value

    def _is_bidir(self) -> bool:
        """Return True when the end section carries a reverse summary.

        Returns:
            True for ``--bidir`` output, False for one-direction results.
        """
        return any(key.endswith("_bidir_reverse") for key in self._end())

    def _col_mean(self, df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        """Return the mean of a DataFrame column, falling back to default.

        Args:
            df: Source DataFrame.
            col: Column name to aggregate.
            default: Value returned when the column is absent or fully null.

        Returns:
            The column mean as a float, or default.
        """
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.mean()) if not s.empty else default

    def _col_max(self, df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        """Return the max of a DataFrame column, falling back to default.

        Args:
            df: Source DataFrame.
            col: Column name to aggregate.
            default: Value returned when the column is absent or fully null.

        Returns:
            The column maximum as a float, or default.
        """
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.max()) if not s.empty else default

    def _col_min(self, df: pd.DataFrame, col: str, default: float = 0.0) -> float:
        """Return the min of a DataFrame column, falling back to default.

        Args:
            df: Source DataFrame.
            col: Column name to aggregate.
            default: Value returned when the column is absent or fully null.

        Returns:
            The column minimum as a float, or default.
        """
        if df.empty or col not in df.columns:
            return default
        s = df[col].dropna()
        return float(s.min()) if not s.empty else default

    def _primary_df(self) -> pd.DataFrame:
        """Return the best available interval DataFrame for scalar aggregates.

        Returns:
            df_summaries when it has rows, otherwise df_streams.
        """
        return self._df_summaries if not self._df_summaries.empty else self._df_streams

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

    def _summary_series(self, label: str | None = None) -> pd.DataFrame | None:
        """Return one consistent interval summary series, if available.

        Prefers the aggregate ``sum`` rows, then sender/receiver summaries.
        Using one label avoids mixing three different rate series per interval.

        Args:
            label: Optional explicit summary label to select instead of the
                default preference order.

        Returns:
            The selected summary DataFrame, or None when no summary is present.
        """
        if self._df_summaries.empty or "_label" not in self._df_summaries.columns:
            return None
        candidates = (
            (label,) if label is not None else ("sum", "sum_received", "sum_sent")
        )
        for candidate in candidates:
            series = self._df_summaries[self._df_summaries["_label"] == candidate]
            if not series.empty:
                return series
        return None

    def _bidir_reverse_series(self, series: pd.DataFrame | None) -> pd.DataFrame | None:
        """Return the reverse-direction interval series matching *series*.

        iperf3 names the reverse channel of a ``--bidir`` run by appending
        ``_bidir_reverse`` to the forward summary label.

        Args:
            series: A summary series returned by :meth:`_summary_series`.

        Returns:
            The matching reverse-direction DataFrame, or None when the input
            has no bidirectional counterpart.
        """
        if series is None or series.empty or "_label" not in series.columns:
            return None
        reverse_label = self._BIDIR_REVERSE_LABELS.get(str(series["_label"].iloc[0]))
        if reverse_label is None:
            return None
        return self._summary_series(reverse_label)

    def _series_throughput_mbps(self, series: pd.DataFrame | None) -> float | None:
        """Return the mean throughput of one interval series in Mbps.

        Args:
            series: Summary rows for one direction.

        Returns:
            Throughput in Mbps, or None when no series was supplied.
        """
        if series is None or series.empty:
            return None
        if "bytes" in series.columns and "seconds" in series.columns:
            total_seconds = float(series["seconds"].sum())
            if total_seconds > 0:
                return (
                    float(series["bytes"].sum())
                    * 8.0
                    / total_seconds
                    / self._BPS_TO_MBPS
                )
        return self._col_mean(series, "bits_per_second") / self._BPS_TO_MBPS

    def _series_interval_rates(self, series: pd.DataFrame | None) -> list[float]:
        """Return one throughput value per reporting interval, in Mbps.

        Args:
            series: Summary rows for one direction.

        Returns:
            Per-interval rates in Mbps, or an empty list when the series has
            no usable rate data.
        """
        if series is None or series.empty:
            return []
        if "bytes" in series.columns and "seconds" in series.columns:
            seconds = series["seconds"].astype(float)
            byte_counts = series["bytes"].astype(float)
            rates = (byte_counts * 8.0 / seconds.where(seconds > 0)) / self._BPS_TO_MBPS
            return [float(value) for value in rates.dropna()]
        if "bits_per_second" in series.columns:
            return [
                float(value) / self._BPS_TO_MBPS
                for value in series["bits_per_second"].dropna()
            ]
        return []

    def _combined_interval_rates(self) -> list[float] | None:
        """Return per-interval forward+reverse rates for a bidirectional run.

        Returns:
            One aggregate rate per interval in Mbps, or None when the result
            has no reverse series or the two direction series cannot be paired.
        """
        series = self._summary_series()
        if series is None:
            return None
        reverse = self._bidir_reverse_series(series)
        if reverse is None:
            return None
        forward_rates = self._series_interval_rates(series)
        reverse_rates = self._series_interval_rates(reverse)
        if not forward_rates or len(forward_rates) != len(reverse_rates):
            return None
        return [
            forward + reverse_rate
            for forward, reverse_rate in zip(forward_rates, reverse_rates, strict=True)
        ]

    @property
    def avg_throughput_mbps(self) -> float:
        """Mean throughput in Mbps, excluding omitted warm-up intervals.

        For bidirectional (``--bidir``) results the forward and reverse
        interval series are both aggregated and their rates are summed, so
        the value is the aggregate throughput of both directions rather than
        one direction's share.
        """
        series = self._summary_series()
        throughput = self._series_throughput_mbps(series)
        if throughput is None:
            return (
                self._col_mean(self._primary_df(), "bits_per_second")
                / self._BPS_TO_MBPS
            )
        reverse = self._series_throughput_mbps(self._bidir_reverse_series(series))
        return throughput + (reverse or 0.0)

    @property
    def max_throughput_mbps(self) -> float:
        """Peak interval throughput in Mbps.

        For bidirectional (``--bidir``) results both directions are summed per
        interval, matching :attr:`avg_throughput_mbps`.
        """
        combined = self._combined_interval_rates()
        if combined:
            return max(combined)
        return self._col_max(self._primary_df(), "bits_per_second") / self._BPS_TO_MBPS

    @property
    def min_throughput_mbps(self) -> float:
        """Minimum interval throughput in Mbps.

        For bidirectional (``--bidir``) results both directions are summed per
        interval, matching :attr:`avg_throughput_mbps`.
        """
        combined = self._combined_interval_rates()
        if combined:
            return min(combined)
        return self._col_min(self._primary_df(), "bits_per_second") / self._BPS_TO_MBPS

    @property
    def pmtu(self) -> int:
        """Path MTU in bytes taken from the last stream interval.

        Returns:
            Path MTU as an integer, or 0 if not present in the data.
        """
        if not self._df_streams.empty and "pmtu" in self._df_streams.columns:
            vals = self._df_streams["pmtu"].dropna()
            if not vals.empty:
                return int(vals.iloc[-1])
        return 0

    # ---- TCP-specific metrics ----

    @property
    def total_bytes_sent(self) -> int:
        """Total bytes sent (TCP), across both directions for --bidir runs.

        Derived from end.sum_sent. When end.sum_sent_bidir_reverse is present
        (``--bidir``), the bytes sent by the reverse-direction sender are
        added, so the value covers both endpoints.
        """
        return int(self._end_pair_value("sum_sent", "bytes"))

    @property
    def total_bytes_received(self) -> int:
        """Total bytes received (TCP), across both directions for --bidir runs.

        Derived from end.sum_received. When end.sum_received_bidir_reverse is
        present (``--bidir``), the bytes received from the reverse direction
        are added, so the value covers both endpoints.
        """
        return int(self._end_pair_value("sum_received", "bytes"))

    @property
    def total_retransmits(self) -> int:
        """Total TCP retransmissions across the entire test.

        For bidirectional (``--bidir``) runs the reverse sender's
        retransmissions (end.sum_sent_bidir_reverse) are included.
        """
        return int(self._end_pair_value("sum_sent", "retransmits"))

    @property
    def avg_rtt_ms(self) -> float:
        """Mean TCP round-trip time across all stream intervals in ms."""
        return self._col_mean(self._df_streams, "rtt") / self._USEC_TO_MS

    @property
    def max_rtt_ms(self) -> float:
        """Maximum TCP round-trip time across all stream intervals in ms."""
        return self._col_max(self._df_streams, "rtt") / self._USEC_TO_MS

    @property
    def avg_rtt_var_us(self) -> float:
        """Mean TCP RTT variance in microseconds."""
        return self._col_mean(self._df_streams, "rttvar")

    @property
    def avg_cwnd_bytes(self) -> float:
        """Mean TCP send congestion window size in bytes."""
        return self._col_mean(self._df_streams, "snd_cwnd")

    @property
    def avg_snd_wnd_bytes(self) -> float:
        """Mean TCP send window size in bytes."""
        return self._col_mean(self._df_streams, "snd_wnd")

    # ---- UDP-specific metrics ----

    @property
    def total_bytes(self) -> int:
        """Total UDP payload bytes, preferring the receiver summary.

        When a reverse-direction counterpart is present (``--bidir``), the
        bytes of both directions are summed, so the value is the payload
        received across the whole bidirectional test.
        """
        for key in ("sum_received", "sum_sent", "sum"):
            value = self._end_pair_value(key, "bytes")
            if value:
                return int(value)
        return 0

    @property
    def total_packets(self) -> int:
        """Total UDP packets, preferring the receiver summaries.

        For bidirectional (``--bidir``) runs this is the sum of both
        direction receivers' packet counts
        (end.sum_received + end.sum_received_bidir_reverse). Otherwise it is
        derived from end.sum, matching iperf3's legacy summary.
        """
        if self._is_bidir():
            received = self._end_pair_value("sum_received", "packets")
            if received:
                return int(received)
        return int(self._end_value("sum", "packets"))

    @property
    def avg_jitter_ms(self) -> float:
        """Mean one-way jitter in milliseconds (UDP).

        For bidirectional (``--bidir``) runs the jitter reported by both
        direction receivers (end.sum_received and
        end.sum_received_bidir_reverse) is combined as a packet-weighted
        mean, so a direction that carried more packets contributes
        proportionally more. Otherwise derived from end.sum.
        """
        if self._is_bidir():
            forward_packets = self._end_value("sum_received", "packets")
            reverse_packets = self._end_value("sum_received_bidir_reverse", "packets")
            total_packets = forward_packets + reverse_packets
            if total_packets > 0:
                weighted = (
                    self._end_value("sum_received", "jitter_ms") * forward_packets
                    + self._end_value("sum_received_bidir_reverse", "jitter_ms")
                    * reverse_packets
                )
                return weighted / total_packets
        return self._end_value("sum", "jitter_ms")

    @property
    def total_lost_packets(self) -> int:
        """Total lost UDP packets.

        For bidirectional (``--bidir``) runs the losses reported by both
        direction receivers (end.sum_received + end.sum_received_bidir_reverse)
        are summed. Otherwise derived from end.sum.
        """
        if self._is_bidir():
            received = self._end_pair_value("sum_received", "packets")
            if received:
                return int(self._end_pair_value("sum_received", "lost_packets"))
        return int(self._end_value("sum", "lost_packets"))

    @property
    def avg_loss_percent(self) -> float:
        """Average UDP packet loss percentage.

        For bidirectional (``--bidir``) runs the percentage is recomputed
        from the combined receiver counts (total lost / total received), not
        by adding the two directions' percentages. Otherwise derived from
        end.sum.
        """
        if self._is_bidir():
            received = self._end_pair_value("sum_received", "packets")
            if received > 0:
                lost = self._end_pair_value("sum_received", "lost_packets")
                return 100.0 * lost / received
        return self._end_value("sum", "lost_percent")

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

    # ---- DataFrame accessors ----

    def get_interval_dataframe(self) -> pd.DataFrame:
        """Return per-stream interval data as a copy of the internal DataFrame.

        Each row is one stream measurement for one reporting interval.
        Columns include all iperf3 stream fields plus _t (end timestamp in
        seconds) and _label (e.g. 'Socket 5').

        Returns:
            A pandas DataFrame copy.
        """
        return self._df_streams.copy()

    def get_summary_dataframe(self) -> pd.DataFrame:
        """Return per-interval summary data as a copy of the internal DataFrame.

        Each row is one aggregated interval entry (sum, sum_sent,
        sum_received, etc.) with _t and _label columns added.

        Returns:
            A pandas DataFrame copy.
        """
        return self._df_summaries.copy()

    # ---- Plotting ----

    def _metric_map(self) -> dict[str, tuple[str, Callable]]:
        """Return the protocol-appropriate metric label and transform map.

        Returns:
            A dict mapping iperf3 JSON field names to (axis_label, transform)
            tuples, where transform converts raw units to display units.
        """

        def _id(x: Any) -> Any:
            return x

        if self._protocol == "UDP":
            return {
                "bits_per_second": ("Throughput (Mbps)", lambda x: x / 1_000_000),
                "bytes": ("Bytes (MB)", lambda x: x / 1_000_000),
                "packets": ("Packets", _id),
                "jitter_ms": ("Jitter (ms)", _id),
                "lost_packets": ("Lost Packets", _id),
                "lost_percent": ("Loss (%)", _id),
                "out_of_order": ("Out-of-Order Packets", _id),
                "pmtu": ("Path MTU (bytes)", _id),
            }
        return {
            "bits_per_second": ("Throughput (Mbps)", lambda x: x / 1_000_000),
            "bytes": ("Bytes (MB)", lambda x: x / 1_000_000),
            "retransmits": ("Retransmits (count)", _id),
            "snd_cwnd": ("Send CWnd (KB)", lambda x: x / 1_000),
            "snd_wnd": ("Send Window (KB)", lambda x: x / 1_000),
            "rtt": ("RTT (ms)", lambda x: x / 1_000),
            "rttvar": ("RTT Var (us)", _id),
            "pmtu": ("Path MTU (bytes)", _id),
        }

    def plot_metric(
        self,
        metric: str,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        use_summary: bool = True,
        fmt: str = "png",
    ) -> str | None:
        """Plot a single time-series metric and save it to a file.

        Args:
            metric: iperf3 JSON field name to plot (e.g. 'bits_per_second',
                'rtt', 'jitter_ms').
            output_dir: Directory where the output file is written. Created
                if it does not exist.
            file_stem: Base filename stem. Output path is
                <output_dir>/<file_stem>_<metric>.<fmt>.
            use_summary: When True, prefers interval summary rows
                (sum/sum_sent) over per-stream rows. Falls back to per-stream
                data when summaries are absent or when the requested metric
                is only present in stream rows.
            fmt: File format for the output image. E.g. 'png', 'pdf'.

        Returns:
            Absolute path of the saved plot file, or None if the metric is
            not present in the recorded data.

        Raises:
            OSError: If the output directory cannot be created.
        """
        mmap = self._metric_map()
        if metric not in mmap:
            return None

        df = self._df_streams
        if (
            use_summary
            and not self._df_summaries.empty
            and metric in self._df_summaries.columns
            and self._df_summaries[metric].notna().any()
        ):
            # Summary rows only carry the shared TCP/UDP metrics; TCP-only
            # series (RTT, cwnd, PMTU) must still fall back to stream rows.
            df = self._df_summaries
        if df.empty or metric not in df.columns or df[metric].isna().all():
            return None

        pretty, transform = mmap[metric]
        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )

        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots(figsize=(10, 5))
        for label, group in df.groupby("_label"):
            ax.plot(group[tcol], transform(group[metric]), label=str(label))
        ax.set_xlabel("Time (s)")
        ax.set_ylabel(pretty)
        ax.set_title(f"{self._protocol} - {pretty}")
        ax.legend()
        ax.grid(True)

        fpath = out_path / f"{file_stem}_{metric}.{fmt}"
        fig.savefig(fpath, bbox_inches="tight", dpi=150)
        plt.close(fig)
        return str(fpath)

    def plot_all(
        self,
        output_dir: str = "/tmp/results",
        file_stem: str = "iperf3",
        fmt: str = "png",
    ) -> list[str]:
        """Plot all available metrics to separate files.

        Iterates over all protocol-appropriate metrics and saves one file
        per metric. Metrics absent from the recorded data are silently
        skipped.

        Args:
            output_dir: Directory where plot files are written.
            file_stem: Base filename stem shared by all output files.
            fmt: File format for all output images. E.g. 'png', 'pdf'.

        Returns:
            List of absolute paths of all successfully saved plot files.
        """
        saved: list[str] = []
        for metric in self._metric_map():
            path = self.plot_metric(metric, output_dir, file_stem, fmt=fmt)
            if path:
                saved.append(path)
        return saved

    def print_summary(self) -> None:
        """Print a human-readable summary of all test results to stdout."""
        proto = self._protocol
        print("=" * 60)
        print(f"          {proto} IPERF3 TEST RESULTS")
        print("=" * 60)
        print(f"Protocol:              {proto}")
        print(f"Duration:              {self.duration_seconds:.1f} s")
        print(f"Parallel streams:      {self.num_streams}")
        print(f"Avg Throughput:        {self.avg_throughput_mbps:.2f} Mbps")
        print(f"Max Throughput:        {self.max_throughput_mbps:.2f} Mbps")
        print(f"Min Throughput:        {self.min_throughput_mbps:.2f} Mbps")
        if proto == "TCP":
            print(
                f"Total Bytes Sent:      {self.total_bytes_sent / self._BYTES_TO_MB:.2f} MB"
            )
            print(
                f"Total Bytes Received:  {self.total_bytes_received / self._BYTES_TO_MB:.2f} MB"
            )
            print(f"Total Retransmits:     {self.total_retransmits}")
            print(f"Avg RTT:               {self.avg_rtt_ms:.2f} ms")
            print(f"Max RTT:               {self.max_rtt_ms:.2f} ms")
            print(
                f"Avg Send CWnd:         {self.avg_cwnd_bytes / self._BYTES_TO_KB:.2f} KB"
            )
        elif proto == "UDP":
            print(
                f"Total Bytes:           {self.total_bytes / self._BYTES_TO_MB:.2f} MB"
            )
            print(f"Total Packets:         {self.total_packets}")
            print(f"Avg Jitter:            {self.avg_jitter_ms:.3f} ms")
            print(f"Lost Packets:          {self.total_lost_packets}")
            print(f"Avg Loss:              {self.avg_loss_percent:.2f} %")
            print(f"Out of Order:          {self.total_out_of_order}")
        if self.pmtu:
            print(f"Path MTU:              {self.pmtu} bytes")
        print("=" * 60)

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of iperf3 results and configuration.

        The 'summary' key holds the computed scalar metrics derived from the
        iperf3 'end' section. Protocol-specific keys are included only for the
        relevant protocol. The 'raw' key holds the complete iperf3 JSON output
        as a dict.

        Returns:
            A dict with keys 'config', 'summary', and 'raw'. Suitable for
            passing directly to json.dumps().
        """
        summary: dict[str, Any] = {
            "protocol": self._protocol,
            "duration_seconds": self.duration_seconds,
            "num_streams": self.num_streams,
            "avg_throughput_mbps": self.avg_throughput_mbps,
            "max_throughput_mbps": self.max_throughput_mbps,
            "min_throughput_mbps": self.min_throughput_mbps,
            "pmtu": self.pmtu,
        }
        if self._protocol == "TCP":
            summary.update(
                {
                    "total_bytes_sent": self.total_bytes_sent,
                    "total_bytes_received": self.total_bytes_received,
                    "total_retransmits": self.total_retransmits,
                    "avg_rtt_ms": self.avg_rtt_ms,
                    "max_rtt_ms": self.max_rtt_ms,
                    "avg_rtt_var_us": self.avg_rtt_var_us,
                    "avg_cwnd_bytes": self.avg_cwnd_bytes,
                    "avg_snd_wnd_bytes": self.avg_snd_wnd_bytes,
                }
            )
        elif self._protocol == "UDP":
            summary.update(
                {
                    "total_bytes": self.total_bytes,
                    "total_packets": self.total_packets,
                    "avg_jitter_ms": self.avg_jitter_ms,
                    "total_lost_packets": self.total_lost_packets,
                    "avg_loss_percent": self.avg_loss_percent,
                    "total_out_of_order": self.total_out_of_order,
                }
            )
        return {
            "config": {
                "protocol": self._config.protocol,
                "duration": self._config.duration,
                "bandwidth_mbps": self._config.bandwidth_mbps,
                "parallel": self._config.parallel,
                "interval": self._config.interval,
                "port": self._config.port,
            },
            "summary": summary,
            "raw": self._data,
        }

    def __repr__(self) -> str:
        return (
            f"Iperf3Results(protocol={self._protocol!r}, "
            f"avg_throughput_mbps={self.avg_throughput_mbps:.2f})"
        )
