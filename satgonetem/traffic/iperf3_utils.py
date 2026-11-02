import enum
import json
import platform
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import pandas as pd
import matplotlib

matplotlib.use("Agg")  # non-GUI backend
import matplotlib.pyplot as plt

if TYPE_CHECKING:
    from satgonetem.models.node import Node


_IPERF3_PORT_RANGE_START = 5201
_IPERF3_PORT_RANGE_END = 5300
_IPERF3_SERVER_READY_TIMEOUT = 2.0
_IPERF3_SERVER_READY_POLL_INTERVAL = 0.1
_IPERF3_SERVER_START_FALLBACK = 0.5


class _Iperf3PortAllocator:
    """Thread-safe allocator of unique iperf3 server ports.

    A single allocator instance is shared process-wide so concurrent flows
    never bind the same destination port. Ports are handed out from a
    bounded range and released by each flow when it finishes.
    """

    def __init__(
        self,
        base_port: int = _IPERF3_PORT_RANGE_START,
        max_port: int = _IPERF3_PORT_RANGE_END,
    ) -> None:
        if not 1 <= base_port <= max_port <= 65535:
            raise ValueError(
                f"Invalid iperf3 port range {base_port}-{max_port}; "
                "expected 1 <= base_port <= max_port <= 65535"
            )
        self._base_port = base_port
        self._max_port = max_port
        self._next_port = base_port
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        """Reserve and return a free port from the configured range.

        Scans forward from the last handed-out port and wraps around once.

        Raises:
            RuntimeError: If every port in the range is already in use.
        """
        with self._lock:
            span = self._max_port - self._base_port + 1
            for _ in range(span):
                port = self._next_port
                self._next_port = (
                    self._base_port
                    if port >= self._max_port
                    else port + 1
                )
                if port not in self._in_use:
                    self._in_use.add(port)
                    return port
        raise RuntimeError(
            f"no free iperf3 port in range "
            f"{self._base_port}-{self._max_port}"
        )

    def release(self, port: int) -> None:
        """Return *port* to the pool. Idempotent for unknown ports."""
        with self._lock:
            self._in_use.discard(port)

    @property
    def in_use_ports(self) -> set[int]:
        """Return a snapshot of the currently reserved ports."""
        with self._lock:
            return set(self._in_use)


_IPERF3_PORT_ALLOCATOR = _Iperf3PortAllocator()


def _is_iperf3_server_listening(
    destination: "Node", listen_ip: str, port: int
) -> Optional[bool]:
    """Check whether an iperf3 server is listening on *listen_ip*:*port*.

    Uses the kernel listener table when available and falls back to a
    process check for containers without ``ss``.

    Returns:
        True when the listener/process was observed, False when it was not,
        or None when the probe could not be executed (e.g. a mocked node).
    """
    port_token = f"{listen_ip}:{port}"
    process_pattern = f"iperf3 -s -1 -B {listen_ip} -p {port}"
    check_cmd = (
        f"ss -ltn 2>/dev/null | grep -F {shlex.quote(port_token)} "
        f"|| pgrep -f {shlex.quote(process_pattern)} 2>/dev/null"
    )
    try:
        output = destination.execute_command(
            check_cmd, ok_exit_codes=(0, 1)
        )
    except TypeError:
        # Nodes with a reduced execute_command signature.
        try:
            output = destination.execute_command(check_cmd)
        except Exception:
            return None
    except Exception:
        return None
    if output is None:
        return None
    return bool(str(output).strip())


def _wait_for_iperf3_server(
    destination: "Node",
    listen_ip: str,
    port: int,
    timeout: float = _IPERF3_SERVER_READY_TIMEOUT,
    poll_interval: float = _IPERF3_SERVER_READY_POLL_INTERVAL,
    fallback: float = _IPERF3_SERVER_START_FALLBACK,
) -> bool:
    """Wait for the destination iperf3 server to accept connections.

    Polls for at most *timeout* seconds. When the listener probe is not
    available at all, keeps the legacy fixed startup grace period instead.

    Returns:
        True when the server was observed listening, False otherwise.
    """
    deadline = time.monotonic() + timeout
    while True:
        listening = _is_iperf3_server_listening(
            destination, listen_ip, port
        )
        if listening is True:
            return True
        if listening is None:
            time.sleep(fallback)
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


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
        if self.bandwidth_mbps < 0:
            raise ValueError(
                f"bandwidth_mbps must be >= 0, got {self.bandwidth_mbps}"
            )

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
            # iperf3 defaults UDP to 1 Mbps; -b 0 keeps it unlimited.
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

    def _summary_series(self) -> Optional[pd.DataFrame]:
        """Return one consistent interval summary series, if available.

        Prefers the aggregate ``sum`` rows, then sender/receiver summaries.
        Using one label avoids mixing three different rate series per interval.

        Returns:
            The selected summary DataFrame, or None when no summary is present.
        """
        if self._df_summaries.empty or "_label" not in self._df_summaries.columns:
            return None
        for label in ("sum", "sum_received", "sum_sent"):
            series = self._df_summaries[self._df_summaries["_label"] == label]
            if not series.empty:
                return series
        return None

    @property
    def avg_throughput_mbps(self) -> float:
        """Mean throughput in Mbps, excluding omitted warm-up intervals."""
        series = self._summary_series()
        if (
            series is not None
            and "bytes" in series.columns
            and "seconds" in series.columns
        ):
            total_seconds = float(series["seconds"].sum())
            if total_seconds > 0:
                return (
                    float(series["bytes"].sum())
                    * 8.0
                    / total_seconds
                    / self._BPS_TO_MBPS
                )
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
        """Total UDP payload bytes, preferring the receiver summary."""
        end = self._end()
        for key in ("sum_received", "sum_sent", "sum"):
            value = end.get(key, {}).get("bytes")
            if value:
                return int(value)
        return 0

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

    Each flow reserves its own server port, so flows sharing one
    Iperf3Config (and one destination) cannot collide on port 5201. The
    caller-supplied config is never mutated.

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
        # Reserved on start so flows constructed up-front (for a scheduler
        # with hundreds of entries) do not exhaust the port pool. config.port
        # is left untouched so shared configs remain safe to reuse.
        self._port: Optional[int] = None

        self._status = FlowStatus.IDLE
        self._result: Optional[Iperf3Results] = None
        self._error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def _reserve_port(self) -> int:
        """Reserve this flow's exclusive server port on first use."""
        if self._port is None:
            self._port = _IPERF3_PORT_ALLOCATOR.allocate()
        return self._port

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
            self._reserve_port()
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

        port = self._port if self._port is not None else self.config.port
        kill_cmd = f"pkill -f 'iperf3 .*-p {port} ' 2>/dev/null; true"
        self.source.execute_command(kill_cmd, detach=True)
        self.destination.execute_command(kill_cmd, detach=True)
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

    def _json_side(self, client_json: str, server_json: str):
        """Return the preferred and fallback JSON locations.

        UDP metrics (loss/jitter) live on the receiver; TCP metrics
        (RTT/CWND/retransmits) live on the sender. Bidirectional runs use the
        client JSON because it contains both direction streams.

        Returns:
            Tuple of (preferred_node, preferred_path, fallback_node,
            fallback_path).
        """
        if self.config.bidir:
            preferred = self.source
            fallback = self.destination
            return preferred, client_json, fallback, server_json

        is_udp = self.config.protocol.upper() == "UDP"
        use_source = self.config.reverse if is_udp else not self.config.reverse
        if use_source:
            return self.source, client_json, self.destination, server_json
        return self.destination, server_json, self.source, client_json

    def _remove_temp_file(self, node: "Node", path: Optional[str]) -> None:
        """Best-effort removal of a temporary JSON file inside a node.

        Cleanup failures must never change the flow outcome, so every error
        is suppressed.
        """
        if not path:
            return
        try:
            node.execute_command(f"rm -f {shlex.quote(path)}", detach=True)
        except Exception:
            pass

    def _kill_server(self, dst_ip: Optional[str]) -> None:
        """Best-effort kill of this flow's detached one-shot iperf3 server.

        The pattern is scoped to the flow's bind address and port so that
        servers belonging to other flows are left alone. When the bind
        address could not even be resolved, fall back to matching only the
        one-shot server on this flow's port. Errors are suppressed.
        """
        port = self._port if self._port is not None else self.config.port
        if dst_ip:
            pattern = f"iperf3 -s -1 -B {dst_ip} -p {port}"
        else:
            pattern = f"iperf3 -s -1 .* -p {port}"
        try:
            self.destination.execute_command(
                f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true"
            )
        except Exception:
            pass

    def _collect_diagnostics(
        self,
        client_json: Optional[str],
        server_json: Optional[str],
    ) -> list[str]:
        """Best-effort capture of client/server output for error reporting."""
        diagnostics: list[str] = []
        for label, path, node in (
            ("client", client_json, self.source),
            ("server", server_json, self.destination),
        ):
            if not path:
                continue
            try:
                content = node.execute_command(f"cat {shlex.quote(path)}")
            except Exception:
                continue
            if isinstance(content, str) and content.strip():
                diagnostics.append(
                    f"--- {label} output ({path}) ---\n{content.strip()}"
                )
        return diagnostics

    def _run(self) -> None:
        """Execute the iperf3 flow and update status on completion.

        The entire body is guarded so that every exit path lands in a
        terminal status (``DONE`` or ``ERROR``), including failures while
        resolving node addresses or building temp paths. The result/status
        are recorded before best-effort cleanup, and cleanup errors can
        never discard a valid measurement.
        """
        client_json: Optional[str] = None
        server_json: Optional[str] = None
        dst_ip: Optional[str] = None
        raw: Optional[str] = None
        try:
            if self.delay > 0.0 and self._stop_event.wait(self.delay):
                with self._lock:
                    self._error = RuntimeError(
                        "Flow stopped before it could start"
                    )
                    self._status = FlowStatus.ERROR
                return

            run_id = uuid.uuid4().hex[:8]
            client_json = (
                f"/tmp/iperf3_cli_{self.source.name}_{self.destination.name}_{run_id}.json"
            )
            server_json = (
                f"/tmp/iperf3_srv_{self.source.name}_{self.destination.name}_{run_id}.json"
            )
            dst_ip = self.destination.loopback.ipv4
            src_ip = self.source.loopback.ipv4
            run_config = replace(self.config, port=self._reserve_port())

            server_cmd = (
                f"iperf3 -s -1 -B {shlex.quote(dst_ip)} -p {run_config.port} "
                f"--json > {shlex.quote(server_json)} 2>&1"
            )
            self.destination.execute_command(server_cmd, detach=True)
            _wait_for_iperf3_server(self.destination, dst_ip, run_config.port)

            client_cmd = run_config.build_client_command(dst_ip, src_ip, client_json)
            self.source.execute_command(client_cmd, detach=False)
            time.sleep(0.3)

            preferred_node, preferred_path, fallback_node, fallback_path = (
                self._json_side(client_json, server_json)
            )
            raw = preferred_node.execute_command(
                f"cat {shlex.quote(preferred_path)}"
            )
            if not isinstance(raw, str) or not raw.strip().startswith("{"):
                raw = fallback_node.execute_command(
                    f"cat {shlex.quote(fallback_path)}"
                )

            if not isinstance(raw, str) or not raw.strip().startswith("{"):
                raise RuntimeError(
                    f"Could not retrieve iperf3 JSON output for flow "
                    f"{self.source.name} -> {self.destination.name}. "
                    "Verify iperf3 is installed in both containers and a route exists."
                )

            result = Iperf3Results(raw_json=raw, config=run_config)
            with self._lock:
                self._result = result
                self._status = FlowStatus.DONE
        except Exception as exc:
            if self._stop_event.is_set() and raw:
                print(raw)
            self._kill_server(dst_ip)
            diagnostics = self._collect_diagnostics(client_json, server_json)

            final_error = exc
            if diagnostics:
                final_error = RuntimeError(f"{exc}\n" + "\n".join(diagnostics))
                final_error.__cause__ = exc  # preserve the original traceback link

            with self._lock:
                self._error = final_error
                self._status = FlowStatus.ERROR
        finally:
            # Success has already stored its result/status above, so cleanup
            # failures here cannot discard a valid measurement. The terminal
            # status check is a last-resort guard for unexpected exit paths.
            self._remove_temp_file(self.source, client_json)
            self._remove_temp_file(self.destination, server_json)
            if self._port is not None:
                _IPERF3_PORT_ALLOCATOR.release(self._port)
            with self._lock:
                if self._status == FlowStatus.RUNNING:
                    self._error = self._error or RuntimeError(
                        "iperf3 flow terminated without recording a result"
                    )
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
    An unused server port is reserved for the run, so concurrent runs cannot
    collide; the supplied config.port is not modified. Temporary JSON files
    written inside the containers are cleaned up after the results are
    retrieved.

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

    # Reserve an exclusive port for this run; config.port is not modified.
    port = _IPERF3_PORT_ALLOCATOR.allocate()
    run_config = replace(config, port=port)
    try:
        server_cmd = (
            f'sh -c "iperf3 -s -1 -B {dst_ip} -p {port} '
            f'--json > {server_json} 2>&1"'
        )
        destination.execute_command(server_cmd, detach=True)
        _wait_for_iperf3_server(destination, dst_ip, port)

        client_cmd = run_config.build_client_command(
            dst_ip, src_ip, client_json
        )
        source.execute_command(client_cmd, detach=False)
        time.sleep(0.3)

        if run_config.protocol.upper() == "UDP":
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

        return Iperf3Results(raw_json=raw, config=run_config)
    finally:
        _IPERF3_PORT_ALLOCATOR.release(port)


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
