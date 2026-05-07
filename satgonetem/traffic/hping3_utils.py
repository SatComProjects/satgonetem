import enum
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from satgonetem.models.node import Node

_TCP_FLAG_MAP: dict[str, str] = {
    "S": "-S",
    "A": "-A",
    "F": "-F",
    "R": "-R",
    "P": "-P",
    "U": "-U",
}

_STATS_RE = re.compile(
    r"(\d+) packets transmitted,\s*(\d+) packets received"
    r",\s*([0-9.]+)% packet loss"
)
_RTT_LINE_RE = re.compile(r"(?:rtt|time)=([0-9]+\.?[0-9]*)\s*ms")
_PAYLOAD_RE = re.compile(r"(\d+) data bytes")


@dataclass
class Hping3Config:
    """Configuration for a single hping3 run executed inside a container.

    Attributes:
        proto: Transport protocol. Accepted values: 'tcp', 'udp', 'icmp'.
        dport: Destination port. Ignored for ICMP. Maps to -p.
        sport: Source port override. None means hping3 chooses. Maps to -s.
        count: Number of packets to send. Maps to -c.
        size: Payload data size in bytes. 0 means no extra payload. Maps to -d.
        ttl: IP time-to-live override. None uses the system default. Maps
            to --ttl.
        rate_type: Sending rate mode. One of: 'interval' (use interval field),
            'fast' (--fast), 'faster' (--faster), 'flood' (--flood).
        interval: Packet interval string, used when rate_type is 'interval'.
            Prefix 'u' for microseconds (e.g. 'u10000' for 10 ms), plain
            integer for seconds (e.g. '1'). Maps to -i.
        flags: List of TCP flag characters to assert. Only applied when
            proto is 'tcp'. Valid values: 'S', 'A', 'F', 'R', 'P', 'U'.
        spoof_src: Source IP address to spoof. Maps to -a. When set, takes
            precedence over the bind_ip argument in build_command. When None,
            build_command falls back to its bind_ip parameter.
    """

    proto: str = "tcp"
    dport: int = 0
    sport: Optional[int] = None
    count: int = 100
    size: int = 0
    ttl: Optional[int] = None
    rate_type: str = "interval"
    interval: str = ""
    flags: List[str] = field(default_factory=list)
    spoof_src: Optional[str] = None

    def build_command(self, dst_ip: str, bind_ip: str) -> str:
        """Build the hping3 shell command string.

        Assembles all configured options into a command that can be passed
        directly to Node.execute_command. The spoof source address is taken
        from spoof_src if set, otherwise from bind_ip. The -a flag is omitted
        only when both are absent.

        Args:
            dst_ip: Destination IP address to send packets to.
            bind_ip: Fallback source IP address for the -a flag, used when
                spoof_src is not set on this config.

        Returns:
            A complete hping3 command string ready for Node.execute_command.
        """
        parts = ["hping3", dst_ip, f"-c {self.count}"]

        proto = self.proto.lower()
        if proto == "udp":
            parts.append("--udp")
        elif proto == "icmp":
            parts.append("--icmp")

        if proto != "icmp" and self.dport:
            parts.append(f"-p {self.dport}")
        if self.sport is not None:
            parts.append(f"-s {self.sport}")
        if self.size:
            parts.append(f"-d {self.size}")
        if self.ttl is not None:
            parts.append(f"--ttl {self.ttl}")

        if proto == "tcp" and self.flags:
            parts.extend(
                _TCP_FLAG_MAP[f] for f in self.flags if f in _TCP_FLAG_MAP
            )

        if self.rate_type == "interval" and self.interval:
            parts.append(f"-i {self.interval}")
        elif self.rate_type == "fast":
            parts.append("--fast")
        elif self.rate_type == "faster":
            parts.append("--faster")
        elif self.rate_type == "flood":
            parts.append("--flood")

        src = self.spoof_src or bind_ip
        if src:
            parts.append(f"-a {src}")

        return " ".join(parts)


class Hping3Results:
    """Parsed results from a single hping3 run.

    Extracts per-packet RTT values and the summary statistics from the raw
    text output produced by hping3. Cumulative transmitted bits are computed
    from per-packet payload size reported in the header.

    Attributes:
        raw_output: Unmodified hping3 command output string.
        config: The Hping3Config instance used for the run.
    """

    def __init__(self, raw_output: str, config: Hping3Config) -> None:
        """
        Args:
            raw_output: Raw text output from the hping3 command.
            config: The Hping3Config instance used to produce this result.

        Raises:
            ValueError: If raw_output is empty.
        """
        if not raw_output or not raw_output.strip():
            raise ValueError(
                "hping3 returned empty output; the command may have failed."
            )

        self.raw_output: str = raw_output
        self.config: Hping3Config = config

        self._payload_bytes: Optional[int] = None
        payload_match = _PAYLOAD_RE.search(raw_output)
        if payload_match:
            try:
                self._payload_bytes = int(payload_match.group(1))
            except ValueError:
                pass

        self._rtt_ms: List[float] = []
        for line in raw_output.splitlines():
            m = _RTT_LINE_RE.search(line)
            if m:
                try:
                    self._rtt_ms.append(float(m.group(1)))
                except ValueError:
                    self._rtt_ms.append(math.nan)

        self._seq: List[int] = list(range(1, len(self._rtt_ms) + 1))

        pb = self._payload_bytes or 0
        acc = 0.0
        self._cumulative_mbit: List[float] = []
        for _ in self._seq:
            acc += pb * 8.0 / 1_000_000.0
            self._cumulative_mbit.append(acc)

        self._transmitted: Optional[int] = None
        self._received: int = len([x for x in self._rtt_ms if not math.isnan(x)])
        self._loss_percent: Optional[float] = None

        stats_match = _STATS_RE.search(raw_output)
        if stats_match:
            try:
                self._transmitted = int(stats_match.group(1))
                self._received = int(stats_match.group(2))
                self._loss_percent = float(stats_match.group(3))
            except ValueError:
                pass

        valid_rtts = [x for x in self._rtt_ms if not math.isnan(x)]
        self._rtt_min: Optional[float] = min(valid_rtts) if valid_rtts else None
        self._rtt_avg: Optional[float] = (
            sum(valid_rtts) / len(valid_rtts) if valid_rtts else None
        )
        self._rtt_max: Optional[float] = max(valid_rtts) if valid_rtts else None

    @property
    def payload_bytes(self) -> Optional[int]:
        """Payload data size per packet in bytes, or None if not reported."""
        return self._payload_bytes

    @property
    def seq(self) -> List[int]:
        """Packet sequence numbers for received replies (1-based)."""
        return list(self._seq)

    @property
    def rtt_ms(self) -> List[float]:
        """Per-packet RTT values in milliseconds. NaN for missing replies."""
        return list(self._rtt_ms)

    @property
    def cumulative_mbit(self) -> List[float]:
        """Cumulative bits transmitted in megabits, one value per reply."""
        return list(self._cumulative_mbit)

    @property
    def packets_transmitted(self) -> Optional[int]:
        """Total packets sent, or None if the summary line was not found."""
        return self._transmitted

    @property
    def packets_received(self) -> int:
        """Total packets that received a reply."""
        return self._received

    @property
    def packet_loss_percent(self) -> Optional[float]:
        """Packet loss percentage (0.0 - 100.0), or None if not available."""
        return self._loss_percent

    @property
    def rtt_min_ms(self) -> Optional[float]:
        """Minimum RTT in milliseconds across all received replies."""
        return self._rtt_min

    @property
    def rtt_avg_ms(self) -> Optional[float]:
        """Average RTT in milliseconds across all received replies."""
        return self._rtt_avg

    @property
    def rtt_max_ms(self) -> Optional[float]:
        """Maximum RTT in milliseconds across all received replies."""
        return self._rtt_max

    @property
    def reachable(self) -> bool:
        """True if at least one reply was received."""
        return self._received > 0

    def print_summary(self) -> None:
        """Print a human-readable summary of the hping3 results to stdout."""
        print("=" * 50)
        print("        HPING3 RESULTS")
        print("=" * 50)
        if self._transmitted is not None:
            print(f"Packets transmitted: {self._transmitted}")
        print(f"Packets received:    {self._received}")
        if self._loss_percent is not None:
            print(f"Packet loss:         {self._loss_percent:.1f}%")
        print(f"Reachable:           {self.reachable}")
        if self._rtt_min is not None:
            print(f"RTT min:             {self._rtt_min:.3f} ms")
        if self._rtt_avg is not None:
            print(f"RTT avg:             {self._rtt_avg:.3f} ms")
        if self._rtt_max is not None:
            print(f"RTT max:             {self._rtt_max:.3f} ms")
        print("=" * 50)

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of all parsed hping3 metrics and config.

        Per-packet RTT values that are NaN (no reply received) are mapped to null.

        Returns:
            A dict with keys 'config' and the parsed result metrics. Suitable
            for passing directly to json.dumps().
        """
        return {
            "config": {
                "proto": self.config.proto,
                "dport": self.config.dport,
                "sport": self.config.sport,
                "count": self.config.count,
                "size": self.config.size,
                "ttl": self.config.ttl,
                "rate_type": self.config.rate_type,
                "interval": self.config.interval,
                "flags": list(self.config.flags),
                "spoof_src": self.config.spoof_src,
            },
            "packets_transmitted": self._transmitted,
            "packets_received": self._received,
            "packet_loss_percent": self._loss_percent,
            "reachable": self.reachable,
            "payload_bytes": self._payload_bytes,
            "rtt_min_ms": self._rtt_min,
            "rtt_avg_ms": self._rtt_avg,
            "rtt_max_ms": self._rtt_max,
            "rtt_ms": [None if math.isnan(x) else x for x in self._rtt_ms],
        }

    def __repr__(self) -> str:
        return (
            f"Hping3Results(received={self._received}, "
            f"rtt_avg={self._rtt_avg})"
        )


class Hping3Status(enum.Enum):
    """Lifecycle state of an Hping3Flow.

    Attributes:
        IDLE: Flow created but not yet started.
        RUNNING: hping3 is executing in the background.
        DONE: hping3 completed successfully; results are available.
        ERROR: hping3 failed; the causing exception is stored on the flow.
    """

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class Hping3Flow:
    """Non-blocking hping3 flow between two nodes.

    Wraps run_hping3 in a daemon thread so the caller is not blocked.
    All state transitions are protected by an internal lock.

    Usage::

        flow = Hping3Flow(source, destination, config)
        flow.start()
        while flow.status() == Hping3Status.RUNNING:
            time.sleep(0.2)
        results = flow.results()
        results.print_summary()

    Attributes:
        source: Node that sends hping3 packets.
        destination: Node that is the hping3 target.
        config: Hping3Config for this flow.
    """

    def __init__(
        self,
        source: "Node",
        destination: "Node",
        config: Hping3Config,
        delay: float = 0.0,
    ) -> None:
        """
        Args:
            source: Node that sends the hping3 packets.
            destination: Node that is the target.
            config: Full hping3 configuration for this run.
            delay: Seconds to wait inside the background thread before
                executing the flow. The flow status is RUNNING during
                this wait period.
        """
        self.source = source
        self.destination = destination
        self.config = config
        self.delay = delay

        self._status = Hping3Status.IDLE
        self._result: Optional[Hping3Results] = None
        self._error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start hping3 in a background daemon thread.

        Returns immediately; use status() to poll for completion.

        Raises:
            RuntimeError: If the flow has already been started.
        """
        with self._lock:
            if self._status != Hping3Status.IDLE:
                raise RuntimeError(
                    f"Hping3Flow has already been started (status={self._status.value})"
                )
            self._status = Hping3Status.RUNNING
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Send SIGTERM to the running hping3 process and wait for the thread to exit.

        Any output produced before termination is printed to stdout.
        No-op if the flow is not currently running.
        """
        with self._lock:
            if self._status != Hping3Status.RUNNING:
                return
        self._stop_event.set()
        self.source.execute_command("pkill -x hping3", detach=True)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def status(self) -> Hping3Status:
        """Return the current lifecycle state.

        Returns:
            The current Hping3Status value.
        """
        with self._lock:
            return self._status

    def results(self) -> Hping3Results:
        """Return the parsed results once the flow has completed.

        Returns:
            Hping3Results from the completed flow.

        Raises:
            RuntimeError: If the flow has not been started, is still running,
                or completed with an error.
        """
        with self._lock:
            match self._status:
                case Hping3Status.IDLE:
                    raise RuntimeError("Hping3Flow has not been started yet")
                case Hping3Status.RUNNING:
                    raise RuntimeError("Hping3Flow is still running")
                case Hping3Status.ERROR:
                    raise RuntimeError(
                        f"Hping3Flow failed: {self._error}"
                    ) from self._error
                case Hping3Status.DONE:
                    return self._result  # type: ignore[return-value]
                case _ as unexpected:
                    raise TypeError(f"Unexpected Hping3Status: {unexpected!r}")

    def _run(self) -> None:
        """Execute hping3 and update status on completion."""
        if self.delay > 0.0:
            time.sleep(self.delay)
        raw: Optional[str] = None
        try:
            dst_ip = self.destination.loopback.ipv4
            bind_ip = self.source.loopback.ipv4
            command = self.config.build_command(dst_ip, bind_ip)
            raw = self.source.execute_command(command)
            if not isinstance(raw, str):
                raise RuntimeError(
                    f"hping3 from {self.source.name} to {self.destination.name} "
                    "returned no output; verify both containers are running and "
                    "a route exists."
                )
            result = Hping3Results(raw_output=raw, config=self.config)
            with self._lock:
                self._result = result
                self._status = Hping3Status.DONE
        except Exception as exc:
            if self._stop_event.is_set() and raw:
                print(raw)
            with self._lock:
                self._error = exc
                self._status = Hping3Status.ERROR


def run_hping3(
    source: "Node",
    destination: "Node",
    config: Hping3Config,
) -> Hping3Results:
    """Execute hping3 from source to destination and return parsed results.

    Runs hping3 on source, spoofing source.loopback.ipv4 as the sender
    address (-a flag), targeting destination.loopback.ipv4. Blocks until
    hping3 exits (i.e. until count packets have been sent).

    Args:
        source: Node that runs hping3.
        destination: Node that is the target.
        config: Full hping3 configuration for this run.

    Returns:
        Hping3Results populated with per-packet RTT values and summary
        statistics parsed from the hping3 output.

    Raises:
        ValueError: If hping3 returns empty output.
        RuntimeError: If source.execute_command does not return a string.
    """
    dst_ip = destination.loopback.ipv4
    bind_ip = source.loopback.ipv4
    command = config.build_command(dst_ip, bind_ip)

    raw = source.execute_command(command)
    if not isinstance(raw, str):
        raise RuntimeError(
            f"hping3 from {source.name} to {destination.name} returned no output; "
            "verify both containers are running and a route exists."
        )

    return Hping3Results(raw_output=raw, config=config)
