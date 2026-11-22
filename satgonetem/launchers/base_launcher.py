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
from collections.abc import Callable


class NetworkLauncher(ABC):
    """Manages the lifecycle of a simulated satellite network."""

    def __init__(
        self,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        ground_object_capacity_kbps: int = 100_000,
        netem_limit: int = 1000,
        txqueuelen: int = 1000,
    ) -> None:
        self.project_name = project_name
        self.isl_capacity_kbps = isl_capacity_kbps
        self.gnd_capacity_kbps = gnd_capacity_kbps
        self.ground_object_capacity_kbps = ground_object_capacity_kbps
        # Queue length of the emulated links, in packets. 0 means "let the
        # backend use its own default".
        self.netem_limit = netem_limit
        self.txqueuelen = txqueuelen

    # Lifecycle

    @abstractmethod
    def start_containers(
        self,
        nodes: list,
        workers: int = 64,
        progress_cb: Callable[[str, int, int], None] | None = None,
        controller: bool = False,
        links: list | None = None,
    ) -> tuple[float, float]:
        """Start (or provision) all node containers / processes.

        Returns:
            ``(container_time, link_time)`` in seconds.
        """

    @abstractmethod
    def wire_links(
        self,
        links: list,
        workers: int = 64,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Create all virtual links and apply initial qdiscs."""

    @abstractmethod
    def close_project(
        self,
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Tear down all containers / processes for this project."""

    @abstractmethod
    def force_close_project(self) -> bool:
        """Best-effort teardown after a failed startup or a failed close.

        Implementations must clean up whatever backend state is still
        reachable. They must not assume a healthy client/session remains: a
        preceding ``close_project()`` may already have dropped it.

        Returns:
            True when the backend is confirmed cleaned up, including when
            there was nothing to clean. False when cleanup could not be
            completed, so the caller can retain its handles and retry.
        """

    # Per-step link management

    @abstractmethod
    def update_link(self, link) -> None:
        """Update delay and rate on both ends of an existing link."""

    @abstractmethod
    def add_link(self, link) -> None:
        """Create a new link and apply qdiscs to both ends.

        Raises:
            RuntimeError: If the backend could not create the link.
        """

    @abstractmethod
    def delete_link(self, link) -> None:
        """Remove a link (both ends).

        Raises:
            RuntimeError: If the backend could not delete the link.
        """

    @abstractmethod
    def set_link_capacities(self, isl_kbps: int, gnd_kbps: int, links: list) -> None:
        """Update the default ISL / GSL capacities and push them to all links."""
