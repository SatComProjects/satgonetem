import logging
import os
import subprocess
import threading
import time
from typing import List, Optional

from satgonetem.models import GroundStation, Interface, Link, Node, Satellite
from satgonetem.ports import ProcessRunner, default_runner
from satgonetem.routing import RoutingDaemon
from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.utils.graph_shared_memory import GraphSharedMemory


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
        topology_manager: TopologyManager,
        p4_controller_path: str,
        shared_memory_name: str = "satgonetem_graph",
        process_runner: ProcessRunner | None = None,
    ):
        super().__init__(topology_manager)
        self.graph_shared_memory = None
        self.p4_controller_path = p4_controller_path
        self.shared_memory_name = shared_memory_name
        self._process_runner = process_runner or default_runner
        # Process handle for the controller
        self.p4_controller_process: Optional[subprocess.Popen] = None
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
            create=True
        )

        started = self.start_p4_controller()
        if not started:
            self.logger.error("P4 controller failed to start")
            self._release_graph_shared_memory()
            return False

        self.logger.info("P4 controller started")
        return True

    def start_p4_controller(self, timeout: int = 10) -> bool:
        """Start the P4 controller script located in self.p4_controller_path.

        Returns True if the process was launched and the gRPC port became available
        within `timeout` seconds; otherwise False.
        """
        script_path = os.path.join(self.p4_controller_path, "run_controller.sh")

        if not os.path.exists(script_path):
            self.logger.error(f"Controller script not found: {script_path}")
            return False

        if not os.access(script_path, os.X_OK):
            self.logger.warning(f"Controller script not executable, attempting to set executable: {script_path}")
            try:
                os.chmod(script_path, os.stat(script_path).st_mode | 0o111)
            except Exception as e:
                self.logger.exception(f"Failed to make controller script executable: {e}")
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

        # Background threads to stream stdout/stderr into logger
        def _stream_reader(stream, log_fn):
            try:
                for line in iter(stream.readline, ''):
                    if not line:
                        break
                    log_fn(line.rstrip())
            except Exception:
                pass

        t_out = threading.Thread(target=_stream_reader, args=(proc.stdout, self.logger.info), daemon=True)
        t_err = threading.Thread(target=_stream_reader, args=(proc.stderr, self.logger.error), daemon=True)
        t_out.start()
        t_err.start()

        # Give the process a short moment to fail fast if something is wrong
        time.sleep(0.5)
        if proc.poll() is not None:
            self.logger.error(f"P4 controller process exited immediately with code {proc.returncode}")
            # Read any remaining stderr if available
            try:
                err = proc.stderr.read() if proc.stderr is not None else None
                if err:
                    self.logger.error(err)
            except Exception:
                pass
            self.stop_p4_controller()
            return False

        return True

    def is_p4_controller_running(self) -> bool:
        return self.p4_controller_process is not None and self.p4_controller_process.poll() is None

    def stop_p4_controller(self, timeout: int = 5) -> None:
        if self.p4_controller_process is None:
            return
        try:
            self.logger.info(f"Stopping P4 controller (pid={self.p4_controller_process.pid})")
            self.p4_controller_process.terminate()
            try:
                self.p4_controller_process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.logger.warning("P4 controller did not terminate gracefully, killing")
                self.p4_controller_process.kill()
                self.p4_controller_process.wait(timeout=timeout)
        except Exception:
            self.logger.exception("Error while stopping P4 controller")
        finally:
            self.p4_controller_process = None

    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
        edges = []

        for link in self.topology.links.values():
            edge = build_edge_from_link(link)
            edges.append(edge)

        self.graph_shared_memory.write_edges(edges)


    def remove(self, node: "Optional[Node]" = None, max_workers: int = 4) -> None:
        self.stop_p4_controller()
        self._release_graph_shared_memory()