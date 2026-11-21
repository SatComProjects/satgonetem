from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # non-GUI backend


if TYPE_CHECKING:
    pass


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
    window_size: str | None = None
    mss: int | None = None
    no_delay: bool = False
    length: str | None = None
    pacing_timer: int | None = None
    reverse: bool = False
    bidir: bool = False
    tos: int | None = None
    ttl: int | None = None
    num_bytes: int | None = None
    omit: int = 0
    affinity: str | None = None

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
            raise ValueError(f"bandwidth_mbps must be >= 0, got {self.bandwidth_mbps}")

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
