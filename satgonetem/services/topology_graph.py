"""Topology graph export and change detection for TopologyManager.

The helpers read the manager's satellite/ground-station/link dictionaries and
its ``nx_adapter``.  They do not import the facade, so they stay leaf-ward.
"""

from __future__ import annotations

import logging
from typing import Any

from satgonetem.services.topology_link_plan import ensure_inter_satellite_directions


def compute_satellites_hash(manager: Any) -> int:
    """Return a change-detection hash over the manager's satellites."""
    return hash(
        frozenset((sat.id, sat.hash_node()) for sat in manager.satellites.values())
    )


def compute_ground_stations_hash(manager: Any) -> int:
    """Return a change-detection hash over the manager's ground stations."""
    return hash(
        frozenset((gs.id, gs.hash_node()) for gs in manager.ground_stations.values())
    )


def compute_links_hash(manager: Any) -> int:
    """Return a change-detection hash over the manager's links."""
    return hash(
        frozenset(
            (
                frozenset((link.source.hash_node(), link.target.hash_node())),
                link.is_active,
                link.distance,
                link.delay,
            )
            for link in manager.links.values()
        )
    )


def check_for_updates(manager: Any) -> dict:
    """Check for updates in satellites, ground stations, and links.

    Args:
        manager: Object whose hash trackers are compared and refreshed.

    Returns:
        dict with bool values for 'satellites', 'ground_stations', 'links'.
    """
    updates = {
        "satellites": False,
        "ground_stations": False,
        "links": False,
    }

    new_sat_hash = compute_satellites_hash(manager)
    new_gs_hash = compute_ground_stations_hash(manager)
    new_links_hash = compute_links_hash(manager)

    if new_sat_hash != manager.satellites_hash:
        updates["satellites"] = True
        manager.satellites_hash = new_sat_hash

    if new_gs_hash != manager.ground_stations_hash:
        updates["ground_stations"] = True
        manager.ground_stations_hash = new_gs_hash

    if new_links_hash != manager.links_hash:
        updates["links"] = True
        manager.links_hash = new_links_hash

    return updates


def build_current_graph(manager: Any):
    """Get the current topology as a NetworkX graph.

    Args:
        manager: Object exposing ``simulation_manager``, ``nx_adapter``,
            ``preference``, ``satellites``, ``ground_stations`` and ``links``.

    Returns:
        A NetworkX graph with weighted edges for the active topology.
    """
    if manager.simulation_manager is None:
        raise ValueError("Simulation manager is not set")
    if manager.nx_adapter is None:
        raise ValueError("nx_adapter is not set")

    if not manager.preference:
        logging.info("No preference provided, Defaulting to distance based")

    available_preferences = [
        "hops_prefer_ISLs",
        "hops_no_preference",
        "latency_prefer_ISLs",
        "latency_no_preference",
    ]

    if manager.preference not in available_preferences:
        logging.warning(
            f"Preference '{manager.preference}' is not recognized. "
            f"Available options: {available_preferences}. "
            "Defaulting to 'latency_no_preference'."
        )
        manager.preference = "latency_no_preference"

    nx_adapter = manager.nx_adapter
    # Custom-created links may leave the optional direction unset, while
    # the networkx adapter assumes an enum. Normalize before export.
    ensure_inter_satellite_directions(
        manager.simulation_manager.get_inter_satellites_links()
    )
    graph = nx_adapter.create_full_networkx_graph(
        export_object_position=True,
        enable_export_flows_data=False,
    )

    def wrapper_link(u, v):
        def ref(node_id):
            if node_id in manager.satellites:
                return f"Sat{node_id}"
            if node_id in manager.ground_stations:
                return f"Gnd{node_id}"
            return None

        source, target = ref(u), ref(v)
        if source is None or target is None:
            return None
        return manager.links.get(frozenset((source, target)))

    def is_routable(u, v, data):
        if data.get("disabled", False):
            return False
        link = wrapper_link(u, v)
        if link is None:
            return True
        # sat_com_topology 5 keeps the disabled flag on the link object;
        # the exported edge data only carries the model dump on request.
        return link.is_active and not getattr(link.satcom_object, "disabled", False)

    graph.remove_edges_from(
        [(u, v) for u, v, data in graph.edges(data=True) if not is_routable(u, v, data)]
    )

    for u, v in graph.edges():
        if manager.preference in ["hops_prefer_ISLs", "hops_no_preference"]:
            graph[u][v]["weight"] = 1
            if (
                manager.preference == "hops_prefer_ISLs"
                and graph[u][v].get("type") == "InterSatelliteLink"
            ):
                graph[u][v]["weight"] = 0.5
        elif manager.preference in ["latency_prefer_ISLs", "latency_no_preference"]:
            graph[u][v]["weight"] = graph[u][v].get("distance_km", 1)
            if (
                manager.preference == "latency_prefer_ISLs"
                and graph[u][v].get("type") == "InterSatelliteLink"
            ):
                graph[u][v]["weight"] *= 0.1

    return graph
