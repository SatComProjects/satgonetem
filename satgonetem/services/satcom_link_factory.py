"""Link construction helpers for :class:`TopologySyncMixin`.

These functions read topology state from a duck-typed manager: satellite and
ground-station dictionaries, link capacities, and link-budget settings.  The
mixin keeps same-named delegates because ``DummySync`` test harnesses override
``_create_link_from_satcom_link``.
"""

from __future__ import annotations

import logging
from typing import Any

from sat_com_model.models import InterSatelliteLinkDirection
from sat_com_model.models import Link as SatComLink

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link
from satgonetem.models.satellite import Satellite
from satgonetem.utils.utils import distance_3d_km, distance_3d_km_many


def create_link_from_satcom_link(manager: Any, sat_com_link: SatComLink) -> Link:
    """Construct a Link from a sat_com_model SatComLink.

    Resolves the source and destination nodes from the manager's satellites and
    ground stations, computes the 3-D distance in metres, and maps the ISL
    direction enum to a human-readable string.

    Args:
        manager: Object exposing ``satellites``, ``ground_stations`` and the
            link-capacity/budget attributes.
        sat_com_link: The sat_com_model link object to convert.

    Returns:
        A fully initialised Link instance with satcom_object set.
    """
    src_uid = sat_com_link.source.topology_uniq_id
    match manager.satellites.get(src_uid):
        case Satellite() as link_source:
            pass
        case None:
            match manager.ground_stations.get(src_uid):
                case GroundStation() as link_source:  # type: ignore[misc]
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
            raise TypeError(f"Expected Satellite in satellites, got {type(unexpected)}")

    dst_uid = sat_com_link.destination.topology_uniq_id
    match manager.satellites.get(dst_uid):
        case Satellite() as link_destination:
            pass
        case None:
            match manager.ground_stations.get(dst_uid):
                case GroundStation() as link_destination:  # type: ignore[misc]
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
            raise TypeError(f"Expected Satellite in satellites, got {type(unexpected)}")

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

    link_direction_id = getattr(
        sat_com_link,
        "inter_satellite_direction",
        InterSatelliteLinkDirection.UNDEFINED,
    )
    link_direction_map = {
        InterSatelliteLinkDirection.ORBITAL: "Orbital",
        InterSatelliteLinkDirection.ADJACENT: "Adjacent",
        InterSatelliteLinkDirection.UNDEFINED: "Undefined",
    }

    if link_type == "InterSatelliteLink":
        default_capacity_kbps = manager.isl_link_capacity
    elif link_type == "GroundObjectLink":
        default_capacity_kbps = getattr(
            manager, "ground_object_link_capacity", manager.gnd_link_capacity
        )
    else:
        default_capacity_kbps = manager.gnd_link_capacity

    link = Link(
        source=link_source,
        target=link_destination,
        distance=link_distance,
        type=link_type,
        direction=link_direction_map.get(link_direction_id, "Undefined"),
        is_active=link_is_active,
        default_capacity_kbps=default_capacity_kbps,
        use_budget=manager.use_budget,
        link_budget_config=manager.link_budget_config,
        loss=getattr(manager, "link_loss_percent", 0.0),
        jitter=getattr(manager, "link_jitter_ms", 0),
    )
    link.satcom_object = sat_com_link
    return link


def create_test_link(manager: Any, node1: Any, node2: Any) -> Link | None:
    """Create a test link between two nodes for performance testing.

    Args:
        manager: Object exposing ``links``, ``use_budget`` and the link-budget
            settings.
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
        except (TypeError, ValueError, KeyError) as e:
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
            default_capacity_kbps=10000,
            is_active=True,
            use_budget=manager.use_budget,
            link_budget_config=manager.link_budget_config,
            loss=getattr(manager, "link_loss_percent", 0.0),
            jitter=getattr(manager, "link_jitter_ms", 0),
        )

        # Ensure delay is an integer
        test_link.delay = int(test_link.delay) if test_link.delay else 1

        # Mark the link for addition
        test_link.to_add = True

        # Add to internal links dictionary
        link_key = frozenset([node1.name, node2.name])

        # Check if link already exists
        if link_key not in manager.links:
            manager.links[link_key] = test_link
            logging.info(
                f"Created test link {node1.name} -- {node2.name} (distance: {int(distance / 1000)} km, delay: {test_link.delay}ms)"
            )
            return test_link
        else:
            logging.debug(f"Test link {node1.name} -- {node2.name} already exists")
            return None

    except (TypeError, ValueError, KeyError) as e:
        logging.error(
            f"Error creating test link between {node1.name} and {node2.name}: {e}"
        )
        return None


def refresh_link_distances(links: list[Link]) -> int:
    """Refresh link distances and delays in one vectorized batch.

    Args:
        links: Existing Link objects whose endpoints have fresh positions.

    Returns:
        Number of links that could not be refreshed.
    """
    if not links:
        return 0

    try:
        distances_km = distance_3d_km_many(
            [link.source.position["latitude"] for link in links],
            [link.source.position["longitude"] for link in links],
            [link.source.position["altitude"] for link in links],
            [link.target.position["latitude"] for link in links],
            [link.target.position["longitude"] for link in links],
            [link.target.position["altitude"] for link in links],
        )
    except Exception as exc:
        # Keep the per-link path as a fallback for malformed positions.
        logging.error(
            "Vectorized link-distance refresh failed (%s); falling back",
            exc,
        )
        errors = 0
        for link in links:
            try:
                link.sync_distance_from_satcom_and_delay()
            except Exception as link_exc:
                errors += 1
                logging.error(
                    "Error refreshing distance for %s--%s: %s",
                    link.source.name,
                    link.target.name,
                    link_exc,
                )
        return errors

    errors = 0
    for link, distance_km in zip(links, distances_km, strict=False):
        try:
            link.update_distance_and_delay(float(distance_km) * 1000.0)
        except Exception as exc:
            errors += 1
            logging.error(
                "Error refreshing distance for %s--%s: %s",
                link.source.name,
                link.target.name,
                exc,
            )
    return errors
