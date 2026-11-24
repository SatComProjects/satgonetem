import enum
import shlex
from typing import TYPE_CHECKING

from satgonetem.traffic.flow import ThreadedFlow
from satgonetem.traffic.hping3_types import Hping3Config, Hping3Results

if TYPE_CHECKING:
    from satgonetem.models.node import Node


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


class Hping3Flow(ThreadedFlow):
    """Non-blocking hping3 flow between two nodes."""

    STATUS = Hping3Status
    flow_name = "Hping3Flow"

    def _execute(self) -> Hping3Results:
        dst_ip = self.destination.loopback.ipv4
        bind_ip = self.source.loopback.ipv4
        command = self.config.build_command(dst_ip, bind_ip)
        # hping3 exits 1 when no replies arrive; the summary is still valid.
        raw = self.source.execute_command(command, ok_exit_codes=(0, 1))
        if not isinstance(raw, str):
            raise RuntimeError(
                f"hping3 from {self.source.name} to {self.destination.name} "
                "returned no output; verify both containers are running and "
                "a route exists."
            )
        self._last_raw = raw
        return Hping3Results(raw_output=raw, config=self.config)

    def _stop_process(self) -> None:
        dst_ip = self.destination.loopback.ipv4
        pattern = f"[h]ping3 .* {dst_ip}"
        self.source.execute_command(
            f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true", detach=True
        )


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

    # hping3 exits 1 when no replies arrive; the summary is still valid.
    raw = source.execute_command(command, ok_exit_codes=(0, 1))
    if not isinstance(raw, str):
        raise RuntimeError(
            f"hping3 from {source.name} to {destination.name} returned no output; "
            "verify both containers are running and a route exists."
        )

    return Hping3Results(raw_output=raw, config=config)
