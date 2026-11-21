"""hping3 configuration and parsed results."""

from __future__ import annotations

import contextlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

_TCP_FLAG_MAP: dict[str, str] = {
    "S": "-S",
    "A": "-A",
    "F": "-F",
    "R": "-R",
    "P": "-P",
    "U": "-U",
}

_VALID_PROTOCOLS: frozenset[str] = frozenset({"tcp", "udp", "icmp"})
_VALID_RATE_TYPES: frozenset[str] = frozenset({"interval", "fast", "faster"})

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
            'fast' (--fast), 'faster' (--faster). 'flood' is rejected because
            --flood ignores -c and never terminates on its own.
        interval: Packet interval string, used when rate_type is 'interval'.
            Prefix 'u' for microseconds (e.g. 'u10000' for 10 ms), plain
            integer for seconds (e.g. '1'). Maps to -i.
        flags: List of TCP flag characters to assert. Only applied when
            proto is 'tcp'. Valid values: 'S', 'A', 'F', 'R', 'P', 'U'.
        spoof_src: Source IP address to spoof. Maps to -a. When set, takes
            precedence over the bind_ip argument in build_command. When None,
            build_command falls back to its bind_ip parameter.

    Raises:
        ValueError: If count is not a positive integer, proto is not one of
            'tcp', 'udp', or 'icmp', rate_type is not one of 'interval',
            'fast', or 'faster', or flags contains an unsupported TCP flag.
    """

    proto: str = "tcp"
    dport: int = 0
    sport: int | None = None
    count: int = 100
    size: int = 0
    ttl: int | None = None
    rate_type: str = "interval"
    interval: str = ""
    flags: list[str] = field(default_factory=list)
    spoof_src: str | None = None

    def __post_init__(self) -> None:
        """Reject configurations that cannot produce a finite hping3 run.

        Raises:
            ValueError: If any configuration field is invalid.
        """
        self._validate()

    def _validate(self) -> None:
        """Validate this configuration before a command is built.

        Called from __post_init__ and again from build_command so that
        mutations after construction cannot smuggle an invalid or
        non-terminating hping3 command through.

        Raises:
            ValueError: If any configuration field is invalid.
        """
        proto = self.proto.lower() if isinstance(self.proto, str) else self.proto
        if proto not in _VALID_PROTOCOLS:
            raise ValueError(
                f"proto must be one of {sorted(_VALID_PROTOCOLS)}, got {self.proto!r}"
            )

        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise ValueError(f"count must be an integer >= 1, got {self.count!r}")
        if self.count < 1:
            raise ValueError(
                f"count must be >= 1 for a finite hping3 run, got {self.count}; "
                "a non-positive count makes hping3 send indefinitely"
            )

        if self.rate_type == "flood":
            raise ValueError(
                "rate_type='flood' is not supported for finite hping3 runs: "
                "--flood ignores -c and never terminates; use 'fast' or "
                "'faster' instead"
            )
        if self.rate_type not in _VALID_RATE_TYPES:
            raise ValueError(
                f"rate_type must be one of {sorted(_VALID_RATE_TYPES)}, "
                f"got {self.rate_type!r}; 'flood' is intentionally unsupported "
                "because --flood ignores -c"
            )

        invalid_flags = [f for f in self.flags if f not in _TCP_FLAG_MAP]
        if invalid_flags:
            raise ValueError(
                f"flags contains unsupported TCP flag(s) {invalid_flags}; "
                f"valid values are {sorted(_TCP_FLAG_MAP)}"
            )

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

        Raises:
            ValueError: If the configuration is invalid. In particular,
                non-positive counts and rate_type='flood' are rejected so a
                finite flow can never turn into an unbounded flood.
        """
        self._validate()
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
            parts.extend(_TCP_FLAG_MAP[f] for f in self.flags if f in _TCP_FLAG_MAP)

        if self.rate_type == "interval" and self.interval:
            parts.append(f"-i {self.interval}")
        elif self.rate_type == "fast":
            parts.append("--fast")
        elif self.rate_type == "faster":
            parts.append("--faster")

        src = self.spoof_src or bind_ip
        if src:
            parts.append(f"-a {src}")

        # hping3 writes its final statistics to stderr; keep them in stdout.
        return " ".join(parts) + " 2>&1"


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

        self._payload_bytes: int | None = None
        payload_match = _PAYLOAD_RE.search(raw_output)
        if payload_match:
            with contextlib.suppress(ValueError):
                self._payload_bytes = int(payload_match.group(1))

        self._rtt_ms: list[float] = []
        for line in raw_output.splitlines():
            m = _RTT_LINE_RE.search(line)
            if m:
                try:
                    self._rtt_ms.append(float(m.group(1)))
                except ValueError:
                    self._rtt_ms.append(math.nan)

        self._seq: list[int] = list(range(1, len(self._rtt_ms) + 1))

        pb = self._payload_bytes or 0
        acc = 0.0
        self._cumulative_mbit: list[float] = []
        for _ in self._seq:
            acc += pb * 8.0 / 1_000_000.0
            self._cumulative_mbit.append(acc)

        self._transmitted: int | None = None
        self._received: int = len([x for x in self._rtt_ms if not math.isnan(x)])
        self._loss_percent: float | None = None

        stats_match = _STATS_RE.search(raw_output)
        if stats_match:
            try:
                self._transmitted = int(stats_match.group(1))
                self._received = int(stats_match.group(2))
                self._loss_percent = float(stats_match.group(3))
            except ValueError:
                pass

        valid_rtts = [x for x in self._rtt_ms if not math.isnan(x)]
        self._rtt_min: float | None = min(valid_rtts) if valid_rtts else None
        self._rtt_avg: float | None = (
            sum(valid_rtts) / len(valid_rtts) if valid_rtts else None
        )
        self._rtt_max: float | None = max(valid_rtts) if valid_rtts else None

    @property
    def payload_bytes(self) -> int | None:
        """Payload data size per packet in bytes, or None if not reported."""
        return self._payload_bytes

    @property
    def seq(self) -> list[int]:
        """Packet sequence numbers for received replies (1-based)."""
        return list(self._seq)

    @property
    def rtt_ms(self) -> list[float]:
        """Per-packet RTT values in milliseconds. NaN for missing replies."""
        return list(self._rtt_ms)

    @property
    def cumulative_mbit(self) -> list[float]:
        """Cumulative bits transmitted in megabits, one value per reply."""
        return list(self._cumulative_mbit)

    @property
    def packets_transmitted(self) -> int | None:
        """Total packets sent, or None if the summary line was not found."""
        return self._transmitted

    @property
    def packets_received(self) -> int:
        """Total packets that received a reply."""
        return self._received

    @property
    def packet_loss_percent(self) -> float | None:
        """Packet loss percentage (0.0 - 100.0), or None if not available."""
        return self._loss_percent

    @property
    def rtt_min_ms(self) -> float | None:
        """Minimum RTT in milliseconds across all received replies."""
        return self._rtt_min

    @property
    def rtt_avg_ms(self) -> float | None:
        """Average RTT in milliseconds across all received replies."""
        return self._rtt_avg

    @property
    def rtt_max_ms(self) -> float | None:
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
        return f"Hping3Results(received={self._received}, rtt_avg={self._rtt_avg})"
