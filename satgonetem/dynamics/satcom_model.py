"""
SatComModel: DynamicsModel implementation backed by the sat_com_model /
sat_com_application / sat_com_adapter libraries.

All satellite-constellation simulation logic (loading the simulation,
syncing topology objects, advancing time, managing connection strategies,
etc.) lives here so that TopologyManager can remain library-agnostic.
"""

import logging
import random
from abc import abstractmethod
from datetime import datetime
from typing import List, Optional

from sat_com_adapter.adapters import NetworkXAdapter
from sat_com_application.simulation_manager import SimulationManager

from satgonetem.dynamics.base import DynamicsModel
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite


class SatComModel(DynamicsModel):
    """Dynamics model that wraps sat_com_application's SimulationManager.

    Intended to be used as a mixin base for TopologyManager:

        class TopologyManager(SatComModel):
            ...

    All attributes below are declared here for type-checker visibility but are
    initialised by the concrete subclass (TopologyManager).
    """

    simulation_manager: SimulationManager
    nx_adapter: Optional[NetworkXAdapter]
    satellites: dict[int, Satellite]
    ground_stations: dict[int, GroundStation]
    links: dict[frozenset, Link]
    satellites_hash: Optional[int]
    ground_stations_hash: Optional[int]
    links_hash: Optional[int]
    update_time: int
    start_time: datetime
    end_time: datetime
    current_time_step: int
    preference: str
    allowed_strategies: List[str]
    gnd_link_capacity: int
    isl_link_capacity: int

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Time management
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Simulation update
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Graph
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Node accessors
    # ------------------------------------------------------------------

    def get_satellites(self) -> List[Satellite]:
        """Return the list of satellites."""
        return list(self.satellites.values())

    def get_ground_stations(self) -> List[GroundStation]:
        """Return the list of ground stations."""
        return list(self.ground_stations.values())
