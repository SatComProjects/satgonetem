"""
This version of TopologyManager will work simultaneously, in real-time, with satcomtopology
Satellites will include a satcom_object, which will correspond to their satcomtopology counterpart, and not be imported from graphs


"""

import contextlib
from mimetypes import init
import warnings
import base64
from datetime import datetime
import multiprocessing
import threading
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Callable, Type, Union
import numpy as np
from satgonetem.models.interface import Interface
from satgonetem.launchers.base_launcher import NetworkLauncher

from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor
import functools
import json
import logging
import os
import time
import networkx as nx


from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.routing.ospf_daemon import OSPFDaemon
from satgonetem.routing.static_daemon import StaticRoutingDaemon
from satgonetem.routing.srmpls_daemon import SRMPLSDaemon
from satgonetem.routing.isis_sr_bird_daemon import ISISBirdSRDaemon


from satgonetem.models.node import Node
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite
from satgonetem.utils.coverage import coverage_percentage_fast
from satgonetem.traffic import (
    Hping3Config,
    Hping3Flow,
    Hping3Results,
    Iperf3Config,
    Iperf3Flow,
    Iperf3Results,
    PingConfig,
    PingFlow,
    PingResults,
    run_hping3,
    run_ping,
    Hping3Status,
)
from satgonetem.traffic.ping_utils import PingStatus
from satgonetem.traffic.iperf3_utils import FlowStatus

from satgonetem.utils.flow_scheduler import FlowScheduler
from satgonetem.utils.utils import distance_3d_km


from sat_com_adapter.adapters import NetworkXAdapter
from sat_com_model.models import GroundStation as SatComGroundStation
from sat_com_model.models import InterSatelliteLinkDirection
from sat_com_model.models import Link as SatComLink
from sat_com_model.models import Satellite as SatComSatellite
from sat_com_model.models import TopologyObject

from satgonetem.dynamics.satcom_model import SatComModel
from sat_com_application.simulation_manager import SimulationManager
from sat_com_builder.models import SimulationProperty
from satgonetem.utils.project_builder import create_and_load_simulation

TI_LFA_ENABLED = True  # Global flag to enable/disable TI-LFA in SR-ISIS initialization
MAX_WORKERS = os.cpu_count() or 4


@dataclass
class NetworkConfig:
    """Network configuration parameters for TopologyManager.

    All fields are optional; defaults match the original built-in values.
    Pass an instance to TopologyManager.__init__ to override any subset.

    Attributes:
        project_name: Human-readable name for the constellation project.
            Defaults to None, which causes TopologyManager to fall back to
            the simulation_manager's own project_name (or "Constellation").
        update_time: Tick interval in seconds between topology updates.
        gnd_link_capacity: Capacity in kbps for ground-station links.
        isl_link_capacity: Capacity in kbps for inter-satellite links.
        protocol: Network-layer protocol string (e.g. "ipv4").
        routing: Default routing method (e.g. "static", "dynamic-ospf").
        satellite_image: Docker image tag used for satellite containers.
        network_launcher: Launcher backend identifier (e.g. "GONETEM").
        gonetem_server: Address of the GoNetem gRPC server.
    """

    project_name: Optional[str] = None
    update_time: int = 5
    gnd_link_capacity: int = 100000
    isl_link_capacity: int = 100000
    protocol: str = "ipv4"
    routing: str = "static"
    satellite_image: str = "jariassuarez/sgnt:satellite"
    network_launcher: str = "GONETEM"
    gonetem_server: str = "localhost:10110"


class TopologyManager(SatComModel):
    """Satellite network topology manager.

    Manages satellites, ground stations, links, and the active routing daemon.
    Custom routing methods can be registered via register_routing_daemon before
    constructing an instance.
    """

    _daemon_registry: ClassVar[Dict[str, Type[RoutingDaemon]]] = {}

    @classmethod
    def register_routing_daemon(
        cls, name: str, daemon_class: Type[RoutingDaemon]
    ) -> None:
        """Register a custom routing daemon under the given method name.

        Call this before constructing a TopologyManager. The name can then be
        passed as routing_method to init_routing(), or set as routing_method in
        config.yaml.

        Args:
            name: Unique method name string (e.g. 'my-custom-routing').
            daemon_class: A RoutingDaemon subclass to instantiate for this method.

        Raises:
            ValueError: If name conflicts with a built-in routing method.
            TypeError: If daemon_class is not a subclass of RoutingDaemon.
        """
        _builtin = {"static", "dynamic-ospf", "dynamic-isis", "sr-mpls"}
        if name in _builtin:
            raise ValueError(
                f"'{name}' is a built-in routing method and cannot be overridden"
            )
        if not (
            isinstance(daemon_class, type) and issubclass(daemon_class, RoutingDaemon)
        ):
            raise TypeError(
                f"daemon_class must be a subclass of RoutingDaemon, got {daemon_class!r}"
            )
        cls._daemon_registry[name] = daemon_class

    @classmethod
    def from_satcom(cls, simulation_property: SimulationProperty) -> "TopologyManager":
        """Create a TopologyManager from a SimulationProperty.

        Builds the SimulationManager from the project configuration in memory
        without requiring any YAML files on disk. Network configuration
        attributes (capacities, routing, protocol) are set to their defaults
        and can be overridden on the returned instance.

        Args:
            simulation_property: A configured SimulationProperty instance.

        Returns:
            An initialised TopologyManager backed by the project's SimulationManager.
        """
        sim_manager = create_and_load_simulation(
            simulation_property.model_dump(), simulation_property.simulation_name
        )
        instance = cls(simulation_manager=sim_manager)
        instance.simulation_property = simulation_property
        return instance

    def to_file(
        self, path: str, network_config: Optional[NetworkConfig] = None
    ) -> None:
        """Save the satcom configuration and network config to a JSON file.

        Serialises the SimulationProperty backing this instance, any referenced
        ground station data files, and the NetworkConfig so the topology can be
        fully reconstructed via from_file().

        Args:
            path: Filesystem path where the JSON file will be written.
            network_config: NetworkConfig to persist. If None, a NetworkConfig
                is built from the current instance attributes.

        Raises:
            OSError: If the file cannot be written or a ground station data
                file cannot be read.
        """
        if network_config is None:
            network_config = NetworkConfig(
                project_name=self.project_name,
                update_time=self.update_time,
                gnd_link_capacity=self.gnd_link_capacity,
                isl_link_capacity=self.isl_link_capacity,
                protocol=self.protocol,
                routing=self.routing,
                satellite_image=self.satellite_image,
                network_launcher=self.network_launcher,
                gonetem_server=self.gonetem_server,
            )

        if self.simulation_property is not None:
            sim_prop_data = self.simulation_property.model_dump()
        else:
            sim_prop_data = dict(
                self.simulation_manager.configuration.get("properties", {})
            )

        ground_files: Dict[str, Dict[str, str]] = {}
        for gop in sim_prop_data.get("ground_objects_properties", []):
            data_file = gop.get("data_file", "")
            if data_file and os.path.isfile(data_file):
                with open(data_file, "r", encoding="utf-8") as fh:
                    ground_files[data_file] = {
                        "content": fh.read(),
                        "basename": os.path.basename(data_file),
                    }

        payload = {
            "simulation_property": sim_prop_data,
            "network_config": network_config.__dict__,
            "ground_files": ground_files,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    @classmethod
    def from_file(cls, path: str) -> "TopologyManager":
        """Load a TopologyManager from a JSON file produced by to_file().

        Restores embedded ground station data files to '/tmp/<stem>_ground_files/',
        then reconstructs the SimulationProperty and NetworkConfig.

        Args:
            path: Path to the JSON file previously written by to_file().

        Returns:
            An initialised TopologyManager with the persisted configuration applied.

        Raises:
            FileNotFoundError: If path does not exist.
            ValueError: If the file content is not a valid topology config.
            OSError: If ground station data files cannot be written.
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        sim_prop_data: Dict[str, Any] = payload["simulation_property"]
        ground_files: Dict[str, Dict[str, str]] = payload.get("ground_files", {})

        if ground_files:
            stem = os.path.splitext(os.path.basename(path))[0]
            ground_dir = os.path.join("/tmp", f"{stem}_ground_files")
            os.makedirs(ground_dir, exist_ok=True)

            path_map: Dict[str, str] = {}
            for original_path, file_info in ground_files.items():
                restored_path = os.path.join(ground_dir, file_info["basename"])
                with open(restored_path, "w", encoding="utf-8") as fh:
                    fh.write(file_info["content"])
                path_map[original_path] = restored_path

            for gop in sim_prop_data.get("ground_objects_properties", []):
                orig = gop.get("data_file", "")
                if orig in path_map:
                    gop["data_file"] = path_map[orig]

        simulation_property = SimulationProperty.model_validate(sim_prop_data)
        network_config = NetworkConfig(**payload["network_config"])

        instance = cls.from_satcom(simulation_property)
        instance._apply_network_config(network_config)
        return instance

    def __init__(
        self,
        simulation_manager: SimulationManager,
        network_config: Optional[NetworkConfig] = None,
    ):
        start_time = time.time()
        logging.info(
            f"\033[92m=== Starting Topology Manager Initialization === [{start_time}]\033[0m"
        )
        self.simulation_manager = simulation_manager
        self.simulation_property: Optional[SimulationProperty] = None
        self._setup_update_actions()
        self._apply_network_config(network_config or NetworkConfig())
        self.nx_adapter = NetworkXAdapter(self.simulation_manager)

        self.satellites: dict[int, Satellite] = {}
        self.ground_stations: dict[int, GroundStation] = {}
        self.links: dict[frozenset[(str)], Link] = {}
        self.interfaces: list[Interface] = []  # List of all interfaces in the network

        # QoS thingies
        self.use_file_routes = False

        # Network Properties
        self.preference = "latency_prefer_ISLs"

        # Time related things
        self.current_time_step = 0
        self.start_time = self.simulation_manager.time_manager.start_date
        self.end_time = self.simulation_manager.time_manager.end_date
        self.update_time = 1  # in seconds

        # Direct launcher (replaces GoNetem gRPC)
        self.direct_launcher: Optional[NetworkLauncher] = None

        # Optional HIL manager. Set this before calling start_gonetem() to
        # replace specific ground stations with host-bridged hardware.
        self.hil_manager = None

        self.status = False
        self.gonetem_is_on = False

        # SatComTopology things
        self.initial_sync = False

        # Hashes to detect changes
        self.satellites_hash = None
        self.ground_stations_hash = None
        self.links_hash = None

        ## Connection strategies
        self.allowed_strategies = [
            "weighted-connection",
            "longest-connection-time-strategy",
            "best-angle-until-disconnection",
            "everything-visible",
            "everything-in-range",
            "best-range-until-disconnection",
            "best-multi-angle-until-disconnection",
            "one-link-per-layer",
        ]

        self.allowed_routing_methods = [
            "static",
            "dynamic-ospf",
            "dynamic-isis",
            "sr-mpls",
        ] + list(self._daemon_registry.keys())

        # Routing initialization flag - only update routes when routing is explicitly enabled
        self.routing_initiated = False

        # Simulation loop state
        self.running: bool = False
        self.update_factor: float = 1.0
        self._stop_evt: threading.Event = threading.Event()
        self._sim_thread: Optional[threading.Thread] = None

        # Active routing daemon - set during init_routing() based on routing method
        self.routing_daemon: Optional[RoutingDaemon] = None

        if self.simulation_manager is not None:

            self.init()

        end_time = time.time()
        logging.info(
            f"\033[92m=== Topology Manager Initialization Complete === [{end_time}] Duration: {end_time - start_time:.2f}s\033[0m"
        )

    def _apply_network_config(self, config: NetworkConfig) -> None:
        """Apply a NetworkConfig to this instance's network attributes.

        Args:
            config: NetworkConfig instance whose values are written to self.
                If config.project_name is None, falls back to the simulation
                manager's project_name attribute, or "Constellation".
        """
        self.project_name = config.project_name or getattr(
            self.simulation_manager, "project_name", "Constellation"
        )
        self.update_time = config.update_time
        self.gnd_link_capacity = config.gnd_link_capacity
        self.isl_link_capacity = config.isl_link_capacity
        self.protocol = config.protocol
        self.routing = config.routing
        self.satellite_image = config.satellite_image
        self.network_launcher = config.network_launcher
        self.gonetem_server = config.gonetem_server
        self.set_link_capacities(config.isl_link_capacity, config.gnd_link_capacity)

    def load_config(self) -> None:
        """No-op implementation satisfying the DynamicsModel ABC contract.

        Configuration is supplied at construction time via from_satcom().
        """

    def _setup_update_actions(self) -> None:
        """Register the standard per-tick update callbacks on the simulation manager.

        Clears any existing tick actions and registers the three core updates:
        configuration propagation, ground station link refresh, and user
        terminal link refresh.

        Raises:
            ValueError: If self.simulation_manager is None.
        """
        if not hasattr(self, "simulation_manager") or self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        self.simulation_manager.time_manager.on_tick_actions.clear()

        self.simulation_manager.time_manager.register_action(
            self.simulation_manager.configuration.update
        )
        self.simulation_manager.time_manager.register_action(
            self.simulation_manager.update_ground_station_links
        )
        self.simulation_manager.time_manager.register_action(
            self.simulation_manager.update_user_terminal_links
        )

    def init(self) -> None:
        """Initialize topology from the simulation manager."""
        self._sync_satellites()
        self._sync_ground_stations()
        self._sync_links()
        self._assign_interfaces_to_nodes()
        self._set_IPs_to_nodes()
        self._add_loopback_interfaces_to_list()
        _ = self.check_for_updates()  # Init hashes

    def _sync_satellites(self) -> None:
        """Sync all satellites from the simulation manager into self.satellites.

        Fetches satellite data from the simulation manager, constructs a
        Satellite instance for each entry in parallel, and stores each one
        keyed by its integer node ID.

        Raises:
            ValueError: If self.simulation_manager is None.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        satellites_data = self.simulation_manager.get_satellites()
        num_satellites = len(satellites_data)

        if num_satellites == 0:
            logging.info("No satellites found to sync.")
            return

        def process_satellite(sat_com_satellite):
            sat_id = f"Sat{getattr(sat_com_satellite, 'topology_uniq_id', 'Unknown')}"
            satellite = Satellite(sat_id)
            satellite.satcom_object = sat_com_satellite
            satellite.sync_position_from_satcom()
            return satellite

        max_workers = min(32, (os.cpu_count() or 4) * 4, num_satellites)
        synced_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_satellite, s): s for s in satellites_data
            }
            for future in as_completed(futures):
                try:
                    satellite = future.result()
                    self.satellites[satellite.id] = satellite
                    synced_count += 1
                except Exception as e:
                    error_count += 1
                    logging.error(f"Error syncing satellite: {e}")

        logging.info(
            f"Satellite sync complete: {synced_count}/{num_satellites} "
            f"(Ok: {synced_count} | Fail: {error_count})"
        )

    def _sync_ground_stations(self) -> None:
        """Sync all ground stations from the simulation manager into self.ground_stations.

        Fetches ground station data from the simulation manager, constructs a
        GroundStation instance for each entry in parallel, and stores each one
        keyed by its integer node ID.

        Raises:
            ValueError: If self.simulation_manager is None.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        gs_data = self.simulation_manager.get_ground_stations()
        num_gs = len(gs_data)

        if num_gs == 0:
            logging.info("No ground stations found to sync.")
            return

        def process_ground_station(sat_com_gs):
            gs_id = f"Gnd{getattr(sat_com_gs, 'topology_uniq_id', 'Unknown')}"
            gs = GroundStation(gs_id)
            gs.satcom_object = sat_com_gs
            gs.city = getattr(sat_com_gs, "label", None)
            gs.sync_position_from_satcom()
            gs.type = "GroundStation"
            return gs

        max_workers = min(32, (os.cpu_count() or 4) * 4, num_gs)
        synced_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_ground_station, gs): gs for gs in gs_data}
            for future in as_completed(futures):
                try:
                    gs = future.result()
                    self.ground_stations[gs.id] = gs
                    synced_count += 1
                except Exception as e:
                    error_count += 1
                    logging.error(f"Error syncing ground station: {e}")

        logging.info(
            f"Ground station sync complete: {synced_count}/{num_gs} "
            f"(Ok: {synced_count} | Fail: {error_count})"
        )

    def _sync_links(self, add_anyway: bool = False) -> None:
        """Sync all links from the simulation manager into self.links.

        Workers compute link results in parallel without mutating shared state.
        All writes to self.links are applied in the main thread after the pool
        completes, avoiding data races.

        Args:
            add_anyway: If True, mark newly discovered links with to_add=True
                even outside the initial sync phase.

        Raises:
            ValueError: If self.simulation_manager is None.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        links_data = self.simulation_manager.get_all_links()
        num_links = len(links_data)

        if num_links == 0:
            logging.info("No links found to sync.")
            return

        existing_keys = frozenset(self.links.keys())

        def compute_link_result(sat_com_link):
            src = getattr(sat_com_link, "source", None)
            dst = getattr(sat_com_link, "destination", None)
            if not src or not dst:
                return None
            key = self._build_link_key(src, dst)
            if key in existing_keys:
                is_active = getattr(
                    sat_com_link,
                    "is_active",
                    not getattr(sat_com_link, "disabled", False),
                )
                return ("update", key, is_active)
            new_link = self._create_link_from_satcom_link(sat_com_link)
            return ("new", key, new_link)

        max_workers = min(32, (os.cpu_count() or 4) * 4, num_links)
        results = []
        done_count = 0
        error_count = 0

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(compute_link_result, link): link for link in links_data
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result is not None:
                        results.append(result)
                    done_count += 1
                except Exception as e:
                    error_count += 1
                    logging.error(f"Error syncing link: {e}")

        new_count = 0
        for action, key, payload in results:
            if action == "new":
                self.links[key] = payload
                if self.initial_sync or add_anyway:
                    payload.to_add = True
                new_count += 1
            else:
                self.links[key].is_active = payload
                self.links[key].sync_distance_from_satcom_and_delay()

        self.initial_sync = True
        logging.info(
            f"Link sync complete: {done_count}/{num_links} "
            f"(Ok: {done_count} | Fail: {error_count} | Added: {new_count})"
        )

    def _sync_links_to_delete(self) -> None:
        """Mark links absent from the simulation manager for removal.

        Compares the current set of link keys in self.links against those
        reported by the simulation manager. Any key no longer present is
        flagged with to_remove=True for downstream cleanup.

        Raises:
            ValueError: If self.simulation_manager is None.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        links = self.simulation_manager.get_all_links()

        current_keys = set()
        for sat_com_link in links:
            key = self._build_link_key(sat_com_link.source, sat_com_link.destination)
            current_keys.add(key)

        keys_to_delete = set(self.links.keys()) - current_keys
        for key in keys_to_delete:
            self.links[key].to_remove = True

    def _sync_node_positions(self) -> None:
        """Update the position of every satellite from its satcom_object.

        Iterates over all satellites in self.satellites and delegates position
        synchronisation to each node's sync_position_from_satcom method.
        """
        for satellite in self.satellites.values():
            satellite.sync_position_from_satcom()

    def _create_link_from_satcom_link(self, sat_com_link: SatComLink) -> Link:
        """Construct a Link from a sat_com_model SatComLink.

        Resolves the source and destination nodes from self.satellites and
        self.ground_stations, computes the 3-D distance in metres, and maps
        the ISL direction enum to a human-readable string.

        Args:
            sat_com_link: The sat_com_model link object to convert.

        Returns:
            A fully initialised Link instance with satcom_object set.
        """
        src_uid = sat_com_link.source.topology_uniq_id
        match self.satellites.get(src_uid):
            case Satellite() as link_source:
                pass
            case None:
                match self.ground_stations.get(src_uid):
                    case GroundStation() as link_source:
                        pass
                    case None:
                        raise ValueError(
                            f"Link source not found for "
                            f"{getattr(sat_com_link.source, 'topology_uniq_id', 'Unknown')}"
                        )
                    case _ as unexpected:
                        raise TypeError(
                            f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                        )
            case _ as unexpected:
                raise TypeError(
                    f"Expected Satellite in satellites, got {type(unexpected)}"
                )

        dst_uid = sat_com_link.destination.topology_uniq_id
        match self.satellites.get(dst_uid):
            case Satellite() as link_destination:
                pass
            case None:
                match self.ground_stations.get(dst_uid):
                    case GroundStation() as link_destination:
                        pass
                    case None:
                        raise ValueError(
                            f"Link destination not found for "
                            f"{getattr(sat_com_link.destination, 'topology_uniq_id', 'Unknown')}"
                        )
                    case _ as unexpected:
                        raise TypeError(
                            f"Expected GroundStation in ground_stations, got {type(unexpected)}"
                        )
            case _ as unexpected:
                raise TypeError(
                    f"Expected Satellite in satellites, got {type(unexpected)}"
                )

        link_capacity = getattr(sat_com_link, "capacity", 1000) * 1024
        link_type = getattr(sat_com_link, "type", "Link")
        link_is_active = not getattr(sat_com_link, "disabled", True)

        link_distance = (
            distance_3d_km(
                lat1=link_source.position["latitude"],
                lon1=link_source.position["longitude"],
                alt1=link_source.position["altitude"],
                lat2=link_destination.position["latitude"],
                lon2=link_destination.position["longitude"],
                alt2=link_destination.position["altitude"],
            )
            * 1000
        )

        link_direction_ID = getattr(
            sat_com_link,
            "inter_satellite_direction",
            InterSatelliteLinkDirection.UNDEFINED,
        )
        link_direction_map = {
            InterSatelliteLinkDirection.ORBITAL: "Orbital",
            InterSatelliteLinkDirection.ADJACENT: "Adjacent",
            InterSatelliteLinkDirection.UNDEFINED: "Undefined",
        }

        link = Link(
            source=link_source,
            target=link_destination,
            distance=link_distance,
            type=link_type,
            direction=link_direction_map.get(link_direction_ID, "Undefined"),
            is_active=link_is_active,
            capacities=[self.gnd_link_capacity, self.isl_link_capacity],
        )
        link.satcom_object = sat_com_link
        return link

    def _build_link_key(
        self, source: "Node | TopologyObject", target: "Node | TopologyObject"
    ) -> frozenset:
        """Build a unique frozenset key identifying a link between two nodes.

        Accepts either SatGoNeTem nodes (Satellite/GroundStation) or
        sat_com_model topology objects (SatComSatellite/SatComGroundStation),
        so the same method can be used during both initial sync and runtime
        update passes.

        Args:
            source: The link source, either a local node or a satcom object.
            target: The link target, either a local node or a satcom object.

        Returns:
            A frozenset of two name strings that uniquely identifies the link
            regardless of direction.

        Raises:
            ValueError: If source/target are not a recognised node type pair.
        """
        if isinstance(source, SatComSatellite) and isinstance(target, SatComSatellite):
            return frozenset(
                (
                    "Sat" + str(getattr(source, "topology_uniq_id", None)),
                    "Sat" + str(getattr(target, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, SatComSatellite) and isinstance(
            target, SatComGroundStation
        ):
            return frozenset(
                (
                    "Sat" + str(getattr(source, "topology_uniq_id", None)),
                    "Gnd" + str(getattr(target, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, SatComGroundStation) and isinstance(
            target, SatComSatellite
        ):
            return frozenset(
                (
                    "Gnd" + str(getattr(source, "topology_uniq_id", None)),
                    "Sat" + str(getattr(target, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, SatComGroundStation) and isinstance(
            target, SatComGroundStation
        ):
            return frozenset(
                (
                    "Gnd" + str(getattr(source, "topology_uniq_id", None)),
                    "Gnd" + str(getattr(target, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, Satellite) and isinstance(target, Satellite):
            return frozenset(
                (
                    "Sat"
                    + str(getattr(source.satcom_object, "topology_uniq_id", None)),
                    "Sat"
                    + str(getattr(target.satcom_object, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, Satellite) and isinstance(target, GroundStation):
            return frozenset(
                (
                    "Sat"
                    + str(getattr(source.satcom_object, "topology_uniq_id", None)),
                    "Gnd"
                    + str(getattr(target.satcom_object, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, GroundStation) and isinstance(target, Satellite):
            return frozenset(
                (
                    "Gnd"
                    + str(getattr(source.satcom_object, "topology_uniq_id", None)),
                    "Sat"
                    + str(getattr(target.satcom_object, "topology_uniq_id", None)),
                )
            )
        elif isinstance(source, GroundStation) and isinstance(target, GroundStation):
            return frozenset(
                (
                    "Gnd"
                    + str(getattr(source.satcom_object, "topology_uniq_id", None)),
                    "Gnd"
                    + str(getattr(target.satcom_object, "topology_uniq_id", None)),
                )
            )
        else:
            raise ValueError(
                "Source and target must be either Satellite or GroundStation"
            )

    def get_gonetem_status(self) -> bool:
        """Return whether GoNetEm has been started."""
        return self.gonetem_is_on

    def set_gonetem_status(self, value: bool) -> None:
        """Set whether GoNetEm is on.

        Args:
            value: True when GoNetEm has been started, False when stopped.
        """
        self.gonetem_is_on = value

    def get_status(self) -> bool:
        """Return whether TopologyManager is active (SimulationManager is live)."""
        return self.status

    def set_status(self, status: bool) -> None:
        """Set the topology active status.

        Args:
            status: True when TopologyManager is created and SimulationManager is active.
        """
        self.status = status

    def get_running(self) -> bool:
        """Return whether the simulation loop has been started via start()."""
        return self.running

    def set_running(self, value: bool) -> None:
        """Set the simulation loop running state.

        Args:
            value: True when start() has been called, False when stopped.
        """
        self.running = value

    def get_routing_initiated(self) -> bool:
        """Return whether routing has been initialised via init_routing()."""
        return self.routing_initiated

    def set_routing_initiated(self, value: bool) -> None:
        """Set the routing-initiated flag.

        Args:
            value: True after init_routing() succeeds, False after routing is torn down.
        """
        self.routing_initiated = value

    def get_path_between_nodes(self, source: Node, target: Node) -> List[Node]:
        """
        Get the path between two nodes using the specified preference.
        """
        graph = self.get_current_graph()

        if type(source) not in [GroundStation, Satellite]:
            raise TypeError("Source must be either a GroundStation or a Satellite")
        try:
            path_ids = nx.shortest_path(
                graph, source=source.id, target=target.id, weight="weight"
            )
            path_nodes = []

            for node_id in path_ids:
                # Check if it's a satellite
                if node_id in self.satellites:
                    path_nodes.append(self.satellites[node_id])
                # Check if it's a ground station
                else:
                    # Find the ground station by ID
                    for gs in self.ground_stations.values():
                        if gs.id == node_id:
                            path_nodes.append(gs)
                            break

            return path_nodes
        except nx.NetworkXNoPath:
            logging.warning(f"No path found between {source.id} and {target.id}")
            return []

    def rebuild_routing_for_current_timestep(self):
        """
        Rebuild routing tables for the current time step.

        Delegates to the active routing daemon. For static routing, the daemon
        recomputes Dijkstra paths and applies incremental route changes to all
        containers. Dynamic (FRR) and SR-MPLS daemons handle their own updates.
        """
        if self.routing_daemon is not None and self.get_status():
            self.routing_daemon.update([], max_workers=MAX_WORKERS)

    def move_to_time(self, new_time: datetime) -> None:
        """
        Jump the simulation to an absolute time and rebuild routing.

        Delegates the time-manager advance to SatComModel, then triggers the
        emulator-level routing rebuild that requires TopologyManager state.

        Args:
            new_time: Target simulation datetime.

        Returns:
            None

        Raises:
            ValueError: If simulation_manager has not been initialised.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        self.simulation_manager.time_manager.set_time(new_time)
        self.simulation_manager.time_manager.execute_actions()
        self.current_time_step = int(
            (new_time - self.start_time).total_seconds() / self.update_time
        )
        self.rebuild_routing_for_current_timestep()

    def update_simulation(self) -> None:
        """
        Advance the simulation by one timestep and apply all topology changes.

        Steps:
        1. Tick the sat_com time manager.
        2. Sync satellite positions and link states from sat_com_model.
        3. Mark stale links for deletion.
        4. Execute bulk link operations (add/update/delete) via the launcher.
        5. Sync emulator interface state to reflect updated link topology.

        Returns:
            None

        Raises:
            ValueError: If simulation_manager has not been initialised.
        """
        start_t = time.time()
        logging.info(
            "Simulation update started for time step %d, %.3fs",
            self.current_time_step,
            start_t,
        )

        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")

        self.current_time_step += 1

        self._update_simulation_manager_time()
        self._sync_node_positions()
        self._sync_links()
        self._sync_links_to_delete()

        pending_delete_count = sum(
            1 for link in self.links.values() if getattr(link, "to_remove", False)
        )

        link_stats = self.bulk_link_operations(to_del=False)
        self._perform_local_link_operations(to_del=False)

        if (
            link_stats["added_count"]
            or link_stats["updated_count"]
            or pending_delete_count
        ):
            self._update_routing_after_link_changes(link_stats["links_to_add"])

        delete_stats = self.bulk_link_operations(to_add=False, to_update=False)
        self._perform_local_link_operations(to_add=False, to_update=False)
        link_stats["deleted_count"] = delete_stats["deleted_count"]
        link_stats["delete_time_total"] = delete_stats["delete_time_total"]
        link_stats["delete_time_per_link"] = delete_stats["delete_time_per_link"]

        if (
            link_stats["added_count"] > 0
            or link_stats["updated_count"] > 0
            or link_stats["deleted_count"] > 0
        ):
            parts = [
                f"Link operations ({self.routing} routing) -",
                f"Added: {link_stats['added_count']}",
            ]
            if link_stats["added_count"] > 0:
                parts.append(
                    f"(total: {link_stats['add_time_total'] * 1000:.2f}ms, "
                    f"per-link: {link_stats['add_time_per_link'] * 1000:.4f}ms)"
                )
            parts.append(f"Updated: {link_stats['updated_count']}")
            if link_stats["updated_count"] > 0:
                parts.append(
                    f"(total: {link_stats['update_time_total'] * 1000:.2f}ms, "
                    f"per-link: {link_stats['update_time_per_link'] * 1000:.4f}ms)"
                )
            parts.append(f"Deleted: {link_stats['deleted_count']}")
            if link_stats["deleted_count"] > 0:
                parts.append(
                    f"(total: {link_stats['delete_time_total'] * 1000:.2f}ms, "
                    f"per-link: {link_stats['delete_time_per_link'] * 1000:.4f}ms)"
                )
            logging.info(" ".join(parts))

        self._sync_interfaces_and_links()

        elapsed_ms = (time.time() - start_t) * 1000
        logging.info(
            "Simulation update completed for time step %d, total time: %.2fms",
            self.current_time_step,
            elapsed_ms,
        )

    def _sync_interfaces_and_links(self) -> None:
        """
        Propagate current link active/inactive state to peer Interface objects.

        Called after every topology update to keep Interface.is_active consistent
        with Link.is_active.

        Returns:
            None
        """
        for link in self.links.values():
            link.update_interfaces_state()

    def _create_test_link(self, node1, node2) -> Optional[Link]:
        """
        Create a test link between two nodes for performance testing.

        Args:
            node1: Source node (Satellite or GroundStation)
            node2: Target node (Satellite or GroundStation)

        Returns:
            A Link object marked for addition, or None if link cannot be created
        """
        try:
            # Avoid self-links
            if node1 == node2:
                return None

            # Calculate distance between nodes
            try:
                distance = (
                    distance_3d_km(
                        lat1=float(node1.position.get("latitude", 0)),
                        lon1=float(node1.position.get("longitude", 0)),
                        alt1=float(node1.position.get("altitude", 0)),
                        lat2=float(node2.position.get("latitude", 0)),
                        lon2=float(node2.position.get("longitude", 0)),
                        alt2=float(node2.position.get("altitude", 0)),
                    )
                    * 1000
                )  # Convert to meters

                # Ensure distance is a valid number
                if (
                    not isinstance(distance, (int, float))
                    or distance <= 0
                    or distance == float("inf")
                ):
                    distance = 100000  # Default distance
                else:
                    distance = float(distance)  # Ensure it's a float
            except Exception as e:
                logging.warning(
                    f"Could not calculate distance between {node1.name} and {node2.name}: {e}"
                )
                distance = 100000.0  # Default distance

            # Determine link type based on node types
            link_type = "SatelliteLink"
            if hasattr(node1, "type") and hasattr(node2, "type"):
                node1_type = str(node1.type)
                node2_type = str(node2.type)

                if "Ground" in node1_type or "Ground" in node2_type:
                    link_type = "GroundStationLink"

            # Create the test link with integer capacity
            test_link = Link(
                source=node1,
                target=node2,
                distance=distance,
                type=link_type,
                direction="",
                capacities=[int(10000)],
                is_active=True,
            )

            # Ensure delay is an integer
            test_link.delay = int(test_link.delay) if test_link.delay else 1

            # Mark the link for addition
            test_link.to_add = True

            # Add to internal links dictionary
            link_key = frozenset([node1.name, node2.name])

            # Check if link already exists
            if link_key not in self.links:
                self.links[link_key] = test_link
                logging.info(
                    f"Created test link {node1.name} -- {node2.name} (distance: {int(distance/1000)} km, delay: {test_link.delay}ms)"
                )
                return test_link
            else:
                logging.debug(f"Test link {node1.name} -- {node2.name} already exists")
                return None

        except Exception as e:
            logging.error(
                f"Error creating test link between {node1.name} and {node2.name}: {e}"
            )
            return None

    def bulk_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
    ) -> dict:
        """
        Perform all link operations (add, update, delete) using a single gRPC channel
        with parallelized operations to optimize performance.

        Args:
            to_add: Whether to process link additions
            to_update: Whether to process link updates
            to_del: Whether to process link deletions
            max_workers: Maximum number of parallel workers

        Returns:
            dict: Contains counts and timing information for link operations:
                - 'added_count': Number of links added
                - 'updated_count': Number of links updated
                - 'deleted_count': Number of links deleted
                - 'add_time_total': Total time to add all links
                - 'add_time_per_link': Time per link for add operations
                - 'update_time_total': Total time to update all links
                - 'update_time_per_link': Time per link for update operations
                - 'delete_time_total': Total time to delete all links
                - 'delete_time_per_link': Time per link for delete operations
        """
        # Collect all link operations that need to be performed
        links_to_add = [
            link for link in self.links.values() if getattr(link, "to_add", False)
        ]
        links_to_update = [
            link for link in self.links.values() if getattr(link, "to_update", False)
        ]
        links_to_remove = [
            link for link in self.links.values() if getattr(link, "to_remove", False)
        ]

        # Initialize timing information
        operation_stats = {
            "added_count": len(links_to_add),
            "updated_count": len(links_to_update),
            "deleted_count": len(links_to_remove),
            "links_to_add": links_to_add,
            "add_time_total": 0.0,
            "add_time_per_link": 0.0,
            "update_time_total": 0.0,
            "update_time_per_link": 0.0,
            "delete_time_total": 0.0,
            "delete_time_per_link": 0.0,
        }

        # If no gRPC operations needed or status is False, return early
        if not self.get_status() or not (
            links_to_add or links_to_update or links_to_remove
        ):
            return operation_stats

        # Direct veth/tc operations (no gRPC)
        t_delete_start = time.perf_counter()
        t_update_start = time.perf_counter()
        t_add_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=1) as executor:
            futures = []

            if to_del:
                t_delete_start = time.perf_counter()
                for link in links_to_remove:
                    futures.append(executor.submit(self._execute_link_delete, link))

            if to_update:
                t_update_start = time.perf_counter()
                for link in links_to_update:
                    futures.append(executor.submit(self._execute_link_update, link))

            if to_add:
                t_add_start = time.perf_counter()
                for link in links_to_add:
                    futures.append(executor.submit(self._execute_link_add, link))

            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"Link operation failed: {e}")

        t_end = time.perf_counter()
        operation_stats["delete_time_total"] = t_end - t_delete_start
        operation_stats["update_time_total"] = t_end - t_update_start
        operation_stats["add_time_total"] = t_end - t_add_start

        # Calculate per-link timing
        if operation_stats["added_count"] > 0:
            operation_stats["add_time_per_link"] = (
                operation_stats["add_time_total"] / operation_stats["added_count"]
            )
        if operation_stats["updated_count"] > 0:
            operation_stats["update_time_per_link"] = (
                operation_stats["update_time_total"] / operation_stats["updated_count"]
            )
        if operation_stats["deleted_count"] > 0:
            operation_stats["delete_time_per_link"] = (
                operation_stats["delete_time_total"] / operation_stats["deleted_count"]
            )

        return operation_stats

    def _update_routing_after_link_changes(
        self, new_links: List[Link], max_workers: int = 4
    ) -> None:
        """
        Update routing after link topology changes based on the configured routing method.

        Args:
            new_links: List of newly added links (used for FRR interface initialization)
            max_workers: Maximum number of worker threads for parallel processing
        """
        # Only update routing if routing has been explicitly initialized
        if not self.get_routing_initiated():
            logging.debug("Skipping routing update - routing not initialized")
            return
        if not self.routing_daemon:
            logging.debug("Skipping routing update - routing daemon not initialized")
            return

        if self.routing == "dynamic-ospf":
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info("Updated FRR OSPF interface status for dynamic routing")
        elif self.routing == "dynamic-isis":
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info("Updated Bird IS-IS SR routing after link changes")
        elif self.routing == "sr-mpls":
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info("Rebuilt SR-MPLS routing after link changes")

        elif self.routing == "static":
            # Static routing - recompute Dijkstra paths and update IP routes
            if self.routing_daemon is None:
                logging.warning("Static routing update skipped: daemon not initialized")
                return
            self.routing_daemon.update(new_links, max_workers=max_workers)
            logging.info("Rebuilt static IP routing after link changes")

    def _execute_link_delete(self, link: Link) -> None:
        """Delete a link's veth pair directly via nsenter+ip."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.teardown_link(link)
            link.to_delete = False
            return
        if self.direct_launcher is None:
            logging.warning("direct_launcher not set, skipping link delete")
            return
        try:
            self.direct_launcher.delete_link(link)
            link.to_delete = False
            logging.info(f"Deleted link {link.source.name} -- {link.target.name}")
        except Exception as err:
            logging.error(
                f"Unable to delete link {link.source.name}--{link.target.name}: {err}"
            )

    def _execute_link_update(self, link) -> None:
        """Update a link's netem delay and TBF rate directly via nsenter+tc."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.update_link(link)
            return
        if self.direct_launcher is None:
            logging.warning("direct_launcher not set, skipping link update")
            return
        try:
            self.direct_launcher.update_link(link)
            delay = max(int(link.delay), 1)
            logging.info(
                f"Updated link {link.source.name} -- {link.target.name} with delay {delay} ms"
            )
        except Exception as err:
            logging.error(
                f"Unable to update link {link.source.name}--{link.target.name}: {err}"
            )

    def set_link_capacities(self, isl_kbps: int, gnd_kbps: int) -> None:
        """Update ISL and GSL capacities on all links.

        Args:
            isl_kbps: New capacity in kbps for inter-satellite links.
            gnd_kbps: New capacity in kbps for ground station links.
        """
        for link in getattr(self, "links", {}).values():
            link.capacity = gnd_kbps if link.type == "GroundStationLink" else isl_kbps
        logging.info(
            "Updated link capacities: ISL=%d kbps, GSL=%d kbps (%d links)",
            isl_kbps,
            gnd_kbps,
            len(getattr(self, "links", {})),
        )

    def _execute_link_add(self, link) -> None:
        """Add a link by creating a veth pair and applying qdiscs directly."""
        if self.hil_manager is not None and self.hil_manager.is_hil_link(link):
            self.hil_manager.setup_link(link)
            return
        if self.direct_launcher is None:
            logging.warning("direct_launcher not set, skipping link add")
            return
        try:
            self.direct_launcher.add_link(link)
            delay = max(int(link.delay), 1)
            logging.info(
                f"Added link {link.source.name} -- {link.target.name} with delay {delay} ms"
            )
        except Exception as err:
            logging.error(
                f"Unable to add link {link.source.name}--{link.target.name}: {err}"
            )

    def _process_links_sequentially(
        self, links_to_remove, links_to_update, links_to_add
    ) -> None:
        """Fallback method for sequential link processing."""
        for link in links_to_remove:
            self._execute_link_delete(link)
        for link in links_to_update:
            self._execute_link_update(link)
        for link in links_to_add:
            self._execute_link_add(link)

    def _perform_local_link_operations(
        self,
        to_add: bool = True,
        to_update: bool = True,
        to_del: bool = True,
        max_workers: int = MAX_WORKERS,
    ) -> None:
        """
        Perform local link operations (interface management, state updates)
        without gRPC calls, using parallel processing where safe.

        Args:
            to_add: Whether to process link additions.
            to_update: Whether to process link updates.
            to_del: Whether to process link deletions.
            max_workers: Maximum number of parallel workers.
        """
        links_to_remove = (
            [link for link in self.links.values() if getattr(link, "to_remove", False)]
            if to_del
            else []
        )
        links_to_update = (
            [link for link in self.links.values() if getattr(link, "to_update", False)]
            if to_update
            else []
        )
        links_to_add = (
            [link for link in self.links.values() if getattr(link, "to_add", False)]
            if to_add
            else []
        )

        total_operations = (
            len(links_to_remove) + len(links_to_update) + len(links_to_add)
        )

        if total_operations == 0:
            return

        self._perform_local_link_operations_parallel(
            links_to_remove, links_to_update, links_to_add, max_workers
        )

    def _perform_local_link_operations_parallel(
        self,
        links_to_remove: list[Link],
        links_to_update: list[Link],
        links_to_add: list[Link],
        max_workers: int,
    ) -> None:
        """
        Perform local link operations in parallel where thread-safe.
        """
        # Thread-safe operations that can be parallelized
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []

            # Parallel operations for individual link processing
            # Remove interface operations (can be done in parallel per link)
            for link in links_to_remove:
                future = executor.submit(self._remove_link_interfaces, link)
                futures.append(future)

            # Update operations (thread-safe flag updates)
            for link in links_to_update:
                future = executor.submit(self._update_link_flags, link)
                futures.append(future)

            # Add interface operations (can be done in parallel per link)
            for link in links_to_add:
                future = executor.submit(self._add_link_interfaces, link)
                futures.append(future)

            # Wait for all parallel operations to complete
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logging.error(f"Local link operation failed: {e}")

        # Sequential operations that require thread safety (modifying shared collections)
        self._cleanup_removed_links_sequential(links_to_remove)

    def _remove_link_interfaces(self, link: Link) -> None:
        """Remove interfaces from nodes for a single link."""
        try:
            # Remove interfaces from nodes
            source: Node = link.source
            target: Node = link.target
            source.remove_interface_connected_to_node(target)
            target.remove_interface_connected_to_node(source)
        except Exception as e:
            logging.error(
                f"Failed to remove interfaces for link {link.source.name}--{link.target.name}: {e}"
            )

    def _cleanup_removed_links_sequential(self, links_to_remove: list[Link]) -> None:
        """
        Clean up removed links from shared data structures.
        Must be done sequentially to ensure thread safety.
        """
        for link in links_to_remove:
            try:
                # Remove interfaces from global list
                self.interfaces = [
                    intf for intf in self.interfaces if intf not in link.peer_interfaces
                ]
                # Remove link from dictionary
                key = self._build_link_key(link.source, link.target)
                del self.links[key]
            except Exception as e:
                logging.error(
                    f"Failed to cleanup link {link.source.name}--{link.target.name}: {e}"
                )

    def _update_link_flags(self, link: Link) -> None:
        """Update flags for a single link."""
        try:
            link.to_update = False
        except Exception as e:
            logging.error(
                f"Failed to update flags for link {link.source.name}--{link.target.name}: {e}"
            )

    def _add_link_interfaces(self, link: Link) -> None:
        """Add interfaces for a single link."""
        try:
            self._build_interfaces_from_link(link, set_ip=True, sync_to_node=True)
            link.to_add = False
        except Exception as e:
            logging.error(
                f"Failed to add interfaces for link {link.source.name}--{link.target.name}: {e}"
            )

    def get_all_links_usage(self):
        """Return usage for all links (mapped), not only congested.

        Returns:
            List of dictionaries with link usage data:
            - src: Source node name
            - dst: Destination node name
            - value: Bandwidth usage in bits per second (from interface monitoring)
            - type: Link type ('InterSatelliteLink' or 'GroundStationLink')
        """
        if not self.get_gonetem_status():
            return []
        return self.monitor_links()

    def _add_loopback_interfaces_to_list(self):
        """
        Method to add loopback interfaces to the list of interfaces
        """
        for node in list(self.satellites.values()) + list(
            self.ground_stations.values()
        ):
            node.loopback.name = f"lo_{node.name}"
            node.loopback.set_ipv4_address()
            self.interfaces.append(node.loopback)

    def _assign_interfaces_to_nodes(self) -> None:
        """
        Method to assign interfaces to nodes
        """
        links = self.links.values()

        for link in links:
            self._build_interfaces_from_link(link)

    def _build_interfaces_from_link(
        self, link: Link, set_ip: bool = True, sync_to_node: bool = True
    ) -> None:
        # Get source and destination
        source = link.source
        target = link.target

        # print(source, target, type)

        int1 = source.create_interface(f"{source.name}.{str(target.id)}")
        int2 = target.create_interface(f"{target.name}.{str(source.id)}")

        # Set delays
        int1.delay = link.delay
        int2.delay = link.delay

        # Set type
        if "Gnd" in [source.name[:3], target.name[:3]]:
            int1.type = "GroundStationLink"
            int2.type = "GroundStationLink"

        elif "Sat" in [source.name[:3], target.name[:3]]:
            int1.type = "InterSatelliteLink"
            int2.type = "InterSatelliteLink"
        else:
            print("Unknown link type")

        # Set peer
        int1.peer = int2
        int2.peer = int1

        # Set state
        int1.is_active = link.is_active
        int2.is_active = link.is_active

        link.peer_interfaces.extend([int1, int2])  # Add interfaces to the link

        self.interfaces.extend([int1, int2])  # Add interfaces to the global list

        if not self.get_status():
            return

        if set_ip:
            int1.set_ipv4_address()
            int2.set_ipv4_address()

        if sync_to_node and set_ip:
            source.set_ipv4s_to_containers(interface=int1, set_lo=False)
            target.set_ipv4s_to_containers(interface=int2, set_lo=False)

    def _set_IPs_to_nodes(self) -> None:
        """
        Method to set IPs to nodes
        """
        for interface in self.interfaces:
            interface.set_ipv4_address()

    def set_ipv4s_for_all_nodes(
        self, set_lo: bool = True, max_workers: int = MAX_WORKERS, sats: bool = True
    ) -> None:
        """
        Assign IPv4s to every satellite and ground station as fast as possible with a spinner.
        """
        nodes = []
        if sats:
            nodes.extend(self.get_satellites())
        nodes.extend(self.get_ground_stations())

        if not nodes:
            logging.info(
                "No nodes to configure (satellites + ground stations list is empty)."
            )
            return

        total_nodes = len(nodes)
        tic = time.perf_counter()

        if max_workers is None:
            max_workers = min(MAX_WORKERS, total_nodes)

        submitted = 0
        errors = 0

        # Start the spinner with a distinct color for network config

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    node.set_ipv4s_to_containers, interface=None, set_lo=set_lo
                ): node
                for node in nodes
            }

            for fut in as_completed(futures):
                node = futures[fut]
                submitted += 1
                try:
                    fut.result()
                except Exception as e:
                    errors += 1
                    logging.exception(
                        f"Failed to dispatch IPv4 assignment on {node.name}: {e}"
                    )

                logging.debug(
                    f"IP Assignment: {submitted}/{total_nodes} (Errors: {errors})"
                )

        toc = time.perf_counter()

        logging.info(
            f"IPv4 assignment dispatched for {submitted - errors}/{submitted} nodes "
            f"in {toc - tic:.3f}s (max_workers={max_workers})."
        )

    def _get_interface_to_peer(self, node: Node, peer_id: int) -> Optional[Interface]:
        """
        Get the interface on a node that connects to a specific peer.

        Args:
            node: Source node
            peer_id: ID of the peer node

        Returns:
            Interface connecting to peer, or None if not found
        """
        for iface in node.interfaces:
            # Interface name format: NodeName.PeerID
            try:
                iface_peer_id = int(iface.name.split(".")[1])
                if iface_peer_id == peer_id:
                    return iface
            except (ValueError, IndexError):
                continue
        return None

    def init_mpls_on_all_nodes(self, max_workers: int = MAX_WORKERS) -> None:
        """Enable MPLS forwarding on all satellites and ground stations.

        Args:
            max_workers: Maximum number of parallel worker threads.
        """
        if isinstance(self.routing_daemon, SRMPLSDaemon):
            self.routing_daemon._enable_mpls_on_all_nodes(max_workers=max_workers)

    def set_sr_custom_path(
        self, source_id: int, dest_id: int, path: List[int]
    ) -> Dict[str, Any]:
        """Set a custom SR-MPLS path for a source-destination ground station pair.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.
            path: Ordered list of node IDs from source to destination.

        Returns:
            Dict with status, message, path, label_stack, and hop_count.
        """
        if not isinstance(self.routing_daemon, SRMPLSDaemon):
            raise RuntimeError("SR-MPLS daemon is not active")
        return self.routing_daemon.set_sr_custom_path(source_id, dest_id, path)

    def clear_sr_custom_path(self, source_id: int, dest_id: int) -> Dict[str, Any]:
        """Remove a custom SR-MPLS path and revert to shortest-path routing.

        Args:
            source_id: Source ground station ID.
            dest_id: Destination ground station ID.

        Returns:
            Dict with status, message, and reverted_to_shortest.
        """
        if not isinstance(self.routing_daemon, SRMPLSDaemon):
            raise RuntimeError("SR-MPLS daemon is not active")
        return self.routing_daemon.clear_sr_custom_path(source_id, dest_id)

    def get_sr_statistics(self) -> Dict[str, Any]:
        """Return SR-MPLS statistics from the routing manager.

        Returns:
            Dictionary with SR-MPLS state counters and route information.
        """
        if not isinstance(self.routing_daemon, SRMPLSDaemon):
            return {"enabled": False}
        return self.routing_daemon.get_sr_statistics()

    def list_sr_custom_paths(self) -> List[Dict[str, Any]]:
        """List all active custom SR-MPLS paths.

        Returns:
            List of dicts describing each custom path, its label stack, and hop count.
        """
        if not isinstance(self.routing_daemon, SRMPLSDaemon):
            return []
        return self.routing_daemon.list_sr_custom_paths()

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

                            except Exception as e:
                                logging.debug(
                                    f"Error processing interface {iface} on {sat_name}: {e}"
                                )
                                continue
                except FileNotFoundError:
                    logging.debug(f"No interface stats file found for {sat_name}")
                except Exception as e:
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
        except Exception as e:
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
                except Exception as e:
                    error += 1
                    logging.error(f"Failed to start tcpdump on {sat.name}: {e}")

    # ┌──────────────────────────────────────────────────────────────────────────────┐
    # │                                                                              │
    # │                        Public facing API methods                             │
    # │                                                                              │
    # └──────────────────────────────────────────────────────────────────────────────┘

    def start_gonetem(self) -> float | None:
        """Start GoNetEm by launching containers and wiring links.

        Returns:
            float: Time taken to start GoNetEm
        """
        tic = time.perf_counter()
        if self.get_gonetem_status():
            return

        if self.network_launcher.upper() == "GONETEM":
            from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher

            direct_launcher = GoNetEmLauncher(
                topology_manager=self,
                server_address=self.gonetem_server,
                project_name=self.project_name,
                isl_capacity_kbps=self.isl_link_capacity,
                gnd_capacity_kbps=self.gnd_link_capacity,
            )
        else:
            from satgonetem.launchers.direct_launcher import DirectLauncher

            direct_launcher = DirectLauncher(
                project_name=self.project_name,
                isl_capacity_kbps=self.isl_link_capacity,
                gnd_capacity_kbps=self.gnd_link_capacity,
                satellite_image=self.satellite_image,
            )

        all_nodes = list(self.satellites.values()) + list(self.ground_stations.values())
        active_links = [lnk for lnk in self.links.values() if lnk.is_active]

        hil = self.hil_manager
        if hil is not None:
            launch_nodes = [n for n in all_nodes if not hil.is_hil_node(n.name)]
            launch_links = [lnk for lnk in active_links if not hil.is_hil_link(lnk)]
            hil_links = [lnk for lnk in active_links if hil.is_hil_link(lnk)]
        else:
            launch_nodes = all_nodes
            launch_links = active_links
            hil_links = []

        direct_launcher.start_containers(launch_nodes, MAX_WORKERS)
        direct_launcher.wire_links(launch_links, MAX_WORKERS)

        if hil is not None:
            hil.wire_links(hil_links)

        self.direct_launcher = direct_launcher
        self.set_status(True)
        self.set_gonetem_status(True)
        self.start_time_ = time.time()

        return time.perf_counter() - tic

    def is_running_tcpdump(self) -> bool:
        """Check if tcpdump is already running on any satellite container."""
        for sat in self.satellites.values():
            if not hasattr(sat, "container") or sat.container is None:
                continue
            try:
                result = sat.container.exec_run("pgrep tcpdump", demux=False)
                if result.exit_code == 0:
                    return True
            except Exception:
                pass
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
            except Exception as e:
                logging.error(f"Failed to disable tcpdump on {sat.name}: {e}")

    def stop_gonetem(self) -> float:
        """Stop GoNetEm and clean up resources.

        Returns:
            float: Time taken to stop GoNetEm
        """
        tic = time.perf_counter()

        self.set_status(False)

        if self.hil_manager is not None:
            self.hil_manager.teardown_all()

        direct_launcher = getattr(self, "direct_launcher", None)

        if direct_launcher is not None:
            direct_launcher.close_project()

        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None

        return time.perf_counter() - tic

    # Simulation controls

    def start(self) -> None:
        """Start the simulation loop in a background thread.

        Raises:
            RuntimeError: If no project is loaded.
        """
        if self.get_running():
            return
        if not self.project_name:
            raise RuntimeError("Open or create a project first.")
        self._stop_evt.clear()
        self.set_running(True)
        self._sim_thread = threading.Thread(target=self._loop, daemon=True)
        self._sim_thread.start()

    def stop(self) -> None:
        """Stop the simulation loop and join the background thread."""
        self._stop_evt.set()
        self.set_running(False)
        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join()

    def next_step(self) -> float:
        """Advance the simulation by one step.

        Delegates directly to update_simulation.
        """
        tic = time.perf_counter()
        self.update_simulation()
        total = time.perf_counter() - tic

        return total if total > 0 else 0.00

    def speed_up(self) -> None:
        """Reduce the update factor to speed up simulation playback.

        Clamps to a minimum of 0.01.
        """
        self.update_factor = max(0.01, round(self.update_factor * 0.9, 2))

    def speed_down(self) -> None:
        """Increase the update factor to slow down simulation playback.

        Clamps to a maximum of 10.0.
        """
        self.update_factor = min(10.0, round(self.update_factor * 1.1, 2))

    def set_update_time(self, seconds: int) -> None:
        """Set the simulation tick interval in seconds.

        Args:
            seconds: Desired interval; clamped to a minimum of 1.
        """
        self.update_time = max(1, int(seconds))

    def _loop(self) -> None:
        """Simulation loop body (runs in a background thread).

        Calls update_simulation once per tick. The tick period is
        update_time * update_factor seconds, polled in 0.2-second slices
        so stop() is noticed promptly.
        """
        try:
            while not self._stop_evt.is_set():
                self.update_simulation()
                delay = max(0.01, self.update_time * self.update_factor)
                self._stop_evt.wait(timeout=delay)
        finally:
            self.set_running(False)

    def set_IP_addresses(self) -> float:
        """Set IPv4 addresses for all interfaces in the topology."""
        tic: float = time.perf_counter()
        self.set_ipv4s_for_all_nodes(set_lo=True, max_workers=MAX_WORKERS)

        return time.perf_counter() - tic

    def delete_routing(self) -> float:
        """Delete all installed static IP routes."""
        if not self.get_routing_initiated() or self.routing_daemon is None:
            logging.warning("Routing is not initiated, cannot delete routes")
            return -1.0
        tic: float = time.perf_counter()

        self.routing_daemon.remove(max_workers=MAX_WORKERS)
        self.set_routing_initiated(False)
        self.routing_method = None
        self.routing_daemon = None

        return time.perf_counter() - tic

    def init_routing(
        self, max_workers: int = MAX_WORKERS, routing_method: str = ""
    ) -> float:
        """
        Initialize routing based on the configured routing method.

        Args:
            max_workers: Maximum number of worker threads to use for parallel operations.
            routing_method: Optional routing method to initialize (overrides self.routing if provided).
        Returns:
            bool: True if initialization is successful, False otherwise.
        """
        tic = time.perf_counter()
        if routing_method:
            self.routing = routing_method

        if self.routing == "static":
            self.routing_daemon = StaticRoutingDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Static IP routing initialization failed")

                return -1.0
        elif self.routing == "dynamic-ospf":
            self.routing_daemon = OSPFDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Dynamic OSPF routing initialization failed")
                return -1.0
        elif self.routing == "dynamic-isis":
            self.routing_daemon = ISISBirdSRDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("Dynamic IS-IS SR (Bird) routing initialization failed")
                return -1.0
        elif self.routing == "sr-mpls":
            self.routing_daemon = SRMPLSDaemon(self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning("SR-MPLS routing initialization failed")
                return -1.0
        elif self.routing in self._daemon_registry:
            self.routing_daemon = self._daemon_registry[self.routing](self)
            if not self.routing_daemon.init(max_workers=max_workers):
                logging.warning(
                    f"Custom routing daemon '{self.routing}' initialization failed"
                )
                return -1.0
        else:
            logging.warning(
                f"Unknown routing method '{self.routing}', cannot initialize routing"
            )
            return -1.0

        self.set_routing_initiated(True)
        return time.perf_counter() - tic

    def get_allowed_routing_methods(self) -> List[str]:
        """Return a list of allowed routing methods."""
        return self.allowed_routing_methods

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
        except Exception as e:
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

    def get_node_IP_addresses(self, name: str) -> List[str]:
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

    def force_stop_gonetem(self) -> float:
        """Force stop GoNetEm without graceful cleanup.

        This method can be used in scenarios where the normal stop_gonetem process fails
        or when a quick reset is needed. It will attempt to kill all containers and clean
        up resources without waiting for graceful shutdown.

        Returns:
            float: Time taken to force stop GoNetEm
        """
        warnings.warn(
            "force_stop_gonetem does not clean up properly; "
            "prefer stop_gonetem() for a clean shutdown.",
            ResourceWarning,
            stacklevel=2,
        )
        tic = time.perf_counter()
        self.set_status(False)

        if self.hil_manager is not None:
            self.hil_manager.teardown_all()

        direct_launcher = getattr(self, "direct_launcher", None)

        if direct_launcher is not None:
            direct_launcher.close_project()

        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None

        return time.perf_counter() - tic

    def run_hping3(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Optional[Hping3Config] = None,
    ) -> List[Hping3Flow]:
        """Launch hping3 flows for all (source, destination) combinations.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one Hping3Flow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        All nodes must already be running containers (i.e., the topology
        must have been started via start_gonetem before calling this method).

        Args:
            source: Node or list of Nodes that will run hping3. Each must be
                present in this topology.
            destination: Node or list of Nodes that are the hping3 targets.
                Each must be present in this topology.
            config: hping3 run configuration. Defaults to Hping3Config() if
                not provided. The same config is reused for every flow.

        Returns:
            A list of started Hping3Flow objects, one per (source, destination)
            pair. Poll each flow.status() for Hping3Status.DONE or
            Hping3Status.ERROR, then call flow.results() to get Hping3Results.

        Example::

            config = Hping3Config(proto="tcp", dport=80, count=50, flags=["S"])
            flows = topology_manager.run_hping3([src1, src2], [dst1, dst2], config)
            for flow in flows:
                while flow.status() == Hping3Status.RUNNING:
                    time.sleep(0.2)
                flow.results().print_summary()
        """
        if config is None:
            config = Hping3Config()
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[Hping3Flow] = []
        for src in sources:
            for dst in destinations:
                f = Hping3Flow(src, dst, config)
                f.start()
                flows.append(f)
        return flows

    def run_iperf3(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Iperf3Config,
    ) -> List[Iperf3Flow]:
        """Launch iperf3 flows for all (source, destination) combinations.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one Iperf3Flow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        Both nodes must already be running containers (i.e., the topology
        must have been started via start_gonetem before calling this method).

        Args:
            source: Node or list of Nodes that will run the iperf3 client.
                Each must be a Satellite or GroundStation present in this
                topology.
            destination: Node or list of Nodes that will run the iperf3
                server. Each must be a Satellite or GroundStation present in
                this topology.
            config: Full iperf3 run configuration. The same config is reused
                for every flow. Construct with Iperf3Config to set protocol,
                bandwidth, congestion control, window size, and all other
                options.

        Returns:
            A list of started Iperf3Flow objects, one per (source, destination)
            pair. Poll each flow.status() for FlowStatus.DONE or
            FlowStatus.ERROR, then call flow.results() to get Iperf3Results.

        Raises:
            RuntimeError: If any flow cannot be started.

        Example::

            config = Iperf3Config(protocol="UDP", duration=30, bandwidth_mbps=50)
            flows = topology_manager.run_iperf3([src1, src2], [dst1, dst2], config)
            for flow in flows:
                while flow.status() == FlowStatus.RUNNING:
                    time.sleep(0.5)
                flow.results().print_summary()
        """
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[Iperf3Flow] = []
        for src in sources:
            for dst in destinations:
                f = Iperf3Flow(src, dst, config)
                f.start()
                flows.append(f)
        return flows

    def ping(
        self,
        source: Union[Node, List[Node]],
        destination: Union[Node, List[Node]],
        config: Optional[PingConfig] = None,
    ) -> List[PingFlow]:
        """Ping all (source, destination) combinations and return PingFlows.

        Accepts a single Node or a list of Nodes for each of source and
        destination. Creates one PingFlow per (source, destination) pair,
        starts each in a background thread, and returns the full list
        immediately.

        All nodes must already be running containers.

        Args:
            source: Node or list of Nodes that send ICMP echo requests.
            destination: Node or list of Nodes that are the ping targets.
            config: PingConfig controlling count, timeout, and interval.
                Defaults to PingConfig() when None. The same config is reused
                for every flow.

        Returns:
            A list of started PingFlow objects, one per (source, destination)
            pair. Poll each flow.status() for PingStatus.DONE or
            PingStatus.ERROR, then call flow.results() to retrieve PingResults.

        Example::

            flows = topology_manager.ping([gnd1, gnd2], [gnd3, gnd4])
            for flow in flows:
                while flow.status() == PingStatus.RUNNING:
                    time.sleep(0.2)
                flow.results().print_summary()
        """
        resolved_config = config or PingConfig()
        sources = source if isinstance(source, list) else [source]
        destinations = destination if isinstance(destination, list) else [destination]
        flows: List[PingFlow] = []
        for src in sources:
            for dst in destinations:
                f = PingFlow(src, dst, resolved_config)
                f.start()
                flows.append(f)
        return flows

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

    def fast_start(self, routing_method: str = "static") -> None:
        """Start GoNetEm with a fast startup sequence for rapid testing iterations.

        This method performs a streamlined startup process that skips some of the
        more time-consuming steps like waiting for containers to be fully ready or
        launching tcpdump. It is intended for use in development and testing scenarios
        where quick feedback is more valuable than a fully initialized environment.

        Args:
            routing_method: Optional routing method to initialize (default: 'static').
                            Must be one of the allowed routing methods.
        """
        self.start_gonetem()
        self.set_IP_addresses()
        self.init_routing(routing_method=routing_method)


def main():
    """Demonstrate topology lifecycle and simulation controls."""
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    """
    from satgonetem.utils.project_builder import create_test_project

    tic = time.perf_counter()

    project = create_test_project()

    topology_manager = TopologyManager.from_satcom(project)

    topology_manager.start_gonetem()

    topology_manager.set_IP_addresses()

    topology_manager.init_routing(routing_method="static")

    # Start a ping
    iperf3_config = Iperf3Config(
        protocol="UDP",
        duration=1800,
        interval=0.1,
        bandwidth_mbps=80,
        length="1000",
    )

    update_time = 1

    src = topology_manager.get_ground_stations()[1]
    dst = topology_manager.get_ground_stations()[2]

    topology_manager.set_update_time(update_time)  # Set tick interval to 10 seconds

    iperf3_flow = Iperf3Flow(src, dst, iperf3_config)
    iperf3_flow.start()

    while True:
        print(f"Current time step: {topology_manager.get_current_time_step()}")

        current_time_step = topology_manager.get_current_time_step()

        if iperf3_flow.status() in [FlowStatus.DONE, FlowStatus.ERROR]:
            break
        if current_time_step % 5 == 0:

            print(iperf3_flow.status())

        try:
            update = topology_manager.next_step()  # Advance simulation by one step
            if update > update_time:
                update = (
                    update_time - 0.01
                )  # Clamp update time to just under the tick interval
            time.sleep(
                update_time - update
            )  # Sleep for the remainder of the tick interval
        except KeyboardInterrupt:
            print("Simulation interrupted by user.")
            break

    iperf3_flow.stop()  # Ensure the flow is stopped

    data = json.dumps(iperf3_flow.results().to_json(), indent=2)

    with open("iperf3_results.json", "w") as f:
        f.write(data)

    topology_manager.stop_gonetem()


if __name__ == "__main__":
    main()
