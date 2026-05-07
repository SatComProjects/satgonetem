"""TopologySyncMixin for TopologyManager."""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from satgonetem.models.ground_station import GroundStation
from satgonetem.models.satellite import Satellite
from satgonetem.models.link import Link
from satgonetem.utils.utils import distance_3d_km
from sat_com_model.models import InterSatelliteLinkDirection
from sat_com_model.models import Link as SatComLink
from sat_com_model.models import Satellite as SatComSatellite
from sat_com_model.models import GroundStation as SatComGroundStation

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.node import Node
    from typing import Optional


class TopologySyncMixin:
    """TopologySync functionality."""

    def init(self) -> None:
        """Initialize topology from the simulation manager."""
        self._sync_satellites()
        self._sync_ground_stations()
        self._sync_links()
        self._assign_interfaces_to_nodes()
        self._set_ips_to_nodes()
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
            default_capacity_kbps = self.isl_link_capacity
        elif link_type == "GroundObjectLink":
            default_capacity_kbps = getattr(self, "ground_object_link_capacity", self.gnd_link_capacity)
        else:
            default_capacity_kbps = self.gnd_link_capacity

        link = Link(
            source=link_source,
            target=link_destination,
            distance=link_distance,
            type=link_type,
            direction=link_direction_map.get(link_direction_id, "Undefined"),
            is_active=link_is_active,
            default_capacity_kbps=default_capacity_kbps,
            use_budget=self.use_budget,
            link_budget_config=self.link_budget_config,
        )
        link.satcom_object = sat_com_link
        return link

    def _build_link_key(self, source: "Node", target: "Node") -> frozenset:
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
                default_capacity_kbps=int(10000),
                is_active=True,
                use_budget=self.use_budget,
                link_budget_config=self.link_budget_config,
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

        except (TypeError, ValueError, KeyError) as e:
            logging.error(
                f"Error creating test link between {node1.name} and {node2.name}: {e}"
            )
            return None
