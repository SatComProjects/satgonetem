"""Simulation-project loading and tick-action validation for TopologyManager.

These helpers translate between sat_com project objects and a construction
ready state.  They never import ``topology_satcom``; the facade composes the
returned manager itself.
"""

from __future__ import annotations

from sat_com_application.simulation_manager import SimulationManager
from sat_com_builder.models import SimulationProperty

from satgonetem.utils.project_builder import create_and_load_simulation


def load_simulation(simulation_property: SimulationProperty) -> SimulationManager:
    """Build and load the SimulationManager from an in-memory project.

    Args:
        simulation_property: A configured SimulationProperty instance.

    Returns:
        The loaded SimulationManager for the project.
    """
    return create_and_load_simulation(
        simulation_property.model_dump(), simulation_property.simulation_name
    )


def require_tick_action(simulation_manager: SimulationManager | None) -> None:
    """Keep sat_com_topology's default per-tick update.

    When the simulation is loaded, SimulationManager.set_time_manager() registers
    the simulation's own update as the time manager's tick action. It runs the
    update protocol: by default satellites, ground stations, user terminals and
    every link type, inter-satellite links included (see
    SimulationManager.get_default_update_protocol). Nothing is registered here;
    this only checks that a tick action exists.

    Args:
        simulation_manager: The manager whose time manager must have a tick
            action.

    Raises:
        ValueError: If *simulation_manager* is None, or if its time manager has
            no tick action (the simulation would never update).
    """
    if simulation_manager is None:
        raise ValueError("Simulation manager is not set")

    if not simulation_manager.time_manager.on_tick_actions:
        raise ValueError(
            "The time manager has no tick action: load the simulation through "
            "sat_com_topology (SimulationManager.set_time_manager) so its "
            "default update runs on each tick"
        )
