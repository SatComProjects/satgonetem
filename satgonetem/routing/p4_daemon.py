import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from satgonetem.models import GroundStation, Interface, Link, Node, Satellite
from satgonetem.ports import ProcessRunner, default_runner
from satgonetem.routing.base_daemon import RoutingContext, RoutingDaemon
from satgonetem.routing.graph_shared_memory import GraphSharedMemory

_READINESS_POLL_INTERVAL = 0.1
_READINESS_PROBE_TIMEOUT = 0.5
_OUTPUT_DRAIN_TIMEOUT = 1.0
_MAX_BUFFERED_STDERR_LINES = 200


def safe_int(x, default=0):
    if x is None:
        return default
    return int(x)


def safe_ip(x):
    return x if x else "0.0.0.0"


def mac_to_int(value) -> int:
    """Convert a MAC address string to an integer, defaulting to zero.

    Accepts colon- or dash-separated 48-bit addresses. Invalid or missing
    values map to ``0`` so a malformed interface does not abort graph export.
    """
    if value is None:
        return 0
    cleaned = str(value).strip().replace(":", "").replace("-", "")
    if not cleaned:
        return 0
    try:
        return int(cleaned, 16)
    except ValueError:
        return 0


def interface_port(interface: Interface) -> int:
    """Return the numeric suffix of an interface name, or zero.

    Emulated interface names are ``<node>.<id>``; the suffix is the
    container-side interface index used by the graph reader.
    """
    name = getattr(interface, "name", "") or ""
    suffix = str(name).rsplit(".", 1)[-1]
    try:
        return int(suffix)
    except ValueError:
        return 0


def build_edge_from_link(link: Link) -> dict:
    interface_source: Interface = link.peer_interfaces[0]
    interface_destination: Interface = link.peer_interfaces[1]

    src_ip = interface_source.ipv4
    dst_ip = interface_destination.ipv4

    src_mac, dst_mac = link.get_mac_addresses()

    src_node: Node = link.source
    dst_node: Node = link.target

    src_name = safe_int(getattr(src_node, "id", None))
    dst_name = safe_int(getattr(dst_node, "id", None))

    src_is_satellite = isinstance(src_node, Satellite)
    src_is_ground_station = isinstance(src_node, GroundStation)

    dst_is_satellite = isinstance(dst_node, Satellite)
    dst_is_ground_station = isinstance(dst_node, GroundStation)

    flag_gateway = 0
    if src_is_ground_station and dst_is_ground_station:
        flag_gateway = 3
    elif src_is_ground_station and dst_is_satellite:
        flag_gateway = 1
    elif src_is_satellite and dst_is_ground_station:
        flag_gateway = 2
    else:
        flag_gateway = 0

    edge = {
        "weight": 1,
        "src_name": src_name,
        "dst_name": dst_name,
        "src_ip": safe_ip(src_ip),
        "dst_ip": safe_ip(dst_ip),
        "src_mac": mac_to_int(src_mac),
        "dst_mac": mac_to_int(dst_mac),
        "src_port": interface_port(interface_source),
        "dst_port": interface_port(interface_destination),
        "flag_gateway": safe_int(flag_gateway),
    }
    return edge


class P4Daemon(RoutingDaemon):
    def __init__(
        self,
        topology_manager: RoutingContext,
        p4_controller_path: str,
        shared_memory_name: str = "satgonetem_graph",
        process_runner: ProcessRunner | None = None,
        controller_host: str = "127.0.0.1",
        controller_port: int = 9559,
        readiness_probe: Callable[[], bool] | None = None,
    ):
        super().__init__(topology_manager)
        self.graph_shared_memory: Any = None
        self.p4_controller_path = p4_controller_path
        self.shared_memory_name = shared_memory_name
        self._process_runner = process_runner or default_runner
        self.controller_host = controller_host
        self.controller_port = controller_port
        self._readiness_probe = readiness_probe
        # Process handle for the controller
        self.p4_controller_process: subprocess.Popen | None = None
        # Logger for controller output and events
        self.logger = logging.getLogger(__name__)

    def _release_graph_shared_memory(self) -> None:
        """Close and unlink the owned graph segment, if any."""
        graph = getattr(self, "graph_shared_memory", None)
        if graph is None:
            return
        try:
            graph.close()
        except Exception:
            self.logger.exception("Failed to close P4 graph shared memory")
        try:
            graph.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            self.logger.exception("Failed to unlink P4 graph shared memory")
        finally:
            self.graph_shared_memory = None

    def init(self, max_workers: int = 4) -> bool:
        self._release_graph_shared_memory()
        self.graph_shared_memory = GraphSharedMemory(
            name=self.shared_memory_name,
            max_edges=len(self.topology.links),
            create=True,
        )

        started = self.start_p4_controller()
        if not started:
            self.logger.error("P4 controller failed to start")
            self._release_graph_shared_memory()
            return False

        self.logger.info("P4 controller started")
        return True

    def _drain_controller_output(
        self,
        threads: tuple[threading.Thread, threading.Thread],
        stderr_lines: deque[str],
    ) -> None:
        """Join the output readers and report the stderr they collected.

        The reader threads own ``proc.stdout``/``proc.stderr``; the main thread
        must never read those streams concurrently. Joining before inspecting
        the buffer makes the failure output deterministic.
        """
        for thread in threads:
            thread.join(timeout=_OUTPUT_DRAIN_TIMEOUT)
        if stderr_lines:
            self.logger.error("P4 controller stderr:\n%s", "\n".join(stderr_lines))

    def _probe_controller_port(self) -> bool:
        """Return True when the controller endpoint accepts a TCP connection."""
        try:
            with socket.create_connection(
                (self.controller_host, self.controller_port),
                timeout=_READINESS_PROBE_TIMEOUT,
            ):
                return True
        except OSError:
            return False

    def start_p4_controller(self, timeout: float = 10) -> bool:
        """Start the P4 controller and wait until its gRPC endpoint is ready.

        Returns True only when the controller process is still running and the
        readiness probe succeeds within ``timeout`` seconds. The default probe
        opens a TCP connection to ``controller_host:controller_port``; pass
        ``readiness_probe`` to the constructor for a different health check.
        Returns False when the script is missing or not executable, the process
        exits, or the endpoint does not become ready before the deadline. Any
        started process is stopped on failure.
        """
        script_path = os.path.join(self.p4_controller_path, "run_controller.sh")

        if not os.path.exists(script_path):
            self.logger.error(f"Controller script not found: {script_path}")
            return False

        if not os.access(script_path, os.X_OK):
            self.logger.warning(
                f"Controller script not executable, attempting to set executable: {script_path}"
            )
            try:
                os.chmod(script_path, os.stat(script_path).st_mode | 0o111)
            except Exception as e:
                self.logger.exception(
                    f"Failed to make controller script executable: {e}"
                )
                return False

        try:
            # Start the controller without a shell to avoid shell injection and allow
            # capturing stdout/stderr. Provide no stdin to avoid interactive sudo prompts.
            proc = self._process_runner.popen(
                [script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            self.logger.exception(f"Failed to start P4 controller process: {e}")
            return False

        self.p4_controller_process = proc

        # Background threads stream stdout/stderr into the logger. stderr is
        # also buffered so the main thread can report it after joining.
        stderr_lines: deque[str] = deque(maxlen=_MAX_BUFFERED_STDERR_LINES)

        def _stream_reader(stream, log_fn, sink=None):
            try:
                for line in iter(stream.readline, ""):
                    if not line:
                        break
                    text = line.rstrip()
                    if sink is not None:
                        sink.append(text)
                    log_fn(text)
            except Exception:
                pass

        t_out = threading.Thread(
            target=_stream_reader, args=(proc.stdout, self.logger.info), daemon=True
        )
        t_err = threading.Thread(
            target=_stream_reader,
            args=(proc.stderr, self.logger.error, stderr_lines),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + timeout
        probe = self._readiness_probe or self._probe_controller_port
        while True:
            if proc.poll() is not None:
                self.logger.error(
                    f"P4 controller process exited during startup "
                    f"with code {proc.returncode}"
                )
                self.stop_p4_controller()
                self._drain_controller_output((t_out, t_err), stderr_lines)
                return False

            if probe():
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.logger.error(
                    f"P4 controller endpoint "
                    f"{self.controller_host}:{self.controller_port} did not become "
                    f"ready within {timeout} seconds"
                )
                self.stop_p4_controller()
                self._drain_controller_output((t_out, t_err), stderr_lines)
                return False

            time.sleep(min(_READINESS_POLL_INTERVAL, remaining))

    def is_p4_controller_running(self) -> bool:
        return (
            self.p4_controller_process is not None
            and self.p4_controller_process.poll() is None
        )

    def stop_p4_controller(self, timeout: int = 5) -> None:
        if self.p4_controller_process is None:
            return
        try:
            self.logger.info(
                f"Stopping P4 controller (pid={self.p4_controller_process.pid})"
            )
            self.p4_controller_process.terminate()
            try:
                self.p4_controller_process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.logger.warning(
                    "P4 controller did not terminate gracefully, killing"
                )
                self.p4_controller_process.kill()
                self.p4_controller_process.wait(timeout=timeout)
        except Exception:
            self.logger.exception("Error while stopping P4 controller")
        finally:
            self.p4_controller_process = None

    def update(self, new_links: "list[Link]", max_workers: int = 4) -> None:
        """Export a snapshot of the active topology graph to shared memory.

        The snapshot contains only links that are active and not pending
        removal, matching the graph used for route computation. If the daemon
        is not initialized, or initialization failed and released the segment,
        there is nothing to update: the call logs a warning and returns without
        raising. When topology growth exceeds the segment capacity, the segment
        is recreated with room for the new edges.

        Args:
            new_links: List of newly added links (unused; the export is a full
                snapshot of the topology).
            max_workers: Maximum number of worker threads (unused).
        """
        if self.graph_shared_memory is None:
            self.logger.warning(
                "P4 graph shared memory is not initialized; skipping graph update"
            )
            return

        edges = [
            build_edge_from_link(link)
            for link in self.topology.links.values()
            if link.is_active and not getattr(link, "to_remove", False)
        ]

        if self.graph_shared_memory.ensure_capacity(len(edges)):
            self.logger.info(
                "P4 graph shared memory resized to at least %d edges", len(edges)
            )
        self.graph_shared_memory.write_edges(edges)

    def remove(self, node: "Node | None" = None, max_workers: int = 4) -> None:
        self.stop_p4_controller()
        self._release_graph_shared_memory()
