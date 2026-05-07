import enum
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from satgonetem.models.node import Node


@dataclass
class PingConfig:
    """Configuration for a single ping run executed inside a container.

    Attributes:
        count: Number of ICMP echo requests to send. Maps to -c.
        timeout_sec: Time in seconds to wait for each reply. Maps to -W.
        interval_sec: Interval between packets in seconds. Maps to -i.
            Values below 0.2 require root inside the container.
        packet_size: Payload size in bytes. Maps to -s.
        preload: send <preload> number of packages while waiting replies. Maps to -l
    """

    count: int = 5
    timeout_sec: float = 0.5
    interval_sec: float = 0.2
    packet_size: int = 56
    preload: int = 2

    def __post_init__(self) -> None:
        if self.interval_sec < 0.002:
            raise ValueError(f"interval_sec must be > 0.002, got {self.interval_sec}")
        if self.preload < 1 or self.preload > 3:
            raise ValueError(f"preload must be > 1 and < 3, got {self.preload}")

    def build_command(self, dst_ip: str, bind_ip: str) -> str:
        """Build the ping shell command string.

        Args:
            dst_ip: Destination IP address to ping.
            bind_ip: Source IP address to bind to (-I flag).

        Returns:
            A complete ping command string ready for Node.execute_command.
        """
        return (
            f"ping -c {self.count}"
            f" -W {self.timeout_sec}"
            f" -i {self.interval_sec}"
            f" -s {self.packet_size}"
            f" -I {bind_ip}"
            f" -l {self.preload}"
            f" {dst_ip}"
        )


_STATS_RE = re.compile(
    r"(\d+) packets transmitted, (\d+) received"
    r"(?:, \+\d+ errors)?"
    r", ([\d.]+)% packet loss"
)
_RTT_RE = re.compile(r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")


class PingResults:
    """Parsed results from a single ping run.

    Extracts packet statistics and RTT values from the raw text output
    produced by the Linux ping utility.

    Attributes:
        raw_output: Unmodified ping command output string.
        config: The PingConfig instance used for the run.
    """

    def __init__(self, raw_output: str, config: PingConfig) -> None:
        """
        Args:
            raw_output: Raw text output from the ping command.
            config: The PingConfig instance used to produce this result.

        Raises:
            ValueError: If raw_output is empty or the statistics line is
                not found (indicates ping did not run successfully).
        """
        if not raw_output or not raw_output.strip():
            raise ValueError("ping returned empty output; the command may have failed.")

        self.raw_output: str = raw_output
        self.config: PingConfig = config

        stats_match = _STATS_RE.search(raw_output)
        if stats_match is None:
            raise ValueError(
                f"Could not parse ping statistics from output:\n{raw_output}"
            )

        self._transmitted: int = int(stats_match.group(1))
        self._received: int = int(stats_match.group(2))
        self._loss_percent: float = float(stats_match.group(3))

        rtt_match = _RTT_RE.search(raw_output)
        if rtt_match is not None:
            self._rtt_min: float = float(rtt_match.group(1))
            self._rtt_avg: float = float(rtt_match.group(2))
            self._rtt_max: float = float(rtt_match.group(3))
            self._rtt_mdev: float = float(rtt_match.group(4))
        else:
            self._rtt_min = 0.0
            self._rtt_avg = 0.0
            self._rtt_max = 0.0
            self._rtt_mdev = 0.0

    @property
    def packets_transmitted(self) -> int:
        """Number of ICMP packets sent."""
        return self._transmitted

    @property
    def packets_received(self) -> int:
        """Number of ICMP replies received."""
        return self._received

    @property
    def packet_loss_percent(self) -> float:
        """Packet loss as a percentage (0.0 - 100.0)."""
        return self._loss_percent

    @property
    def reachable(self) -> bool:
        """True if at least one reply was received."""
        return self._received > 0

    @property
    def rtt_min_ms(self) -> float:
        """Minimum round-trip time in milliseconds."""
        return self._rtt_min

    @property
    def rtt_avg_ms(self) -> float:
        """Average round-trip time in milliseconds."""
        return self._rtt_avg

    @property
    def rtt_max_ms(self) -> float:
        """Maximum round-trip time in milliseconds."""
        return self._rtt_max

    @property
    def rtt_mdev_ms(self) -> float:
        """Mean deviation of round-trip times in milliseconds."""
        return self._rtt_mdev

    def print_summary(self) -> None:
        """Print a human-readable summary of the ping results to stdout."""
        print("=" * 50)
        print("          PING RESULTS")
        print("=" * 50)
        print(f"Packets transmitted: {self._transmitted}")
        print(f"Packets received:    {self._received}")
        print(f"Packet loss:         {self._loss_percent:.1f}%")
        print(f"Reachable:           {self.reachable}")
        if self.reachable:
            print(f"RTT min:             {self._rtt_min:.3f} ms")
            print(f"RTT avg:             {self._rtt_avg:.3f} ms")
            print(f"RTT max:             {self._rtt_max:.3f} ms")
            print(f"RTT mdev:            {self._rtt_mdev:.3f} ms")
        print("=" * 50)

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of all parsed ping metrics and config.

        Returns:
            A dict with keys 'config' and the parsed result metrics. Suitable
            for passing directly to json.dumps().
        """
        return {
            "config": {
                "count": self.config.count,
                "timeout_sec": self.config.timeout_sec,
                "interval_sec": self.config.interval_sec,
                "packet_size": self.config.packet_size,
            },
            "packets_transmitted": self._transmitted,
            "packets_received": self._received,
            "packet_loss_percent": self._loss_percent,
            "reachable": self.reachable,
            "rtt_min_ms": self._rtt_min,
            "rtt_avg_ms": self._rtt_avg,
            "rtt_max_ms": self._rtt_max,
            "rtt_mdev_ms": self._rtt_mdev,
        }

    def __repr__(self) -> str:
        return (
            f"PingResults(received={self._received}/{self._transmitted}, "
            f"loss={self._loss_percent:.1f}%, "
            f"rtt_avg={self._rtt_avg:.3f}ms)"
        )


class PingStatus(enum.Enum):
    """Lifecycle state of a PingFlow.

    Attributes:
        IDLE: Flow created but not yet started.
        RUNNING: Ping is executing in the background.
        DONE: Ping completed successfully; results are available.
        ERROR: Ping failed; the causing exception is stored on the flow.
    """

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class PingFlow:
    """Non-blocking ping between two nodes.

    Wraps run_ping in a daemon thread so the caller is not blocked.
    All state transitions are protected by an internal lock.

    Usage::

        flow = PingFlow(source, destination, config)
        flow.start()
        while flow.status() == PingStatus.RUNNING:
            time.sleep(0.2)
        results = flow.results()
        results.print_summary()

    Attributes:
        source: Node that sends the ICMP echo requests.
        destination: Node that is pinged.
        config: PingConfig for this flow.
    """

    def __init__(
        self,
        source: "Node",
        destination: "Node",
        config: PingConfig,
        delay: float = 0.0,
    ) -> None:
        """
        Args:
            source: Node that sends the ICMP echo requests.
            destination: Node that receives and replies to them.
            config: Full ping configuration for this run.
            delay: Seconds to wait inside the background thread before
                executing the flow. The flow status is RUNNING during
                this wait period.
        """
        self.source = source
        self.destination = destination
        self.config = config
        self.delay = delay

        self._status = PingStatus.IDLE
        self._result: Optional[PingResults] = None
        self._error: Optional[Exception] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the ping in a background daemon thread.

        Returns immediately; use status() to poll for completion.

        Raises:
            RuntimeError: If the flow has already been started.
        """
        with self._lock:
            if self._status != PingStatus.IDLE:
                raise RuntimeError(
                    f"PingFlow has already been started (status={self._status.value})"
                )
            self._status = PingStatus.RUNNING
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Send SIGTERM to the running ping process and wait for the thread to exit.

        Any output produced before termination is printed to stdout.
        No-op if the flow is not currently running.
        """
        with self._lock:
            if self._status != PingStatus.RUNNING:
                return
        self._stop_event.set()
        self.source.execute_command("pkill -x ping", detach=True)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def status(self) -> PingStatus:
        """Return the current lifecycle state.

        Returns:
            The current PingStatus value.
        """
        with self._lock:
            return self._status

    def results(self) -> PingResults:
        """Return the parsed results once the flow has completed.

        Returns:
            PingResults from the completed ping.

        Raises:
            RuntimeError: If the flow has not been started, is still running,
                or completed with an error.
        """
        with self._lock:
            match self._status:
                case PingStatus.IDLE:
                    raise RuntimeError("PingFlow has not been started yet")
                case PingStatus.RUNNING:
                    raise RuntimeError("PingFlow is still running")
                case PingStatus.ERROR:
                    raise RuntimeError(
                        f"PingFlow failed: {self._error}"
                    ) from self._error
                case PingStatus.DONE:
                    return self._result  # type: ignore[return-value]
                case _ as unexpected:
                    raise TypeError(f"Unexpected PingStatus: {unexpected!r}")

    def _run(self) -> None:
        """Execute ping and update status on completion."""
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
                    f"ping from {self.source.name} to {self.destination.name} "
                    "returned no output; verify both containers are running and "
                    "a route exists."
                )
            result = PingResults(raw_output=raw, config=self.config)
            with self._lock:
                self._result = result
                self._status = PingStatus.DONE
        except Exception as exc:
            if self._stop_event.is_set() and raw:
                print(raw)
            with self._lock:
                self._error = exc
                self._status = PingStatus.ERROR


def run_ping(
    source: "Node",
    destination: "Node",
    config: PingConfig,
) -> PingResults:
    """Execute a ping from source to destination and return parsed results.

    Runs the ping command on source, bound to source.loopback.ipv4, targeting
    destination.loopback.ipv4. Blocks until the ping completes.

    Args:
        source: Node that runs the ping client.
        destination: Node that is the ping target.
        config: Full ping configuration for this run.

    Returns:
        PingResults populated with packet statistics and RTT values.

    Raises:
        ValueError: If the ping output is empty or unparseable.
        RuntimeError: If source.execute_command does not return a string
            (e.g. the container is not running).
    """
    dst_ip = destination.loopback.ipv4
    bind_ip = source.loopback.ipv4
    command = config.build_command(dst_ip, bind_ip)

    raw = source.execute_command(command)
    if not isinstance(raw, str):
        raise RuntimeError(
            f"ping from {source.name} to {destination.name} returned no output; "
            "verify both containers are running and a route exists."
        )

    return PingResults(raw_output=raw, config=config)
