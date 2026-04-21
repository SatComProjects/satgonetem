from abc import ABC, abstractmethod
from datetime import datetime
from typing import List


class DynamicsModel(ABC):
    """Abstract base class for satellite constellation dynamics models.

    Defines the interface that any dynamics backend must implement, allowing
    TopologyManager to be used with different simulation libraries.
    """

    @abstractmethod
    def load_config(self) -> None:
        """Load and parse the configuration required by this dynamics model."""
        ...

    @abstractmethod
    def init(self) -> None:
        """Initialize topology entities (satellites, ground stations, links)."""
        ...

    @abstractmethod
    def check_for_updates(self) -> dict:
        """Check for topology changes since the last call.

        Returns:
            dict with bool values for 'satellites', 'ground_stations', 'links'.
        """
        ...

    @abstractmethod
    def update_simulation(self) -> None:
        """Advance the simulation by one timestep and apply topology changes."""
        ...

    @abstractmethod
    def move_to_time(self, new_time: datetime) -> None:
        """Jump the simulation to a specific absolute time."""
        ...

    @abstractmethod
    def get_current_time(self):
        """Return the current simulation time."""
        ...

    @abstractmethod
    def reset_simulation(self) -> None:
        """Reset the simulation back to the start time."""
        ...

    @abstractmethod
    def get_current_graph(self):
        """Return the current topology as a NetworkX graph."""
        ...

    @abstractmethod
    def get_satellites(self) -> list:
        """Return the list of all satellite nodes."""
        ...

    @abstractmethod
    def get_ground_stations(self) -> list:
        """Return the list of all ground-station nodes."""
        ...
