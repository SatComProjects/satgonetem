import datetime
import logging
from typing import List

import numpy as np
from sat_com_connectivity.ground_object_to_space_connectivity.ground_connection_strategy_registry import (
    register_ground_to_space_connectivity_strategy,
)


from sat_com_connectivity.ground_object_to_space_connectivity.base import (
    GroundToSpaceConnectionStrategy,
)
from sat_com_connectivity.ground_object_to_space_connectivity.models import (
    ComputedGroundObjectConnectionsResult,
    GroundToSpaceConnection,
)

from sat_com_model.models import Satellite, GroundObject, GroundToSpaceLink
from sat_com_connectivity.ground_object_to_space_connectivity.visibility_angle_utils import (
    get_visible_satellites,
    VisibleSatellite,
)
from sat_com_trajectopy.angle_utils import get_elevation_angle


@register_ground_to_space_connectivity_strategy
class LongestConnectionTimeStrategy(GroundToSpaceConnectionStrategy):
    """
    Connection strategy that connects ground objects to the furthest away satellites that are getting closer.
    It sorts the visible satellites by their elevation above horizon and then checks which of them is getting closer to the ground object.
    If a satellite is getting closer, it is added to the list of considered satellites. The strategy then connects the ground object to the best considered satellite.
    If no satellites are getting closer, it connects the ground object to the best visible satellite.
    If the ground object already has a connection to a satellite, it will keep that connection as long as it is still visible.
    Minimizes number of handover
    """

    strategy_name = "longest-connection-time-strategy"

    def is_approaching(
        self, satellites: list[VisibleSatellite], ground_object: GroundObject
    ) -> bool:
        """
        Check if the satellite is approaching the ground object.
        """
        output = []
        for satellite in satellites:
            coordinates_satellite = (
                satellite.satellite.get_coordinates().to_latitude_longitude_altitude()
            )
            future_coordinates_satellite = (
                satellite.satellite.movement_model.orbital_object.get_lonlatalt(
                    satellite.satellite.movement_model.clock.get_current_time()
                    + datetime.timedelta(seconds=10)
                )
            )
            angular_velocity_direction = [
                future_coordinates_satellite[1] - coordinates_satellite[0],
                future_coordinates_satellite[0] - coordinates_satellite[1],
            ]
            angular_velocity_direction = np.array(
                angular_velocity_direction
            ) / np.linalg.norm(angular_velocity_direction)

            coordinates_ground_object = (
                ground_object.get_coordinates().to_latitude_longitude_altitude()
            )

            difference_vector = [
                coordinate_ground_object - coordinate_satellite
                for coordinate_satellite, coordinate_ground_object in zip(
                    coordinates_satellite[:2], coordinates_ground_object[:2]
                )
            ]

            difference_vector = np.array(difference_vector) / np.linalg.norm(
                difference_vector
            )

            dot_product = np.dot(angular_velocity_direction, difference_vector)

            if dot_product > 0:
                # The satellite is approaching the ground object
                output.append((satellite, dot_product))
        # Sort the output by angle in descending order

        output.sort(
            key=lambda x: x[1], reverse=True
        )  # Sort by angle (the higher the dot product, the closer the satellite is approaching the ground object)
        output = [satellite for satellite, _ in output]
        return output

    def estimate_time_until_disconnection(
        self,
        ground_object: GroundObject,
        satellite: Satellite,
        minimum_elevation: float = 10.0,
        tol: float = 0.1,
        max_time: float = 3600.0,
    ) -> float:
        # Cache coordinates and times
        now = satellite.movement_model.clock.get_current_time()
        gnd_lat, gnd_lon, gnd_alt = (
            ground_object.get_coordinates().to_latitude_longitude_altitude()
        )

        # Helper to compute elevation at now + delta_t seconds
        def elev_at(delta_t: float) -> float:
            try:
                lon, lat, alt = satellite.movement_model.orbital_object.get_lonlatalt(
                    now + datetime.timedelta(seconds=delta_t)
                )
            except Exception as e:
                lon, lat, alt = satellite.movement_model.get_position_at(
                    now + datetime.timedelta(seconds=delta_t)
                )
            return get_elevation_angle(
                sat_coordinates=(lat, lon, alt),
                gnd_coordinates=(gnd_lat, gnd_lon, gnd_alt),
            )

        # Check current elevation
        current_elev = elev_at(0.0)
        if current_elev < minimum_elevation:
            raise ValueError(
                f"Satellite {satellite.satellite_name} is already below minimum elevation "
                f"of {minimum_elevation}° (current: {current_elev:.2f}°)."
            )

        # Find an upper bound t_high where elevation ≤ minimum_elevation
        t_low = 0.0
        t_high = min(300.0, max_time)  # start with a 5-minute guess

        while elev_at(t_high) > minimum_elevation and t_high < max_time:
            t_high = min(t_high * 2, max_time)

        if elev_at(t_high) > minimum_elevation:
            # never dips below within max_time
            logging.warning(
                f"Satellite {satellite.satellite_name} stays above {minimum_elevation}° "
                f"for at least {max_time}s; returning {max_time}s."
            )
            return max_time

        # Bisection between t_low (elev > min) and t_high (elev ≤ min)
        while t_high - t_low > 1e-3:  # 1ms resolution on time
            t_mid = 0.5 * (t_low + t_high)
            if elev_at(t_mid) > minimum_elevation:
                t_low = t_mid
            else:
                t_high = t_mid

        return t_high

    def compute_ground_object_connection_strategy(
        self,
        ground_object: GroundObject,
        satellites: List[Satellite],
    ):

        maximum_number_of_links = (
            ground_object.ground_object_domain.maximum_connected_satellites
        )

        computed_ground_object_connections_result = (
            ComputedGroundObjectConnectionsResult()
        )

        # Remove all existing links, we will compute them all anyway
        computed_ground_object_connections_result.obsolete_links.extend(
            [
                link
                for link in ground_object.links
                if isinstance(link, GroundToSpaceLink)
            ]
        )

        visible_satellites = get_visible_satellites(
            satellites=satellites, ground_object=ground_object
        )

        # There is a linear relationship between distance and elevation so lets sort by elevation
        visible_satellites.sort(key=lambda x: x.elevation_above_horizon, reverse=True)

        ############## First check: Keep existing connections if they are still valid ##############

        for visible_satellite in visible_satellites:

            if (
                visible_satellite.satellite
                in [
                    link.source
                    for link in ground_object.links
                    if isinstance(
                        link, GroundToSpaceLink
                    )  # This checks if satellite is already connected
                ]
                and len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                < maximum_number_of_links
            ):  # This checks if the maximum number of links is not exceeded
                new_link = GroundToSpaceConnection(
                    ground_object=ground_object,
                    satellite=visible_satellite.satellite,
                    connection_info={
                        "elevation_above_horizon": visible_satellite.elevation_above_horizon
                    },
                )

                # Keep the connection if it already exists
                computed_ground_object_connections_result.new_ground_object_connections.append(
                    new_link
                )

        if (
            len(computed_ground_object_connections_result.new_ground_object_connections)
            >= maximum_number_of_links
        ):
            # If we already have enough connections, we can stop here

            return computed_ground_object_connections_result

        list_of_satellites = []
        for visible_satellite in visible_satellites:
            time_until_disconnection = self.estimate_time_until_disconnection(
                ground_object=ground_object,
                satellite=visible_satellite.satellite,
                minimum_elevation=ground_object.ground_object_domain.elevation_above_horizon,
            )

            list_of_satellites.append((visible_satellite, time_until_disconnection))

        # Sort the visible satellites by time until disconnection
        list_of_satellites.sort(key=lambda x: x[1], reverse=True)

        for visible_satellite, time_until_disconnection in list_of_satellites:
            if (
                len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                >= maximum_number_of_links
            ):
                # If we already have enough connections, we can stop here
                break
            new_link = GroundToSpaceConnection(
                ground_object=ground_object,
                satellite=visible_satellite.satellite,
                connection_info={
                    "elevation_above_horizon": visible_satellite.elevation_above_horizon
                },
            )
            computed_ground_object_connections_result.new_ground_object_connections.append(
                new_link
            )

        return computed_ground_object_connections_result


@register_ground_to_space_connectivity_strategy
class OneLinkPerLayer(GroundToSpaceConnectionStrategy):
    """
    Connection strategy that connects ground objects to a single satellite per layer.
    It sorts the visible satellites by their elevation above horizon and then connects the ground object to the best visible satellite in each layer.
    If the ground object already has a connection to a satellite, it will keep that connection as long as it is still visible.
    This strategy is useful in multi-layer networks where we want to minimize the number of connections and ensure that each layer is represented.
    It is a simple strategy that does not take into account the time until disconnection or the distance to the ground object.
    It is a good strategy for networks with a small number of layers
    """

    strategy_name = "one-link-per-layer"

    def compute_ground_object_connection_strategy(
        self,
        ground_object: GroundObject,
        satellites: List[Satellite],
    ):

        maximum_number_of_links = (
            ground_object.ground_object_domain.maximum_connected_satellites
        )

        visible_satellites = get_visible_satellites(
            satellites=satellites, ground_object=ground_object
        )

        connected_layers = set()
        for visible_satellite in visible_satellites:
            # Get the layer of the satellite
            layer = visible_satellite.satellite.walker_shell.identifier
            if layer not in connected_layers:
                connected_layers.add(layer)

        if maximum_number_of_links < len(connected_layers):
            logging.warning(
                f"Ground object {ground_object.label} has a maximum of {maximum_number_of_links} links, "
                f"but there are {len(connected_layers)} layers. "
                f"Increasing maximum number of links to {len(connected_layers)}."
            )
            maximum_number_of_links = len(connected_layers)

        computed_ground_object_connections_result = (
            ComputedGroundObjectConnectionsResult()
        )

        # Remove all existing links, we will compute them all anyway
        computed_ground_object_connections_result.obsolete_links.extend(
            [
                link
                for link in ground_object.links
                if isinstance(link, GroundToSpaceLink)
            ]
        )

        # There is a linear relationship between distance and elevation so lets sort by elevation
        visible_satellites.sort(key=lambda x: x.elevation_above_horizon, reverse=True)

        ############## First check: Keep existing connections if they are still valid ##############

        for visible_satellite in visible_satellites:

            if (
                visible_satellite.satellite
                in [
                    link.source
                    for link in ground_object.links
                    if isinstance(
                        link, GroundToSpaceLink
                    )  # This checks if satellite is already connected
                ]
                and len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                < maximum_number_of_links
            ):  # This checks if the maximum number of links is not exceeded
                new_link = GroundToSpaceConnection(
                    ground_object=ground_object,
                    satellite=visible_satellite.satellite,
                    connection_info={
                        "elevation_above_horizon": visible_satellite.elevation_above_horizon
                    },
                )

                # Keep the connection if it already exists
                computed_ground_object_connections_result.new_ground_object_connections.append(
                    new_link
                )

        if (
            len(computed_ground_object_connections_result.new_ground_object_connections)
            >= maximum_number_of_links
        ):
            # If we already have enough connections, we can stop here
            return computed_ground_object_connections_result

        # Create a set to keep track of the layers we have already connected to
        connected_layers = set()

        for visible_satellite in visible_satellites:
            if (
                len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                >= maximum_number_of_links
            ):
                # If we already have enough connections, we can stop here
                break

            # Get the layer of the satellite
            layer = visible_satellite.satellite.walker_shell.identifier

            if layer not in connected_layers:
                # If we haven't connected to this layer yet, connect to the satellite
                new_link = GroundToSpaceConnection(
                    ground_object=ground_object,
                    satellite=visible_satellite.satellite,
                    connection_info={
                        "elevation_above_horizon": visible_satellite.elevation_above_horizon
                    },
                )
                computed_ground_object_connections_result.new_ground_object_connections.append(
                    new_link
                )
                connected_layers.add(layer)

        return computed_ground_object_connections_result


@register_ground_to_space_connectivity_strategy
class WeightedConnection(GroundToSpaceConnectionStrategy):
    """
    Connection strategy that connects ground objects to the best visible satellites based on their elevation above horizon AND the number of already connected ground
    stations to the satellite.
    """

    strategy_name = "weighted-connection"

    elevation_weight = 0.5
    connected_stations_weight = 0.5
    maximum_connected_stations = 3

    def compute_ground_object_connection_strategy(
        self,
        ground_object: GroundObject,
        satellites: List[Satellite],
    ):

        maximum_number_of_links = (
            ground_object.ground_object_domain.maximum_connected_satellites
        )

        visible_satellites = get_visible_satellites(
            satellites=satellites, ground_object=ground_object
        )

        computed_ground_object_connections_result = (
            ComputedGroundObjectConnectionsResult()
        )

        # Remove all existing links, we will compute them all anyway
        computed_ground_object_connections_result.obsolete_links.extend(
            [
                link
                for link in ground_object.links
                if isinstance(link, GroundToSpaceLink)
            ]
        )

        # There is a linear relationship between distance and elevation so lets sort by elevation
        visible_satellites.sort(key=lambda x: x.elevation_above_horizon, reverse=True)

        ############## First check: Keep existing connections if they are still valid ##############

        for visible_satellite in visible_satellites:

            if (
                visible_satellite.satellite
                in [
                    link.source
                    for link in ground_object.links
                    if isinstance(
                        link, GroundToSpaceLink
                    )  # This checks if satellite is already connected
                ]
                and len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                < maximum_number_of_links
            ):  # This checks if the maximum number of links is not exceeded
                new_link = GroundToSpaceConnection(
                    ground_object=ground_object,
                    satellite=visible_satellite.satellite,
                    connection_info={
                        "elevation_above_horizon": visible_satellite.elevation_above_horizon
                    },
                )

                # Keep the connection if it already exists
                computed_ground_object_connections_result.new_ground_object_connections.append(
                    new_link
                )

        if (
            len(computed_ground_object_connections_result.new_ground_object_connections)
            >= maximum_number_of_links
        ):
            # If we already have enough connections, we can stop here
            return computed_ground_object_connections_result

        # Create a list to store the weighted visible satellites
        weighted_visible_satellites = []
        for visible_satellite in visible_satellites:
            # Get the number of already connected ground stations to the satellite
            connected_stations = len(
                [
                    link
                    for link in visible_satellite.satellite.links
                    if isinstance(link, GroundToSpaceLink)
                ]
            )

            # Calculate the weight based on elevation and connected stations
            normalized_elevation = (
                visible_satellite.elevation_above_horizon
                - ground_object.ground_object_domain.elevation_above_horizon
            ) / (90 - ground_object.ground_object_domain.elevation_above_horizon)
            weight = (
                self.elevation_weight * normalized_elevation
                + self.connected_stations_weight
                * (1 - connected_stations / self.maximum_connected_stations)
            )

            weighted_visible_satellites.append((visible_satellite, weight))

        # Sort the weighted visible satellites by weight in descending order
        weighted_visible_satellites.sort(key=lambda x: x[1], reverse=True)

        # print('====================================================')
        # for visible_satellite, weight in weighted_visible_satellites:
        #     print(f"Satellite {visible_satellite.satellite.satellite_name} has elevation {visible_satellite.elevation_above_horizon}° and weight {weight:.2f}.\n"
        #           f"It is connected to {len([link for link in visible_satellite.satellite.links if isinstance(link, GroundToSpaceLink)])} ground stations.")

        # input()

        # Connect the ground object to the best visible satellites based on the weights
        for visible_satellite, weight in weighted_visible_satellites:
            # Check if we have reached the maximum number of links
            if (
                len(
                    computed_ground_object_connections_result.new_ground_object_connections
                )
                >= maximum_number_of_links
            ):
                break

            # Create a new link for the ground object and the visible satellite
            new_link = GroundToSpaceConnection(
                ground_object=ground_object,
                satellite=visible_satellite.satellite,
                connection_info={
                    "elevation_above_horizon": visible_satellite.elevation_above_horizon
                },
            )

            computed_ground_object_connections_result.new_ground_object_connections.append(
                new_link
            )

        return computed_ground_object_connections_result
