import enum
import json
import os
import platform
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd
import matplotlib

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

if TYPE_CHECKING:
    from satgonetem.models.node import Node


@dataclass
class Iperf3Config:
    """Configuration parameters for a single iperf3 traffic run.

    Covers all iperf3 client options for both TCP and UDP modes.
    Protocol-specific options are silently ignored when not applicable.

    Attributes:
        protocol: Transport protocol. Accepted values: 'TCP', 'UDP'.
        duration: Test duration in seconds. Maps to the -t flag.
        bandwidth_mbps: Target send bandwidth in Mbps. 0 means unlimited.
            Maps to the -b flag.
        parallel: Number of parallel streams. Maps to the -P flag.
        interval: Per-stream reporting interval in seconds. Maps to
            --interval.
        port: Server listen port. Maps to -p flag on both client and server.
        congestion_control: TCP congestion control algorithm name (e.g.
            'bbr', 'cubic'). Maps to -C. TCP only.
        window_size: Socket send/receive buffer size (e.g. '256K', '1M').
            Maps to -w.
        mss: TCP maximum segment size in bytes. Maps to -M. TCP only.
        no_delay: Disable Nagle algorithm (set TCP_NODELAY). Maps to -N.
            TCP only.
        length: Read/write buffer length in bytes or with suffix (e.g.
            '1400', '1K'). Maps to -l.
        pacing_timer: UDP pacing timer value in microseconds. Maps to
            --pacing-timer. UDP only.
        reverse: Run in reverse mode (server sends, client receives). Maps
            to --reverse.
        bidir: Run bidirectional test simultaneously. Maps to --bidir.
        tos: IP Type of Service / DSCP byte value. Maps to -S.
        ttl: IP time-to-live. Maps to --ttl.
        num_bytes: Number of bytes to transmit instead of a timed run.
            When set, duration is ignored. Maps to -n.
        omit: Seconds to omit from the start (warm-up period). Maps to -O.
        affinity: CPU affinity string (e.g. '0', '0,1'). Maps to
            --affinity.
    """

    protocol: str = "TCP"
    duration: int = 10
    bandwidth_mbps: float = 0.0
    parallel: int = 1
    interval: float = 1.0
    port: int = 5201
    congestion_control: str = "bbr"
    window_size: Optional[str] = None
    mss: Optional[int] = None
    no_delay: bool = False
    length: Optional[str] = None
    pacing_timer: Optional[int] = None
    reverse: bool = False
    bidir: bool = False
    tos: Optional[int] = None
    ttl: Optional[int] = None
    num_bytes: Optional[int] = None
    omit: int = 0
    affinity: Optional[str] = None

    def build_client_command(
        self,
        server_ip: str,
        bind_ip: str,
        output_json_path: str,
    ) -> str:
        """Build the complete iperf3 client shell command string.

        Assembles all configured options into a shell-escaped command that
        can be passed directly to Node.execute_command. JSON output is
        always enabled and redirected to output_json_path.

        Args:
            server_ip: IP address of the iperf3 server to connect to.
            bind_ip: Local address to bind the client socket to (-B flag).
            output_json_path: Container path where JSON output is written.

        Returns:
            A shell command string ready for Node.execute_command.
        """
        parts = [
            "iperf3",
            f"-c {server_ip}",
            f"-B {bind_ip}",
            f"-p {self.port}",
            f"--interval {self.interval}",
            "--json",
        ]

        if self.num_bytes is not None:
            parts.append(f"-n {self.num_bytes}")
        else:
            parts.append(f"-t {self.duration}")

        parts.append(f"-P {self.parallel}")

        if self.protocol.upper() == "UDP":
            parts.append("-u")
            if self.bandwidth_mbps > 0:
                parts.append(f"-b {self.bandwidth_mbps}M")
            if self.pacing_timer is not None:
                parts.append(f"--pacing-timer {self.pacing_timer}")
        else:
            if self.bandwidth_mbps > 0:
                parts.append(f"-b {self.bandwidth_mbps}M")
            if self.congestion_control:
                parts.append(f"-C {self.congestion_control}")
            if self.window_size:
                parts.append(f"-w {self.window_size}")
            if self.mss is not None:
                parts.append(f"-M {self.mss}")
            if self.no_delay:
                parts.append("-N")

        if self.length:
            parts.append(f"-l {self.length}")
        if self.reverse:
            parts.append("--reverse")
        if self.bidir:
            parts.append("--bidir")
        if self.tos is not None:
            parts.append(f"-S {self.tos}")
        if self.ttl is not None:
            parts.append(f"--ttl {self.ttl}")
        if self.omit > 0:
            parts.append(f"-O {self.omit}")
        if self.affinity:
            parts.append(f"--affinity {self.affinity}")

        parts.append(f"> {shlex.quote(output_json_path)} 2>&1")
        return " ".join(parts)


class Iperf3Results:
    """Parsed results from an iperf3 JSON output.

    Provides typed property getters for all TCP and UDP metrics, interval-
    level DataFrames for time-series analysis, and methods to plot
    individual metrics to files.

    All summary metrics are derived from the 'end' section of the iperf3
    JSON (the most reliable aggregated values). Time-series properties pull
    from the flattened interval DataFrames.
    """

    _BPS_TO_MBPS: float = 1_000_000.0
    _BYTES_TO_MB: float = 1_000_000.0
    _BYTES_TO_KB: float = 1_000.0
    _USEC_TO_MS: float = 1_000.0

    def __init__(self, raw_json: str, config: Iperf3Config) -> None:
        """
        Args:
            raw_json: Raw JSON string from iperf3 --json client output.
            config: The Iperf3Config instance used to produce this result.

        Raises:
            ValueError: If raw_json is empty or cannot be parsed as JSON.
        """
        if not raw_json or not raw_json.strip():
            raise ValueError(
                "iperf3 returned empty output; the flow may have failed."
            )
        try:
            self._data: dict = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Failed to parse iperf3 JSON output: {exc}"
            ) from exc

        self._config = config
        self._protocol: str = str(
            self._data.get("start", {})
            .get("test_start", {})
            .get("protocol", config.protocol)
        ).upper()
        self._df_streams, self._df_summaries = self._flatten_intervals()

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

    @property
    def avg_throughput_mbps(self) -> float:
        """Mean throughput across all reporting intervals in Mbps."""
        return self._col_mean(self._primary_df(), "bits_per_second") / self._BPS_TO_MBPS

    @property
    def max_throughput_mbps(self) -> float:
        """Peak interval throughput in Mbps."""
        return self._col_max(self._primary_df(), "bits_per_second") / self._BPS_TO_MBPS

    @property
    def min_throughput_mbps(self) -> float:
        """Minimum interval throughput in Mbps."""
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
        """Total bytes sent (TCP). Derived from end.sum_sent."""
        return int(self._end().get("sum_sent", {}).get("bytes", 0))

    @property
    def total_bytes_received(self) -> int:
        """Total bytes received (TCP). Derived from end.sum_received."""
        return int(self._end().get("sum_received", {}).get("bytes", 0))

    @property
    def total_retransmits(self) -> int:
        """Total TCP retransmissions across the entire test."""
        return int(self._end().get("sum_sent", {}).get("retransmits", 0))

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
        """Total bytes transmitted (UDP). Derived from end.sum."""
        return int(self._end().get("sum", {}).get("bytes", 0))

    @property
    def total_packets(self) -> int:
        """Total UDP packets transmitted. Derived from end.sum."""
        return int(self._end().get("sum", {}).get("packets", 0))

    @property
    def avg_jitter_ms(self) -> float:
        """Mean one-way jitter in milliseconds (UDP). Derived from end.sum."""
        return float(self._end().get("sum", {}).get("jitter_ms", 0.0))

    @property
    def total_lost_packets(self) -> int:
        """Total lost UDP packets. Derived from end.sum."""
        return int(self._end().get("sum", {}).get("lost_packets", 0))

    @property
    def avg_loss_percent(self) -> float:
        """Average UDP packet loss percentage. Derived from end.sum."""
        return float(self._end().get("sum", {}).get("lost_percent", 0.0))

    @property
    def total_out_of_order(self) -> int:
        """Total out-of-order UDP packets. Derived from end.sum."""
        return int(self._end().get("sum", {}).get("out_of_order", 0))

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
    ) -> Optional[str]:
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
                data when summaries are absent.
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

        df = (
            self._df_summaries
            if (use_summary and not self._df_summaries.empty)
            else self._df_streams
        )
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
            print(f"Avg Send CWnd:         {self.avg_cwnd_bytes / self._BYTES_TO_KB:.2f} KB")
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
            summary.update({
                "total_bytes_sent": self.total_bytes_sent,
                "total_bytes_received": self.total_bytes_received,
                "total_retransmits": self.total_retransmits,
                "avg_rtt_ms": self.avg_rtt_ms,
                "max_rtt_ms": self.max_rtt_ms,
                "avg_rtt_var_us": self.avg_rtt_var_us,
                "avg_cwnd_bytes": self.avg_cwnd_bytes,
                "avg_snd_wnd_bytes": self.avg_snd_wnd_bytes,
            })
        elif self._protocol == "UDP":
            summary.update({
                "total_bytes": self.total_bytes,
                "total_packets": self.total_packets,
                "avg_jitter_ms": self.avg_jitter_ms,
                "total_lost_packets": self.total_lost_packets,
                "avg_loss_percent": self.avg_loss_percent,
                "total_out_of_order": self.total_out_of_order,
            })
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


class FlowStatus(enum.Enum):
    """Lifecycle state of an Iperf3Flow.

    Attributes:
        IDLE: Flow has been created but not yet started.
        RUNNING: Flow is executing in the background.
        DONE: Flow completed successfully; results are available.
        ERROR: Flow failed; the causing exception is stored on the flow.
    """

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class Iperf3Flow:
    """Non-blocking iperf3 flow between two nodes.

    Wraps run_iperf3_flow in a daemon thread so the caller is not blocked.
    All state transitions are protected by an internal lock, making status
    and results safe to poll from any thread.

    Usage::

        flow = Iperf3Flow(source, destination, config)
        flow.start()
        while flow.status() == FlowStatus.RUNNING:
            time.sleep(0.5)
        results = flow.results()

    Attributes:
        source: Node that runs the iperf3 client.
        destination: Node that runs the iperf3 server.
        config: Iperf3Config for this flow.
    """

    def __init__(
        self,
        source: "Node",
        destination: "Node",
        config: Iperf3Config,
        delay: float = 0.0,
    ) -> None:
        """
        Args:
            source: Node that runs the iperf3 client.
            destination: Node that runs the iperf3 server.
            config: Full iperf3 configuration for this flow.
            delay: Seconds to wait inside the background thread before
                executing the flow. The flow status is RUNNING during
                this wait period.
        """
        self.source = source
        self.destination = destination
        self.config = config
        self.delay = delay

        self._status = FlowStatus.IDLE
        self._result: Optional[Iperf3Results] = None
        self._error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the iperf3 flow in a background daemon thread.

        Returns immediately; use status() to poll for completion.

        Raises:
            RuntimeError: If the flow has already been started.
        """
        with self._lock:
            if self._status != FlowStatus.IDLE:
                raise RuntimeError(
                    f"Flow has already been started (status={self._status.value})"
                )
            self._status = FlowStatus.RUNNING
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Send SIGTERM to the running iperf3 client and server and wait for the thread.

        Any partial JSON output retrieved from the output file is printed to stdout.
        Both client and server processes are terminated. No-op if not running.
        """
        with self._lock:
            if self._status != FlowStatus.RUNNING:
                return
        self._stop_event.set()
        self.source.execute_command("pkill -x iperf3", detach=True)
        self.destination.execute_command("pkill -x iperf3", detach=True)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def status(self) -> FlowStatus:
        """Return the current lifecycle state of the flow.

        Returns:
            The current FlowStatus value.
        """
        with self._lock:
            return self._status

    def results(self) -> Iperf3Results:
        """Return the parsed results once the flow has completed.

        Returns:
            Iperf3Results from the completed flow.

        Raises:
            RuntimeError: If the flow has not been started, is still running,
                or completed with an error.
        """
        with self._lock:
            match self._status:
                case FlowStatus.IDLE:
                    raise RuntimeError("Flow has not been started yet")
                case FlowStatus.RUNNING:
                    raise RuntimeError("Flow is still running")
                case FlowStatus.ERROR:
                    raise RuntimeError(
                        f"Flow failed: {self._error}"
                    ) from self._error
                case FlowStatus.DONE:
                    return self._result  # type: ignore[return-value]
                case _ as unexpected:
                    raise TypeError(f"Unexpected FlowStatus: {unexpected!r}")

    def _run(self) -> None:
        """Execute the iperf3 flow and update status on completion."""
        if self.delay > 0.0:
            time.sleep(self.delay)

        run_id = uuid.uuid4().hex[:8]
        client_json = (
            f"/tmp/iperf3_cli_{self.source.name}_{self.destination.name}_{run_id}.json"
        )
        server_json = (
            f"/tmp/iperf3_srv_{self.source.name}_{self.destination.name}_{run_id}.json"
        )
        dst_ip = self.destination.loopback.ipv4
        src_ip = self.source.loopback.ipv4

        raw: Optional[str] = None
        try:
            server_cmd = (
                f"iperf3 -s -1 -B {shlex.quote(dst_ip)} -p {self.config.port} "
                f"--json > {shlex.quote(server_json)} 2>&1"
            )
            self.destination.execute_command(server_cmd, detach=True)
            time.sleep(0.5)

            client_cmd = self.config.build_client_command(dst_ip, src_ip, client_json)
            self.source.execute_command(client_cmd, detach=False)
            time.sleep(0.3)

            if self.config.protocol.upper() == "UDP":
                raw = self.destination.execute_command(f"cat {server_json}")
                if not isinstance(raw, str) or not raw.strip().startswith("{"):
                    raw = self.source.execute_command(f"cat {client_json}")
            else:
                raw = self.source.execute_command(f"cat {client_json}")
                if not isinstance(raw, str) or not raw.strip().startswith("{"):
                    raw = self.destination.execute_command(f"cat {server_json}")

            self.source.execute_command(f"rm -f {client_json}", detach=True)
            self.destination.execute_command(f"rm -f {server_json}", detach=True)

            if not isinstance(raw, str) or not raw.strip().startswith("{"):
                raise RuntimeError(
                    f"Could not retrieve iperf3 JSON output for flow "
                    f"{self.source.name} -> {self.destination.name}. "
                    "Verify iperf3 is installed in both containers and a route exists."
                )

            result = Iperf3Results(raw_json=raw, config=self.config)
            with self._lock:
                self._result = result
                self._status = FlowStatus.DONE
        except Exception as exc:
            if self._stop_event.is_set() and raw:
                print(raw)
            self.source.execute_command(f"rm -f {client_json}", detach=True)
            self.destination.execute_command(f"rm -f {server_json}", detach=True)
            with self._lock:
                self._error = exc
                self._status = FlowStatus.ERROR


def run_iperf3_flow(
    source: "Node",
    destination: "Node",
    config: Iperf3Config,
) -> Iperf3Results:
    """Launch an iperf3 flow between two nodes and return parsed results.

    Starts a one-shot iperf3 server on destination bound to
    destination.loopback.ipv4, then runs the iperf3 client on source bound
    to source.loopback.ipv4 (-B flag). The client runs synchronously; this
    function blocks until it completes.

    The server exits automatically after the first client connection (-1).
    Temporary JSON files written inside the containers are cleaned up after
    the results are retrieved.

    Args:
        source: Node that runs the iperf3 client.
        destination: Node that runs the iperf3 server.
        config: Full iperf3 configuration for this run.

    Returns:
        Iperf3Results populated with the parsed JSON output. For UDP flows,
        the server-side JSON is used because receiver metrics (jitter, packet
        loss, out-of-order) are only reported on the receiving end. For TCP,
        the client-side JSON is used, with the server as fallback.

    Raises:
        ValueError: If the retrieved JSON is empty or unparseable.
        RuntimeError: If neither client nor server JSON could be retrieved
            from the containers.
    """
    run_id = uuid.uuid4().hex[:8]
    client_json = f"/tmp/iperf3_cli_{source.name}_{destination.name}_{run_id}.json"
    server_json = f"/tmp/iperf3_srv_{source.name}_{destination.name}_{run_id}.json"

    dst_ip = destination.loopback.ipv4
    src_ip = source.loopback.ipv4

    server_cmd = (
        f'sh -c "iperf3 -s -1 -B {dst_ip} -p {config.port} '
        f'--json > {server_json} 2>&1"'
    )
    destination.execute_command(server_cmd, detach=True)
    time.sleep(0.5)

    client_cmd = config.build_client_command(dst_ip, src_ip, client_json)
    source.execute_command(client_cmd, detach=False)
    time.sleep(0.3)

    if config.protocol.upper() == "UDP":
        raw: Any = destination.execute_command(f"cat {server_json}")
        if not isinstance(raw, str) or not raw.strip().startswith("{"):
            raw = source.execute_command(f"cat {client_json}")
    else:
        raw = source.execute_command(f"cat {client_json}")
        if not isinstance(raw, str) or not raw.strip().startswith("{"):
            raw = destination.execute_command(f"cat {server_json}")

    source.execute_command(f"rm -f {client_json}", detach=True)
    destination.execute_command(f"rm -f {server_json}", detach=True)

    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        raise RuntimeError(
            f"Could not retrieve iperf3 JSON output for flow "
            f"{source.name} -> {destination.name}. "
            "Verify iperf3 is installed in both containers and a route exists."
        )

    return Iperf3Results(raw_json=raw, config=config)


def generate_iperf3_plots(
    json_file_path: str, json_file_name: str = "iperf3", use_tmp_dir=True
) -> list[str]:
    """
    Read an iperf3 JSON result file and save all relevant plots (per-stream and summaries)
    into /tmp/results as PNGs. Returns the list of saved file paths and opens the folder.
    """
    # ---------- Load ----------
    with open(json_file_path) as f:
        data = json.load(f)
    protocol = str(
        data.get("start", {}).get("test_start", {}).get("protocol", "TCP")
    ).upper()

    # ---------- Output dir ----------
    if use_tmp_dir:
        out_dir = Path("/tmp/results")
    else:
        path = Path(json_file_path)
        path = path.parent
        out_dir = Path(f"{path}/results")
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---------- Flatten helpers ----------
    def flatten_intervals(d):
        intervals = d.get("intervals", [])
        stream_rows = []
        sum_rows = []
        for it in intervals:
            for s in it.get("streams", []):
                row = dict(s)
                row["_t"] = s.get("end", s.get("start"))
                row["_label"] = f"Socket {s.get('socket', '?')}"
                stream_rows.append(row)
            for sum_key in [
                "sum",
                "sum_sent",
                "sum_received",
                "sum_bidir",
                "sum_bidir_reverse",
            ]:
                if sum_key in it:
                    s = dict(it[sum_key])
                    s["_t"] = s.get("end", s.get("start"))
                    s["_label"] = sum_key
                    sum_rows.append(s)
        return (
            pd.DataFrame(stream_rows) if stream_rows else pd.DataFrame(),
            pd.DataFrame(sum_rows) if sum_rows else pd.DataFrame(),
        )

    df_streams, df_summaries = flatten_intervals(data)

    # ---------- Metric maps ----------
    def _id(x):
        return x

    def _bps_to_mbps(x):
        return x / 1_000_000  # Convert bps to Mbps

    def _bytes_to_mb(x):
        return x / 1_000_000  # Convert bytes to MB

    def _bytes_to_kb(x):
        return x / 1_000  # Convert bytes to KB

    metric_map_tcp = {
        "bits_per_second": ("Throughput (Mbps)", _bps_to_mbps),
        "bytes": ("Bytes (MB)", _bytes_to_mb),
        "retransmits": ("Retransmits (count)", _id),
        "snd_cwnd": ("Send Congestion Window (KB)", _bytes_to_kb),
        "snd_wnd": ("Send Window (KB)", _bytes_to_kb),
        "rtt": ("RTT (ms)", lambda s: s / 1000.0),  # usec -> ms
        "rttvar": ("RTT Var (usec)", _id),
        "pmtu": ("Path MTU (bytes)", _id),
    }
    metric_map_udp = {
        "bits_per_second": ("Throughput (Mbps)", _bps_to_mbps),
        "bytes": ("Bytes (MB)", _bytes_to_mb),
        "packets": ("Packets", _id),
        "jitter_ms": ("Jitter (ms)", _id),
        "lost_packets": ("Lost Packets", _id),
        "lost_percent": ("Loss (%)", _id),
        "out_of_order": ("Out-of-Order Packets", _id),
        "pmtu": ("Path MTU (bytes)", _id),
    }
    metric_map = metric_map_udp if protocol == "UDP" else metric_map_tcp

    # ---------- Detect present metrics ----------
    def present_metrics(df, metric_map):
        if df.empty:
            return {}
        avail = {}
        for k, v in metric_map.items():
            if k in df.columns and df[k].notna().any():
                avail[k] = v
        return avail

    stream_metrics = present_metrics(df_streams, metric_map)
    summary_metrics = present_metrics(df_summaries, metric_map)

    # ---------- Plotting helper ----------
    def plot_and_save(df, metrics, title_prefix, stem_prefix):
        saved = []
        if df.empty or not metrics:
            return saved
        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )
        # Ensure label column exists
        if "_label" not in df.columns:
            df = df.copy()
            if "socket" in df.columns:
                df["_label"] = df["socket"].apply(lambda s: f"Socket {s}")
            else:
                df["_label"] = "series"
        for metric_key, (pretty, tfm) in metrics.items():
            plt.figure(figsize=(10, 5))
            for label, g in df.groupby("_label"):
                plt.plot(g[tcol], tfm(g[metric_key]), label=str(label))
            plt.xlabel("Time (s)")
            plt.ylabel(pretty)
            plt.title(f"{title_prefix} — {pretty}")
            plt.legend()
            plt.grid(True)
            fname = f"{json_file_name}_{stem_prefix}_{metric_key}.png"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            fname = f"{json_file_name}_{stem_prefix}_{metric_key}.pdf"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            plt.close()
            saved.append(str(fpath))
        return saved

    saved_files = []
    saved_files += plot_and_save(
        df_streams,
        stream_metrics,
        f"{protocol} per-stream",
        f"{protocol.lower()}_per_stream",
    )
    saved_files += plot_and_save(
        df_summaries,
        summary_metrics,
        f"{protocol} interval summaries",
        f"{protocol.lower()}_summaries",
    )

    # ---------- Cumulative data plot ----------
    def plot_cumulative_data(df, title_prefix, stem_prefix):
        saved = []
        if df.empty:
            return saved

        tcol = (
            "end"
            if "end" in df.columns
            else ("_t" if "_t" in df.columns else "seconds")
        )

        # Ensure label column exists
        if "_label" not in df.columns:
            df = df.copy()
            if "socket" in df.columns:
                df["_label"] = df["socket"].apply(lambda s: f"Socket {s}")
            else:
                df["_label"] = "series"

        # Only plot if bytes column exists
        if "bytes" in df.columns and df["bytes"].notna().any():
            plt.figure(figsize=(10, 5))
            for label, g in df.groupby("_label"):
                # Sort by time to ensure proper cumulative calculation
                g_sorted = g.sort_values(by=tcol)
                cumulative_mb = g_sorted["bytes"].cumsum() / 1_000_000  # Convert to MB
                plt.plot(g_sorted[tcol], cumulative_mb, label=str(label))

            plt.xlabel("Time (s)")
            plt.ylabel("Cumulative Data Transmitted (MB)")
            plt.title(f"{title_prefix} — Cumulative Data Transmitted")
            plt.legend()
            plt.grid(True)
            fname = f"{json_file_name}_{stem_prefix}_cumulative_data.png"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            fname = f"{json_file_name}_{stem_prefix}_cumulative_data.pdf"
            fpath = out_dir / fname
            plt.savefig(fpath, bbox_inches="tight", dpi=150)
            plt.close()
            saved.append(str(fpath))

        return saved

    # Add cumulative plots for both streams and summaries
    saved_files += plot_cumulative_data(
        df_streams, f"{protocol} per-stream", f"{protocol.lower()}_per_stream"
    )
    saved_files += plot_cumulative_data(
        df_summaries, f"{protocol} interval summaries", f"{protocol.lower()}_summaries"
    )

    # ---------- Calculate and print statistics ----------
    def calculate_and_print_stats(df_streams, df_summaries, protocol):
        # Clear console
        # os.system('clear' if os.name == 'posix' else 'cls')

        print("=" * 60)
        print(f"           {protocol} IPERF3 TEST STATISTICS")
        print("=" * 60)

        # Use summary data if available, otherwise use stream data
        df_for_stats = df_summaries if not df_summaries.empty else df_streams

        if df_for_stats.empty:
            print("No data available for statistics calculation.")
            return

        # Average Throughput (Mbps)
        if "bits_per_second" in df_for_stats.columns:
            avg_throughput_bps = df_for_stats["bits_per_second"].mean()
            avg_throughput_mbps = avg_throughput_bps / 1_000_000
            print(f"Average Throughput:      {avg_throughput_mbps:.2f} Mbps")
        else:
            print("Average Throughput:      Not available")

        # Total Retransmissions (TCP only)
        if protocol == "TCP" and "retransmits" in df_for_stats.columns:
            total_retransmits = df_for_stats["retransmits"].sum()
            print(f"Total Retransmissions:   {total_retransmits:.0f}")
        elif protocol == "TCP":
            print("Total Retransmissions:   Not available")

        # Average RTT (TCP only)
        if protocol == "TCP" and "rtt" in df_for_stats.columns:
            # RTT is in microseconds, convert to milliseconds
            avg_rtt_us = df_for_stats["rtt"].mean()
            avg_rtt_ms = avg_rtt_us / 1000.0
            print(f"Average RTT:             {avg_rtt_ms:.2f} ms")
        elif protocol == "TCP":
            print("Average RTT:             Not available")

        # UDP-specific stats
        if protocol == "UDP":
            if "lost_packets" in df_for_stats.columns:
                total_lost = df_for_stats["lost_packets"].sum()
                print(f"Total Lost Packets:      {total_lost:.0f}")

            if "lost_percent" in df_for_stats.columns:
                avg_loss_percent = df_for_stats["lost_percent"].mean()
                print(f"Average Packet Loss:     {avg_loss_percent:.2f}%")

            if "jitter_ms" in df_for_stats.columns:
                avg_jitter = df_for_stats["jitter_ms"].mean()
                print(f"Average Jitter:          {avg_jitter:.2f} ms")

        # Total Data Transferred
        if "bytes" in df_for_stats.columns:
            total_bytes = df_for_stats["bytes"].sum()
            total_mb = total_bytes / 1_000_000
            print(f"Total Data Transferred:  {total_mb:.2f} MB")

        print("=" * 60)
        print(f"Plots saved to: {out_dir}")
        print("=" * 60)

    calculate_and_print_stats(df_streams, df_summaries, protocol)

    # ---------- Open folder in explorer ----------
    if use_tmp_dir:
        try:
            system = platform.system()
            if system == "Darwin":  # macOS
                subprocess.run(["open", str(out_dir)], check=False)
            elif system == "Windows":
                subprocess.run(["explorer", str(out_dir)], check=False)
            else:  # Linux and others
                subprocess.run(["xdg-open", str(out_dir)], check=False)
        except Exception:
            pass

    return saved_files


# Example:
# files = generate_iperf_plots("/path/to/iperf3.json")
# print("Saved plots:", files)
