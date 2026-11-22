"""
This version of TopologyManager will work simultaneously, in real-time, with satcomtopology
Satellites will include a satcom_object, which will correspond to their satcomtopology counterpart, and not be imported from graphs


"""

from satgonetem.utils.satcom_fix import apply_satcom_fix

apply_satcom_fix()


import json
import logging
import threading
import time
from typing import Any, ClassVar

from sat_com_adapter.adapters import NetworkXAdapter
from sat_com_application.simulation_manager import (
    SimulationManager,
)
from sat_com_builder.models import SimulationProperty

from satgonetem.launchers.base_launcher import NetworkLauncher
from satgonetem.link_budget.config import AntennaConfig, LinkBudgetConfig
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite
from satgonetem.routing.base_daemon import RoutingDaemon
from satgonetem.routing.registry import available_methods, is_known_method
from satgonetem.services.mixins.backend_control import BackendControlMixin
from satgonetem.services.mixins.controller import ControllerMixin
from satgonetem.services.mixins.diagnostics import DiagnosticsMixin
from satgonetem.services.mixins.interface_mgr import InterfaceMgrMixin
from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.services.mixins.network_lifecycle import NetworkLifecycleMixin
from satgonetem.services.mixins.routing_mgr import RoutingManagerMixin
from satgonetem.services.mixins.simulation_loop import SimulationLoopMixin
from satgonetem.services.mixins.topology_sync import TopologySyncMixin
from satgonetem.services.mixins.traffic_testing import TrafficTestingMixin
from satgonetem.services.network_config import (
    MAX_LINK_QUEUE_PACKETS as MAX_LINK_QUEUE_PACKETS,
)
from satgonetem.services.network_config import (
    NetworkConfig as NetworkConfig,
)
from satgonetem.services.network_config import apply_config, config_from_instance
from satgonetem.services.satcom_project import load_simulation, require_tick_action
from satgonetem.services.topology_graph import (
    build_current_graph,
    compute_ground_stations_hash,
    compute_links_hash,
    compute_satellites_hash,
)
from satgonetem.services.topology_graph import (
    check_for_updates as check_topology_updates,
)
from satgonetem.services.topology_persistence import (
    RUNTIME_DATE_FORMAT,
    apply_runtime_state,
    ground_file_restore_path,
    restore_runtime_state,
    restore_simulation_property,
    save_to_file,
    serialize_runtime_state,
)

# Backward-compatible private alias; persistence owns the canonical format.
_RUNTIME_DATE_FORMAT = RUNTIME_DATE_FORMAT


def _ground_file_restore_path(ground_dir: str, basename: object) -> str:
    """Return a safe restore path for an embedded ground station data file."""
    return ground_file_restore_path(ground_dir, basename)


class TopologyManager(
    TopologySyncMixin,
    LinkOpsMixin,
    InterfaceMgrMixin,
    NetworkLifecycleMixin,
    SimulationLoopMixin,
    RoutingManagerMixin,
    TrafficTestingMixin,
    DiagnosticsMixin,
    ControllerMixin,
    BackendControlMixin,
):
    """Satellite network topology manager.

    Manages satellites, ground stations, links, and the active routing daemon.
    Custom routing methods can be registered via register_routing_daemon before
    constructing an instance.
    """

    _daemon_registry: ClassVar[dict[str, type[RoutingDaemon]]] = {}

    @classmethod
    def register_routing_daemon(
        cls, name: str, daemon_class: type[RoutingDaemon]
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
        if is_known_method(name):
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
    def from_satcom(
        cls,
        simulation_property: SimulationProperty,
        network_config: NetworkConfig | None = None,
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
        sim_manager = load_simulation(simulation_property)
        if network_config is None:
            network_config = NetworkConfig()
        instance = cls(simulation_manager=sim_manager, network_config=network_config)
        instance.simulation_property = simulation_property
        return instance

    @classmethod
    def from_simulation_manager(
        cls,
        simulation_manager: SimulationManager,
        network_config: NetworkConfig | None = None,
    ) -> "TopologyManager":
        """Create a TopologyManager from an existing SimulationManager.

        Args:
            simulation_manager: An already-initialized SimulationManager instance.
            network_config: A NetworkConfig instance to override default values.
        Returns:
            An initialised TopologyManager backed by the provided SimulationManager.
        """
        if network_config is None:
            network_config = NetworkConfig()
        instance = cls(
            simulation_manager=simulation_manager, network_config=network_config
        )
        return instance

    def _serialize_runtime_state(self) -> dict[str, Any]:
        """Extract satellites, ground objects and links from the live SimulationManager.

        Returns:
            A dictionary that can be passed to _deserialize_runtime_state().
        """
        return serialize_runtime_state(self)

    def to_file(self, path: str, network_config: NetworkConfig | None = None) -> None:
        """Save the satcom configuration and network config to a JSON file.

        Serialises the SimulationProperty backing this instance, any referenced
        ground station data files, and the NetworkConfig so the topology can be
        fully reconstructed via from_file().

        When the TopologyManager was created from a SimulationManager with
        programmatically added satellites and links (rather than from a
        SimulationProperty), the runtime state is also serialised. That state
        covers satellites, ground stations, user terminals, points of presence
        and every link type between them.

        Args:
            path: Filesystem path where the JSON file will be written.
            network_config: NetworkConfig to persist. If None, a NetworkConfig
                is built from the current instance attributes.

        Raises:
            OSError: If the file cannot be written or a ground station data
                file cannot be read.
        """
        if network_config is None:
            network_config = self._network_config_from_instance()

        save_to_file(
            self,
            path,
            network_config,
            serialize_state=self._serialize_runtime_state,
        )

    @classmethod
    def _deserialize_runtime_state(
        cls, runtime_state: dict[str, Any], network_config: NetworkConfig
    ) -> "TopologyManager":
        """Rebuild a TopologyManager from a saved runtime state.

        Args:
            runtime_state: Dictionary produced by _serialize_runtime_state().
            network_config: NetworkConfig to apply to the new instance.

        Returns:
            A fully initialised TopologyManager.
        """
        sim_manager = restore_runtime_state(runtime_state, network_config)
        instance = cls.from_simulation_manager(sim_manager, network_config)
        apply_runtime_state(instance, runtime_state)
        return instance

    @classmethod
    def from_file(
        cls,
        path: str,
        network_config: NetworkConfig | None = None,
    ) -> "TopologyManager":
        """Load a TopologyManager from a JSON file produced by to_file().

        Restores embedded ground station data files to a private temporary
        directory created with ``tempfile.mkdtemp()``, then reconstructs the
        SimulationProperty and NetworkConfig.

        When the file contains a ``runtime_state`` block (produced when saving a
        topology that was built programmatically via ``from_simulation_manager``),
        the full runtime state—including custom satellites, ground stations,
        user terminals, points of presence, and their links—is restored instead.

        Args:
            path: Path to the JSON file previously written by to_file().
            network_config: Optional config that replaces the persisted
                ``network_config`` wholesale. Use it to enable IPv6 before the
                address plan is built. When omitted, the persisted config is
                used, so existing call sites are unchanged.

        Returns:
            An initialised TopologyManager with the persisted (or overridden)
            configuration applied.

        Raises:
            FileNotFoundError: If path does not exist.
            ValueError: If the file content is not a valid topology config, or
                an embedded ground station file name is unsafe.
            OSError: If ground station data files cannot be written.
        """
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)

        if network_config is None:
            network_config = NetworkConfig(**payload["network_config"])

        if "runtime_state" in payload:
            return cls._deserialize_runtime_state(
                payload["runtime_state"], network_config
            )

        simulation_property = restore_simulation_property(payload, path)
        instance = cls.from_satcom(simulation_property, network_config)
        if "runtime_overlay" in payload:
            apply_runtime_state(instance, payload["runtime_overlay"])
        return instance

    def __init__(
        self,
        simulation_manager: SimulationManager,
        network_config: NetworkConfig | None = None,
    ):
        start_time = time.time()
        logging.info(
            f"\033[92m=== Starting Topology Manager Initialization === [{start_time}]\033[0m"
        )
        self.simulation_manager = simulation_manager
        self.simulation_property: SimulationProperty | None = None
        self._setup_update_actions()
        self._apply_network_config(network_config or NetworkConfig())
        self.nx_adapter = NetworkXAdapter(self.simulation_manager)

        self.satellites: dict[int, Satellite] = {}
        self.ground_stations: dict[int, GroundStation] = {}
        self.links: dict[frozenset[(str)], Link] = {}
        # Link keys whose backend add failed and must be retried before local
        # interfaces are built. Initialised eagerly so the add thread pool
        # cannot race on first use.
        self._failed_link_adds: set[frozenset[str]] = set()
        # Links whose backend add succeeded while their local interfaces or
        # addresses are still pending. Retries must not repeat LinkAdd.
        self._backend_link_adds: set[frozenset[str]] = set()
        self.interfaces: list[Interface] = []  # List of all interfaces in the network
        self.interface_version = 0

        # QoS thingies
        self.use_file_routes = False

        # Network Properties
        self.preference = "latency_prefer_ISLs"

        # Time related things
        self.current_time_step = 0
        self.start_time = self.simulation_manager.time_manager.start_date
        self.end_time = self.simulation_manager.time_manager.end_date

        # Active network launcher (GoNetEm)
        self.launcher: NetworkLauncher | None = None

        # Topology-wide address plans, built during init().
        self.address_plan: Any = None
        self.address_plan_v6: Any = None

        # Optional HIL manager. Set this before calling start_gonetem() to
        # replace specific ground stations with host-bridged hardware.
        self.hil_manager = None

        # Link budget configuration applied automatically to new links.
        self.link_budget_config: LinkBudgetConfig | None = None

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

        self.allowed_routing_methods = available_methods(self._daemon_registry)

        # Routing initialization flag - only update routes when routing is explicitly enabled
        self.routing_initiated = False

        # Simulation loop state
        self.running: bool = False
        self.update_factor: float = 1.0
        self._stop_evt: threading.Event = threading.Event()
        self._sim_thread: threading.Thread | None = None
        self._lifecycle_lock: threading.Lock = threading.Lock()
        # Most recent exception raised by a simulation loop tick (None when no
        # tick has failed). Read via SimulationLoopMixin.get_last_simulation_error.
        self.last_simulation_error: BaseException | None = None
        # Serializes loop ticks and API-driven topology mutations (next_step,
        # move_to_time, simulate_link_failure). Reentrant because API entry
        # points nest update_simulation; deliberately separate from
        # _lifecycle_lock, which stop() holds while joining the loop thread.
        self._simulation_lock: threading.RLock = threading.RLock()

        # Active routing daemon - set during init_routing() based on routing method
        self.routing_daemon: RoutingDaemon | None = None

        # Track active traffic flows started via TrafficTestingMixin
        self._active_flows: list = []

        if self.simulation_manager is not None:
            self.init()

        end_time = time.time()
        logging.info(
            f"\033[92m=== Topology Manager Initialization Complete === [{end_time}] Duration: {end_time - start_time:.2f}s\033[0m"
        )

    def _network_config_from_instance(self) -> NetworkConfig:
        """Build a NetworkConfig from the current instance fields."""
        return config_from_instance(self)

    def _apply_network_config(self, config: NetworkConfig) -> None:
        """Apply a NetworkConfig to this instance's network attributes.

        Args:
            config: NetworkConfig instance whose values are written to self.
                If config.project_name is None, falls back to the simulation
                manager's project_name attribute, or "Constellation".
        """
        apply_config(self, config)

    def load_config(self) -> None:
        """No-op: configuration is supplied at construction time via from_satcom()."""

    def _setup_update_actions(self) -> None:
        """Keep sat_com_topology's default per-tick update.

        When the simulation is loaded, SimulationManager.set_time_manager() registers
        the simulation's own update as the time manager's tick action. It runs the
        update protocol: by default satellites, ground stations, user terminals and
        every link type, inter-satellite links included (see
        SimulationManager.get_default_update_protocol). Nothing is registered here;
        this only checks that a tick action exists.

        Raises:
            ValueError: If self.simulation_manager is None, or if its time manager
                has no tick action (the simulation would never update).
        """
        require_tick_action(getattr(self, "simulation_manager", None))

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
                link.refresh_link_capacities()

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

        for link in self.links.values():
            if link.use_budget and link.type == "GroundStationLink":
                link.refresh_link_capacities()

    def check_for_updates(self) -> dict:
        """Check for updates in satellites, ground stations, and links.

        Returns:
            dict with bool values for 'satellites', 'ground_stations', 'links'.
        """
        return check_topology_updates(self)

    def _compute_satellites_hash(self):
        return compute_satellites_hash(self)

    def _compute_ground_stations_hash(self):
        return compute_ground_stations_hash(self)

    def _compute_links_hash(self):
        return compute_links_hash(self)

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
        return build_current_graph(self)

    def get_satellites(self) -> list[Satellite]:
        """Return the list of satellites."""
        return list(self.satellites.values())

    def get_ground_stations(self) -> list[GroundStation]:
        """Return the list of ground stations."""
        return list(self.ground_stations.values())

    def get_links(self) -> list[Link]:
        """Return the list of links."""
        return list(self.links.values())

    @staticmethod
    def apply_satcom_fix() -> None:
        from satgonetem.utils.satcom_fix import apply_satcom_fix

        apply_satcom_fix()

    def set_addressable_satellites(
        self, satellites: list[Satellite] | Satellite
    ) -> None:
        """
        Set addressable status to multiple satellites
        """
        if any(
            not isinstance(sat, Satellite)
            for sat in (satellites if isinstance(satellites, list) else [satellites])
        ):
            raise ValueError("All items in satellites must be instances of Satellite")

        if isinstance(satellites, Satellite):
            satellites.set_addressable(True)
            return

        for satellite in satellites:
            satellite.set_addressable(True)

    def connectivity_matrix(
        self,
        nodes: list[Any] | None = None,
        *,
        max_workers: int = 128,
        max_pairs: int | None = 10_000,
        config: Any = None,
    ) -> Any:
        """Ping every ordered pair and return a typed matrix.

        Args:
            nodes: Nodes to test. Defaults to every ground station plus every
                addressable satellite, matching :meth:`test_connectivity`.
                Duplicate node names are ignored before the pair count is
                checked and before any flow is scheduled.
            max_workers: Maximum concurrent ping flows.
            max_pairs: Safety bound for the all-pairs expansion. Raise before
                scheduling when the deduplicated pair count exceeds it. Pass
                ``None`` to disable the bound.
            config: Optional ``PingConfig``; the connectivity helper supplies
                a fast ten-ping default when omitted.

        Returns:
            A :class:`satgonetem.connectivity.ConnectivityMatrix`.
        """
        from satgonetem.connectivity import build_connectivity_matrix

        if nodes is None:
            nodes = [
                *self.get_ground_stations(),
                *[sat for sat in self.get_satellites() if sat.is_addressable()],
            ]
        else:
            nodes = list(nodes)

        # Deduplicate before counting: the builder ignores repeated names, so
        # counting raw entries could reject a topology that schedules no extra
        # pairs (and >100 unique nodes is a supported legacy topology).
        unique_nodes = []
        seen: set[str] = set()
        for node in nodes:
            name = str(getattr(node, "name", node))
            if name in seen:
                continue
            seen.add(name)
            unique_nodes.append(node)

        pair_count = len(unique_nodes) * (len(unique_nodes) - 1)
        if max_pairs is not None and pair_count > max_pairs:
            raise ValueError(
                f"Connectivity matrix would schedule {pair_count} pairs, "
                f"exceeding max_pairs={max_pairs}; pass an explicit node subset "
                "or raise max_pairs"
            )

        return build_connectivity_matrix(
            unique_nodes,
            config=config,
            max_workers=max_workers,
        )

    def test_connectivity(
        self,
        max_workers: int = 128,
        *,
        max_pairs: int | None = None,
    ) -> dict:
        """Ping every ground station and addressable satellite and print a summary.

        Kept for compatibility with the original convenience helper. The legacy
        API accepted any number of nodes, so the default is unbounded; pass
        ``max_pairs`` to opt into the :meth:`connectivity_matrix` safety bound.
        The full typed result is available from :meth:`connectivity_matrix` and
        is also returned here as a dictionary.
        """
        matrix = self.connectivity_matrix(
            max_workers=max_workers,
            max_pairs=max_pairs,
        )
        matrix.print_summary()
        return matrix.to_dict()


def main():
    print(
        "You're not supposed to run this file directly! This is a library module, import and use TopologyManager from your own script."
    )


if __name__ == "__main__":
    main()
