"""
Abstract base class for network launchers.

A launcher is responsible for the full lifecycle of a simulated network:
  - starting node containers / processes
  - wiring virtual links between them
  - updating link parameters at each simulation step
  - tearing everything down

Subclass and implement the abstract methods to plug in a different backend
(e.g. a GoNetEm-based launcher, a pure-namespace launcher, etc.).
"""

from abc import ABC, abstractmethod
from typing import Callable, Optional


class NetworkLauncher(ABC):
    """Manages the lifecycle of a simulated satellite network."""

    def __init__(
        self,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        ground_object_capacity_kbps: int = 100_000,
    ) -> None:
        self.project_name = project_name
        self.isl_capacity_kbps = isl_capacity_kbps
        self.gnd_capacity_kbps = gnd_capacity_kbps
        self.ground_object_capacity_kbps = ground_object_capacity_kbps

    # Lifecycle

    @abstractmethod
    def start_containers(
        self,
        nodes: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Start (or provision) all node containers / processes."""

    @abstractmethod
    def wire_links(
        self,
        links: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Create all virtual links and apply initial qdiscs."""

    @abstractmethod
    def close_project(
        self,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Tear down all containers / processes for this project."""

    # Per-step link management

    @abstractmethod
    def update_link(self, link) -> None:
        """Update delay and rate on both ends of an existing link."""

    @abstractmethod
    def add_link(self, link) -> None:
        """Create a new link and apply qdiscs to both ends."""

    @abstractmethod
    def delete_link(self, link) -> None:
        """Remove a link (both ends)."""

    @abstractmethod
    def set_link_capacities(self, isl_kbps: int, gnd_kbps: int, links: list) -> None:
        """Update the default ISL / GSL capacities and push them to all links."""
