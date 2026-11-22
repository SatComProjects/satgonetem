from __future__ import annotations

import enum
import shlex
import threading
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")  # non-GUI backend


if TYPE_CHECKING:
    from satgonetem.models.node import Node


import contextlib

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.flow import ThreadedFlow
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_io import (
    _extract_iperf3_json,
    _text_outside_iperf3_json,
    _wait_for_iperf3_server,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_RESULT_POLL_INTERVAL,
    _IPERF3_RESULT_WAIT_TIMEOUT,
)
from satgonetem.traffic.iperf3_results import Iperf3Results


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


class Iperf3Flow(ThreadedFlow):
    """Non-blocking iperf3 flow between two nodes.

    Wraps run_iperf3_flow in a daemon thread so the caller is not blocked.
    The thread/status/error lifecycle is inherited from ThreadedFlow, so the
    flow can be scheduled by FlowScheduler like every other traffic flow.
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

    STATUS = FlowStatus

    def __init__(
        self,
        source: Node,
        destination: Node,
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
        super().__init__(source, destination, config, delay)
        # Reserved on start so flows constructed up-front (for a scheduler
        # with hundreds of entries) do not exhaust the port pool. config.port
        # is left untouched so shared configs remain safe to reuse.
        self._port: int | None = None
        self._port_lock = threading.Lock()

    def _reserve_port(self) -> int:
        """Reserve this flow's exclusive server port on first use."""
        with self._port_lock:
            if self._port is None:
                self._port = iperf3_ports._IPERF3_PORT_ALLOCATOR.allocate()
            return self._port

    def start(self) -> None:
        """Start the iperf3 flow in a background daemon thread.

        Returns immediately; use status() to poll for completion.

        Raises:
            RuntimeError: If the flow has already been started.
        """
        self._reserve_port()
        super().start()

    def _stop_process(self) -> None:
        """Send SIGTERM to the running iperf3 client and server.

        The bracket expression ``[i]perf3`` matches the real processes but
        not the wrapper ``sh -c`` command line that carries the pattern, so
        pkill cannot signal its own parent shell.
        """
        port = self._port if self._port is not None else self.config.port
        kill_cmd = f"pkill -f '[i]perf3 .*-p {port} ' 2>/dev/null; true"
        self.source.execute_command(kill_cmd, detach=True)
        self.destination.execute_command(kill_cmd, detach=True)

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

    @staticmethod
    def _read_temp_file(node: Node, path: str) -> str | None:
        """Best-effort read of a temporary JSON file inside a node."""
        try:
            return node.execute_command(f"cat {shlex.quote(path)}")
        except Exception:
            return None

    def _read_json_output(
        self, client_json: str, server_json: str
    ) -> tuple[str | None, str | None]:
        """Read the flow's iperf3 JSON, from the preferred side first.

        The server file is written when the server's side of the test ends,
        which can be after the client returned: when it is the preferred side
        it is waited for (bounded), so the metrics come from the side
        _json_side() chose instead of silently from the other one (for UDP,
        the sender's loss figures instead of the receiver's). Text around the
        JSON object (iperf3 warnings) is skipped.

        Returns:
            Tuple of (raw JSON or None, "client" / "server" or None).
        """
        preferred_node, preferred_path, fallback_node, fallback_path = self._json_side(
            client_json, server_json
        )

        def side(path: str) -> str:
            return "client" if path == client_json else "server"

        raw = _extract_iperf3_json(self._read_temp_file(preferred_node, preferred_path))
        if raw is None and preferred_path == server_json:
            deadline = time.monotonic() + _IPERF3_RESULT_WAIT_TIMEOUT
            while raw is None and time.monotonic() < deadline:
                time.sleep(_IPERF3_RESULT_POLL_INTERVAL)
                raw = _extract_iperf3_json(
                    self._read_temp_file(preferred_node, preferred_path)
                )
        if raw is not None:
            return raw, side(preferred_path)

        raw = _extract_iperf3_json(self._read_temp_file(fallback_node, fallback_path))
        return raw, (side(fallback_path) if raw is not None else None)

    def _remove_temp_file(self, node: Node, path: str | None) -> None:
        """Best-effort removal of a temporary JSON file inside a node.

        Cleanup failures must never change the flow outcome, so every error
        is suppressed.
        """
        if not path:
            return
        with contextlib.suppress(Exception):
            node.execute_command(f"rm -f {shlex.quote(path)}", detach=True)

    def _kill_server(self, dst_ip: str | None) -> None:
        """Best-effort kill of this flow's detached one-shot iperf3 server.

        The pattern is scoped to the flow's bind address and port so that
        servers belonging to other flows are left alone. When the bind
        address could not even be resolved, fall back to matching only the
        one-shot server on this flow's port. Errors are suppressed.
        """
        port = self._port if self._port is not None else self.config.port
        if dst_ip:
            pattern = f"[i]perf3 -s -1 -B {dst_ip} -p {port}"
        else:
            pattern = f"[i]perf3 -s -1 .* -p {port}"
        with contextlib.suppress(Exception):
            self.destination.execute_command(
                f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true"
            )

    def _collect_diagnostics(
        self,
        client_json: str | None,
        server_json: str | None,
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

    def _execute(self) -> Iperf3Results:
        """Execute the iperf3 flow and return its parsed results.

        The body is guarded so that failures while resolving node addresses
        or building temp paths still produce a diagnostic error, and
        best-effort cleanup can never discard a valid measurement.
        """
        client_json: str | None = None
        server_json: str | None = None
        dst_ip: str | None = None
        raw: str | None = None
        try:
            run_id = uuid.uuid4().hex[:8]
            client_json = f"/tmp/iperf3_cli_{self.source.name}_{self.destination.name}_{run_id}.json"
            server_json = f"/tmp/iperf3_srv_{self.source.name}_{self.destination.name}_{run_id}.json"
            dst_ip = self.destination.loopback.ipv4
            src_ip = self.source.loopback.ipv4
            run_config = replace(self.config, port=self._reserve_port())

            server_cmd = (
                f"iperf3 -s -1 -B {shlex.quote(dst_ip)} -p {run_config.port} "
                f"--json > {shlex.quote(server_json)} 2>&1"
            )
            self.destination.execute_command(server_cmd, detach=True)
            if not _wait_for_iperf3_server(self.destination, dst_ip, run_config.port):
                raise RuntimeError(
                    f"iperf3 server did not start on {dst_ip}:{run_config.port}; "
                    f"client {self.source.name} -> {self.destination.name} "
                    "was not started."
                )

            client_cmd = run_config.build_client_command(dst_ip, src_ip, client_json)
            client_started = time.monotonic()
            self.source.execute_command(client_cmd, detach=False)
            client_seconds = time.monotonic() - client_started
            time.sleep(0.3)

            client_text = self._read_temp_file(self.source, client_json)
            raw, json_side = self._read_json_output(client_json, server_json)
            if raw is None:
                raise RuntimeError(
                    f"Could not retrieve iperf3 JSON output for flow "
                    f"{self.source.name} -> {self.destination.name}. "
                    "Verify iperf3 is installed in both containers and a route exists. "
                    f"(client returned after {client_seconds:.1f} s, configured "
                    f"duration {run_config.duration} s; waited up to "
                    f"{_IPERF3_RESULT_WAIT_TIMEOUT:.0f} s for the server file)"
                )

            result = Iperf3Results(raw_json=raw, config=run_config)
            result.json_side = json_side
            result.client_notes = _text_outside_iperf3_json(client_text)
            return result
        except Exception as exc:
            if self._stop_event.is_set() and raw:
                print(raw)
            self._kill_server(dst_ip)
            diagnostics = self._collect_diagnostics(client_json, server_json)
            if diagnostics:
                raise RuntimeError(f"{exc}\n" + "\n".join(diagnostics)) from exc
            raise
        finally:
            # Cleanup is best-effort and runs on every path so a failed flow
            # never leaks temp files or keeps a port reserved.
            self._remove_temp_file(self.source, client_json)
            self._remove_temp_file(self.destination, server_json)
            if self._port is not None:
                iperf3_ports._IPERF3_PORT_ALLOCATOR.release(self._port)
