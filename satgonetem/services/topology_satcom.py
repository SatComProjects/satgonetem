"""
This version of TopologyManager will work simultaneously, in real-time, with satcomtopology
Satellites will include a satcom_object, which will correspond to their satcomtopology counterpart, and not be imported from graphs


"""

from satgonetem.utils.satcom_fix import apply_satcom_fix

apply_satcom_fix()

from satgonetem.utils.utils import time_

import threading
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Callable, Type, Union
import json
import logging
import os
import time

from satgonetem.models.interface import Interface
from satgonetem.launchers.base_launcher import NetworkLauncher

from satgonetem.routing.base_daemon import RoutingDaemon

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite
from satgonetem.link_budget.config import AntennaConfig, LinkBudgetConfig
from satgonetem.traffic import PingConfig, PingFlow
from satgonetem.traffic.ping_utils import PingStatus

from sat_com_adapter.adapters import NetworkXAdapter
from sat_com_application.simulation_manager import SimulationManager
from sat_com_builder.models import SimulationProperty
from satgonetem.utils.project_builder import create_and_load_simulation

from satgonetem.services.mixins.topology_sync import TopologySyncMixin
from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.services.mixins.interface_mgr import InterfaceMgrMixin
from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin
from satgonetem.services.mixins.simulation_loop import SimulationLoopMixin
from satgonetem.services.mixins.routing_mgr import RoutingManagerMixin
from satgonetem.services.mixins.traffic_testing import TrafficTestingMixin
from satgonetem.services.mixins.diagnostics import DiagnosticsMixin


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
    use_budget: bool = False


class TopologyManager(
    TopologySyncMixin,
    LinkOpsMixin,
    InterfaceMgrMixin,
    NetworkLifecycleMixin,
    SimulationLoopMixin,
    RoutingManagerMixin,
    TrafficTestingMixin,
    DiagnosticsMixin,
):
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

    @time_
    @classmethod
    def from_satcom(
        cls,
        simulation_property: SimulationProperty,
        network_config: Optional[NetworkConfig] = None,
    ) -> "TopologyManager":
        """Create a TopologyManager from a SimulationProperty.

        Builds the SimulationManager from the project configuration in memory
        without requiring any YAML files on disk. Network configuration
        attributes (capacities, routing, protocol) are set to their defaults
        and can be overridden on the returned instance.

        Args:
            simulation_property: A configured SimulationProperty instance.
            network_config: A NetworkConfig instance to override default values.
        Returns:
            An initialised TopologyManager backed by the project's SimulationManager.
        """
        sim_manager = create_and_load_simulation(
            simulation_property.model_dump(), simulation_property.simulation_name
        )
        if network_config is None:
            network_config = NetworkConfig()
        instance = cls(simulation_manager=sim_manager, network_config=network_config)
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
                use_budget=self.use_budget,
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

        instance = cls.from_satcom(simulation_property, network_config)
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

        # Link budget configuration applied automatically to new links.
        self.link_budget_config: Optional[LinkBudgetConfig] = None

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
        self.use_budget = config.use_budget

    def load_config(self) -> None:
        """No-op: configuration is supplied at construction time via from_satcom()."""

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

    def set_link_budget_config(self, config: LinkBudgetConfig) -> None:
        """Apply a link-budget configuration to all existing and future links.

        The configuration is stored on the manager and propagated to every
        link currently in ``self.links``.  New links created afterwards
        automatically inherit it.

        Args:
            config: ``LinkBudgetConfig`` instance with downlink/uplink frequencies.
        """
        self.link_budget_config = config
        for link in self.links.values():
            link.link_budget_config = config
            if link.use_budget and link.type == "GroundStationLink":
                link.update_link_capacities()

    def set_antenna(
        self, nodes: list[Satellite] | list[GroundStation], config: AntennaConfig
    ) -> None:
        """Attach an antenna built from *config* to every node in *nodes*.

        Args:
            nodes: Iterable of :class:`~satgonetem.models.node.Node` instances.
            config: ``AntennaConfig`` describing the antenna to create.
        """
        antenna = config.to_antenna()
        for node in nodes:
            node.antenna = antenna

    def check_for_updates(self) -> dict:
        """Check for updates in satellites, ground stations, and links.

        Returns:
            dict with bool values for 'satellites', 'ground_stations', 'links'.
        """
        updates = {
            "satellites": False,
            "ground_stations": False,
            "links": False,
        }

        new_sat_hash = self._compute_satellites_hash()
        new_gs_hash = self._compute_ground_stations_hash()
        new_links_hash = self._compute_links_hash()

        if new_sat_hash != self.satellites_hash:
            updates["satellites"] = True
            self.satellites_hash = new_sat_hash

        if new_gs_hash != self.ground_stations_hash:
            updates["ground_stations"] = True
            self.ground_stations_hash = new_gs_hash

        if new_links_hash != self.links_hash:
            updates["links"] = True
            self.links_hash = new_links_hash

        return updates

    def _compute_satellites_hash(self):
        return hash(
            frozenset((sat.id, sat.hash_node()) for sat in self.satellites.values())
        )

    def _compute_ground_stations_hash(self):
        return hash(
            frozenset((gs.id, gs.hash_node()) for gs in self.ground_stations.values())
        )

    def _compute_links_hash(self):
        return hash(
            frozenset(
                (
                    frozenset((link.source.hash_node(), link.target.hash_node())),
                    link.is_active,
                    link.distance,
                    link.delay,
                )
                for link in self.links.values()
            )
        )

    def get_current_time(self):
        """Get the current time from the simulation manager."""
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        return self.simulation_manager.time_manager.get_current_time()

    def get_current_time_step(self) -> int:
        """Get the current time step counter."""
        return self.current_time_step

    def get_project_duration_in_timesteps(self) -> int:
        """Get the project duration in time steps."""
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        total_seconds = (self.end_time - self.start_time).total_seconds()
        return int(total_seconds / self.update_time)

    def reset_simulation(self) -> None:
        """Reset the simulation to the start time."""
        print("Resetting simulation to start time")
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        self.simulation_manager.time_manager.set_time(
            self.simulation_manager.time_manager.start_date
        )
        self.current_time_step = 0
        self.simulation_manager.time_manager.execute_actions()

    def _update_simulation_manager_time(self) -> None:
        """Tick the simulation manager by one update interval."""
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        self.simulation_manager.time_manager.tick(self.update_time)

    def get_current_graph(self):
        """Get the current topology as a NetworkX graph."""
        import networkx as nx

        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        if self.nx_adapter is None:
            raise ValueError("nx_adapter is not set")

        if not self.preference:
            logging.info("No preference provided, Defaulting to distance based")

        available_preferences = [
            "hops_prefer_ISLs",
            "hops_no_preference",
            "latency_prefer_ISLs",
            "latency_no_preference",
        ]

        if self.preference not in available_preferences:
            logging.warning(
                f"Preference '{self.preference}' is not recognized. "
                f"Available options: {available_preferences}. "
                "Defaulting to 'latency_no_preference'."
            )
            self.preference = "latency_no_preference"

        nx_adapter = self.nx_adapter
        graph = nx_adapter.create_full_networkx_graph(
            export_link_length=True,
            export_object_position=True,
            enable_export_flows_data=False,
        )

        for u, v in graph.edges():
            if self.preference in ["hops_prefer_ISLs", "hops_no_preference"]:
                graph[u][v]["weight"] = 1
                if (
                    self.preference == "hops_prefer_ISLs"
                    and graph[u][v].get("type") == "InterSatelliteLink"
                ):
                    graph[u][v]["weight"] = 0.5
            elif self.preference in ["latency_prefer_ISLs", "latency_no_preference"]:
                graph[u][v]["weight"] = graph[u][v].get("distance", 1)
                if (
                    self.preference == "latency_prefer_ISLs"
                    and graph[u][v].get("type") == "InterSatelliteLink"
                ):
                    graph[u][v]["weight"] *= 0.1

        return graph

    def get_satellites(self) -> List[Satellite]:
        """Return the list of satellites."""
        return list(self.satellites.values())

    def get_ground_stations(self) -> List[GroundStation]:
        """Return the list of ground stations."""
        return list(self.ground_stations.values())

    @staticmethod
    def apply_satcom_fix() -> None:
        from satgonetem.utils.satcom_fix import apply_satcom_fix

        apply_satcom_fix()


def main():
    """Demonstrate topology lifecycle and simulation controls."""
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    """
    from satgonetem.utils.project_builder import create_test_project

    TopologyManager.apply_satcom_fix()

    project = create_test_project()

    topology_manager = TopologyManager.from_satcom(project)

    # for link in topology_manager.links.values():
    #     if link.type == "GroundStationLink":
    #         print(
    #             f"Peer1 throughput: {link.peer1_capacity} kbps, Peer2 throughput: {link.peer2_capacity} kbps"
    #         )

    # ## Create antennas
    # satellites = topology_manager.get_satellites()
    # antenna_satellite_config = AntennaConfig(
    #     diameter=0.3,  # meters
    #     efficiency=0.6,  # unitless
    # )
    # topology_manager.set_antenna(
    #     satellites,
    #     antenna_satellite_config,
    # )

    # ground_stations = topology_manager.get_ground_stations()
    # antenna_gnd_config = AntennaConfig(
    #     diameter=2.0,  # meters
    #     efficiency=0.7,  # unitless
    # )
    # topology_manager.set_antenna(
    #     ground_stations,
    #     antenna_gnd_config,
    # )

    # topology_manager.set_link_budget_config(
    #     LinkBudgetConfig(
    #         downlink_freq_ghz=19.0,
    #         uplink_freq_ghz=14.25,
    #         bandwidth_hz_downlink=100e6,
    #         bandwidth_hz_uplink=100e6,
    #     )
    # )

    # for link in topology_manager.links.values():
    #     if link.type == "GroundStationLink":
    #         print(
    #             f"Peer1 throughput: {link.peer1_capacity} kbps, Peer2 throughput: {link.peer2_capacity} kbps"
    #         )

    topology_manager.start_gonetem()

    topology_manager.set_ip_addresses()

    topology_manager.init_routing(routing_method="static")

    topology_manager.stop_gonetem()


if __name__ == "__main__":
    main()
