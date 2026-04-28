"""DiagnosticsMixin for TopologyManager."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite
from satgonetem.utils.constants import MAX_WORKERS
from satgonetem.utils.coverage import coverage_percentage_fast

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.services.topology_satcom import TopologyManager
    from satgonetem.models.node import Node
    from typing import Any, Dict, List, Optional


class DiagnosticsMixin:
    """Diagnostics functionality."""

    def monitor_links(self, debug: bool = False):
        """
        Method to monitor links in a threaded manner.

        Reads interface usage files and retrieves bandwidth metrics for each interface.
        Each file contains JSON objects where the last line represents the current state
        and previous lines represent historical states.

        Returns:
            List of dictionaries with link usage data:
            - src: Source node name
            - dst: Destination node name
            - value: Bandwidth usage in bits per second (from interface monitoring)
            - type: Link type ('InterSatelliteLink' or 'GroundStationLink')

        File format: JSON objects with interface names as keys and their bandwidth usage
        in bits per second as values. Example:
        {"eth0": 656, "eth55": 272974304, ...}
        """
        if not hasattr(self, "links") or not isinstance(self.links, dict):
            return []

        out = []
        link_usage_map = {}  # Track usage by link key to avoid duplicates
        lock = threading.Lock()  # Protect concurrent access
        max_workers = min(MAX_WORKERS, len(self.links))

        log_path = "/tmp/interfaces/"
        paths = [
            f"{log_path}{self.project_name}.{sat.name}" for sat in self.get_satellites()
        ]

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._process_interface_file, path): sat.name
                for path, sat in zip(paths, self.get_satellites())
            }

            for fut in as_completed(futures):
                sat_name = futures[fut]
                try:
                    interface_stats = fut.result()
                    if interface_stats:
                        logging.debug(
                            f"Link monitor stats for {sat_name}: {interface_stats}"
                        )
                        # Process the interface stats to extract link usage
                        for iface, val in interface_stats.items():
                            # iface format: 'project.SatX' or 'project.GndX'
                            node_part = (
                                iface.split(".", 1)[1] if "." in iface else iface
                            )
                            peer_id = node_part[3:]  # strip 'Sat' or 'Gnd' prefix
                            iface_sat = f"Sat{peer_id}"
                            iface_gnd = f"Gnd{peer_id}"
                            match self.links.get(frozenset((sat_name, iface_gnd))):
                                case Link() as link:
                                    pass
                                case None:
                                    match self.links.get(
                                        frozenset((sat_name, iface_sat))
                                    ):
                                        case Link() as link:
                                            pass
                                        case None:
                                            logging.debug(
                                                f"No link found for interface {iface} on satellite {sat_name}"
                                            )
                                            continue
                                        case _ as unexpected:
                                            raise TypeError(
                                                f"Expected Link in links, got {type(unexpected)}"
                                            )
                                case _ as unexpected:
                                    raise TypeError(
                                        f"Expected Link in links, got {type(unexpected)}"
                                    )
                            try:

                                # Use the proper link key for consistency
                                link_key = self._build_link_key(
                                    link.source, link.target
                                )
                                usage_bps = int(val or 0)

                                # Store link data (thread-safe)
                                with lock:
                                    # Use maximum usage if link appears in multiple files
                                    if link_key not in link_usage_map:
                                        src_name = (
                                            getattr(link.source, "name", None)
                                            or f"Sat{getattr(link.source, 'topology_uniq_id', 'Unknown')}"
                                        )
                                        dst_name = (
                                            getattr(link.target, "name", None)
                                            or f"Sat{getattr(link.target, 'topology_uniq_id', 'Unknown')}"
                                        )
                                        link_usage_map[link_key] = {
                                            "src": src_name,
                                            "dst": dst_name,
                                            "value": usage_bps,
                                            "type": getattr(link, "type", None),
                                            "link": link,
                                        }
                                    else:
                                        # Keep the maximum usage value for this link
                                        current_usage = link_usage_map[link_key][
                                            "value"
                                        ]
                                        if usage_bps > current_usage:
                                            link_usage_map[link_key][
                                                "value"
                                            ] = usage_bps

                                # Also update the link object's tx value
                                link.tx = usage_bps
                                link.rx = 0  # Not measured

                            except (
                                AttributeError,
                                KeyError,
                                TypeError,
                                ValueError,
                            ) as e:
                                logging.warning(
                                    f"Error processing interface {iface} on {sat_name}: {e}"
                                )
                                continue
                except FileNotFoundError:
                    logging.debug(f"No interface stats file found for {sat_name}")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    logging.error(
                        f"Error processing interface file for {sat_name}: {e}"
                    )

        # Convert map to output list (remove link object reference)
        out = [
            {k: v for k, v in item.items() if k != "link"}
            for item in link_usage_map.values()
        ]

        if debug:
            # Sort by usage value (highest to lowest) and get top 20
            out_sorted = sorted(out, key=lambda x: x["value"], reverse=True)[:20]
            logging.info(f"Top 20 links by usage:")
            for link_data in out_sorted:
                logging.info(
                    f"  {link_data['src']} → {link_data['dst']}: {link_data['value']:,} bps ({link_data['type']})"
                )

        return out

    def _process_interface_file(self, file_path: str) -> dict[str, int]:
        """
        Process an interface monitoring file and return current interface usage.

        Args:
            file_path: Path to the interface usage file

        Returns:
            Dictionary mapping interface names to their bandwidth usage in bits per second.
            Example: {"eth0": 656, "eth55": 272974304}
        """
        try:
            with open(file_path, "r") as f:
                lines = f.readlines()

            if not lines:
                return {}

            # Get the last line which represents the current state
            last_line = lines[-1].strip()
            if not last_line:
                return {}

            current_state = json.loads(last_line)
            return current_state
        except FileNotFoundError:
            raise
        except json.JSONDecodeError as e:
            logging.warning(f"Failed to parse JSON from {file_path}: {e}")
            return {}
        except OSError as e:
            logging.error(f"Error reading interface file {file_path}: {e}")
            raise

    def launch_tcpdump_on_satellites(
        self, dump_dir: str = "/tmp/dump", satellites: list = list()
    ) -> None:
        """
        Launch tcpdump on each interface of each satellite and dump traffic to .pcap files.

        For every satellite, starts a detached tcpdump process per interface writing to:
            <dump_dir>/<satellite_name>_<interface_name>.pcap

        /tmp is shared across all satellite containers, so the directory is created once
        on the host before spawning workers.

        :param dump_dir: Directory where .pcap files are written.
                         Defaults to /tmp/interfaces/dump.
        """
        sats = [
            s for s in satellites if hasattr(s, "container") and s.container is not None
        ]

        if not sats:
            logging.warning(
                "No satellites with associated containers found, skipping tcpdump."
            )
            return

        # /tmp is shared across all satellite containers — create once on the host
        os.makedirs(dump_dir, exist_ok=True)

        total_sats = len(sats)
        max_workers = min(MAX_WORKERS, total_sats)
        tic = time.perf_counter()

        def _start_tcpdump(sat):
            for interface in sat.interfaces:
                iname = interface.get_iname()
                pcap_file = f"{dump_dir}/{sat.name}_{iname}.pcap"
                cmd = f"tcpdump -i {iname} -s 96 -w {pcap_file}"
                sat.container.exec_run(cmd=cmd, detach=True)

        ok = 0
        error = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_start_tcpdump, sat): sat for sat in sats}
            for fut in as_completed(futures):
                sat = futures[fut]
                try:
                    fut.result()
                    ok += 1
                except (RuntimeError, OSError) as e:
                    error += 1
                    logging.error(f"Failed to start tcpdump on {sat.name}: {e}")

    def is_running_tcpdump(self) -> bool:
        """Check if tcpdump is already running on any satellite container."""
        for sat in self.satellites.values():
            if not hasattr(sat, "container") or sat.container is None:
                continue
            try:
                result = sat.container.exec_run("pgrep tcpdump", demux=False)
                if result.exit_code == 0:
                    return True
            except (RuntimeError, OSError) as exc:
                logging.warning(
                    "Failed to check tcpdump status on %s: %s", sat.name, exc
                )
        return False

    def enable_tcpdump_on_satellites(
        self,
        satellites: list = list(),
        dump_dir: str = "/tmp/dump",
    ) -> None:
        """
        Enable tcpdump on satellites if not already enabled.

        This method can be called after GoNetEm is started to launch tcpdump on satellite interfaces.
        It checks if tcpdump is already running and only launches if not present.

        Args:
            dump_dir: Directory where .pcap files are written (default: /tmp/dump)
        """
        if not self.get_gonetem_status():
            logging.warning("GoNetEm is not running. Cannot enable tcpdump.")
            return

        if not satellites:
            satellites = list(self.satellites.values())

        if self.is_running_tcpdump():
            logging.info(
                "tcpdump (net_logger) is already running on satellites. Skipping launch."
            )
            return

        logging.info("Enabling tcpdump on satellites...")
        self.launch_tcpdump_on_satellites(dump_dir=dump_dir, satellites=satellites)

    def disable_tcpdump_on_satellites(self, satellites: list = list()) -> None:
        """
        Disable tcpdump on satellites by killing the process in each satellite container.

        Args:
            satellites: List of satellite objects to disable tcpdump on. If empty, disables on all satellites.
        """
        if not self.get_gonetem_status():
            logging.warning("GoNetEm is not running. Cannot disable tcpdump.")
            return

        if not satellites:
            satellites = list(self.satellites.values())

        for sat in satellites:
            if not hasattr(sat, "container") or sat.container is None:
                continue
            try:
                # Kill tcpdump processes
                sat.container.exec_run("pkill tcpdump", demux=False)
                logging.info(f"Disabled tcpdump on {sat.name}")
            except (RuntimeError, OSError) as e:
                logging.error(f"Failed to disable tcpdump on {sat.name}: {e}")

    def get_topology_summary(self) -> Dict[str, Any]:
        """Return a summary of the topology including counts of satellites, ground stations, and links."""
        return {
            "satellites": len(self.satellites),
            "ground_stations": len(self.ground_stations),
            "links": len(self.links),
        }

    def execute_command_on(self, node: str = "", command: str = "") -> Dict[str, Any]:
        """Execute a command on a specified node and return the output.

        Args:
            node: Name of the node (satellite or ground station) to execute the command on.
            command: The command to execute as a string.
        Returns:
            A dictionary containing the output and error messages from the command execution.
        """
        if node.startswith("Sat"):
            node_id = int(node[3:]) if len(node) > 3 else 0
            match self.satellites.get(node_id):
                case Satellite() as target_node:
                    pass
                case None:
                    logging.warning(f"Node '{node}' not found in topology")
                    return {"output": "", "error": f"Node '{node}' not found"}
                case _ as unexpected:
                    raise TypeError(
                        f"Expected Satellite in satellites, got {type(unexpected)}"
                    )
        elif node.startswith("Gnd"):
            node_id = int(node[3:]) if len(node) > 3 else 0
            match self.ground_stations.get(node_id):
                case GroundStation() as target_node:
                    pass
                case None:
                    logging.warning(f"Node '{node}' not found in topology")
                    return {"output": "", "error": f"Node '{node}' not found"}
                case _ as unexpected:
                    raise TypeError(
                        f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                    )
        else:
            logging.warning(
                f"Invalid node name '{node}'. Must start with 'Sat' or 'Gnd'."
            )
            return {"output": "", "error": f"Invalid node name '{node}'"}

        if not hasattr(target_node, "container") or target_node.container is None:
            logging.warning(
                f"Node '{node}' does not have an associated container to execute commands on"
            )
            return {"output": "", "error": f"Node '{node}' has no container"}

        try:
            result = target_node.container.exec_run(cmd=command)
            output = result.output.decode("utf-8") if result.output else ""
            error = (
                ""
                if result.exit_code == 0
                else f"Command exited with code {result.exit_code}"
            )
            return {"output": output, "error": error}
        except (RuntimeError, OSError) as e:
            logging.error(f"Error executing command on node '{node}': {e}")
            return {"output": "", "error": str(e)}

    def get_node_by_name(self, name: str) -> Optional[Node]:
        """Get a node (satellite or ground station) by its name."""
        if name.startswith("Sat"):
            match self.satellites.get(int(name[3:])):
                case Satellite() as node:
                    return node
                case None:
                    return None
                case _ as unexpected:
                    raise TypeError(
                        f"Expected Satellite in satellites, got {type(unexpected)}"
                    )
        elif name.startswith("Gnd"):
            match self.ground_stations.get(int(name[3:])):
                case GroundStation() as node:
                    return node
                case None:
                    return None
                case _ as unexpected:
                    raise TypeError(
                        f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                    )
        return None

    def get_node_by_id(self, node_id: int) -> Optional[Node]:
        """Get a node (satellite or ground station) by its ID."""
        match self.satellites.get(node_id):
            case Satellite() as node:
                return node
            case None:
                match self.ground_stations.get(node_id):
                    case GroundStation() as node:
                        return node
                    case None:
                        return None
                    case _ as unexpected:
                        raise TypeError(
                            f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                        )
            case _ as unexpected:
                raise TypeError(
                    f"Expected Satellite in satellites, got {type(unexpected)}"
                )

    def get_node_ip_addresses(self, name: str) -> List[str]:
        """Get the list of IPv4 addresses assigned to a node."""
        node = self.get_node_by_name(name)
        if not node:
            logging.warning(f"Node '{name}' not found in topology")
            return []
        return [iface.ipv4 for iface in node.interfaces if iface.ipv4]

    def get_node_ifaces(self, name: str) -> List[str]:
        """Get the list of interfaces for a node."""
        node = self.get_node_by_name(name)
        if not node:
            logging.warning(f"Node '{name}' not found in topology")
            return []
        return [iface.name for iface in node.interfaces if iface.name and iface.ipv4]

    def get_coverage_percentage(
        self,
        elev_min_deg: float = 10.0,
        grid_res_deg: float = 1.0,
        max_latitude_deg: float = 90.0,
        R_earth_km: float = 6_371.0,
    ) -> float:
        """Return the current ground coverage percentage of the constellation.

        Delegates to coverage_percentage_fast using the satellites currently
        tracked in self.satellites.  Each satellite must expose a 'position'
        dict with keys 'latitude', 'longitude', and 'altitude' (km).

        Args:
            elev_min_deg: Minimum elevation angle in degrees for a ground point
                to be considered covered by a satellite.
            grid_res_deg: Resolution of the sampling grid in degrees.  Smaller
                values are more accurate but slower.
            max_latitude_deg: Latitude bound (symmetric) of the sampling grid.
            R_earth_km: Mean Earth radius in kilometres used for geometry.

        Returns:
            float: Coverage as a percentage in the range [0.0, 100.0].
        """
        sats = list(self.satellites.values())
        return coverage_percentage_fast(
            sats=sats,
            elev_min_deg=elev_min_deg,
            grid_res_deg=grid_res_deg,
            max_latitude_deg=max_latitude_deg,
            R_earth_km=R_earth_km,
        )
