import datetime
import logging
from typing import List
from sgp4.api import Satrec, WGS72, jday, SGP4_ERRORS

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

    def estimate_time_until_disconnection_2(
            self,
            ground_object: GroundObject,
            satellite: Satellite,
            minimum_elevation: float = 10.0,
            max_time: float = 3600.0,
    ) -> float:
        '''
        Analytically compute the time until the satellite drops below minimum elevation.
        Uses TEME position/velocity from get_position_earth_general_inertial and
        transforms to ECEF for a closed-form quadratic solution.
        '''

        # Helpers
        def lla_to_ecef(lat_deg, lon_deg, alt_m):
            """WGS-84 LLA to ECEF (meters)."""
            a = 6378137.0  # WGS-84 semi-major axis
            e2 = 0.00669437999013  # eccentricity squared
            lat = np.radians(lat_deg)
            lon = np.radians(lon_deg)
            N = a / np.sqrt(1 - e2 * np.sin(lat)**2)
            x = (N + alt_m) * np.cos(lat) * np.cos(lon)
            y = (N + alt_m) * np.cos(lat) * np.sin(lon)
            z = (N * (1 - e2) + alt_m) * np.sin(lat)
            return np.array([x, y, z])  # meters
        
        def teme_to_ecef(r_teme_km, t_utc: datetime.datetime):
            """Convert TEME position (km) to ECEF (km)."""
            jd, fr = jday(t_utc.year, t_utc.month, t_utc.day,
                        t_utc.hour, t_utc.minute, 
                        t_utc.second + t_utc.microsecond * 1e-6)
            # GMST calculation (IAU 1982 model)
            JD_utc = jd + fr
            T = (JD_utc - 2451545.0) / 36525.0
            gmst_deg = (
                280.46061837
                + 360.98564736629 * (JD_utc - 2451545.0)
                + 0.000387933 * T**2
                - (T**3) / 38710000.0
            )
            gmst_deg = gmst_deg % 360.0
            theta = np.deg2rad(gmst_deg)  # Earth rotation angle in radians
            # Rotation about Z-axis
            cos_t, sin_t = np.cos(theta), np.sin(theta)
            r_ecef = np.array([
                cos_t * r_teme_km[0] + sin_t * r_teme_km[1],
                -sin_t * r_teme_km[0] + cos_t * r_teme_km[1],
                r_teme_km[2]
            ])
            return r_ecef

        # Current UTC time from satellite clock
        t_utc = satellite.movement_model.clock.get_current_time()
        
        # Ground station LLA (lat, lon, alt in meters)
        gs_lat, gs_lon, gs_alt = ground_object.get_coordinates().to_latitude_longitude_altitude()
        
        # Satellite TEME position and velocity from SGP4
        r_teme_km, v_teme_km_s = satellite.movement_model.get_position_earth_general_inertial()
        
        # Convert satellite position to ECEF (meters)
        r_sat_ecef_km = teme_to_ecef(np.array(r_teme_km), t_utc)
        r_sat_ecef = r_sat_ecef_km * 1000.0  # to meters
        
        # Ground station in ECEF (meters)
        R_gs_ecef = lla_to_ecef(gs_lat, gs_lon, gs_alt)
        
        # Relative position (meters)
        s0 = r_sat_ecef - R_gs_ecef
        
        # Velocity in ECEF (m/s)
        # v_ecef = R @ v_teme - omega_E × r_ecef
        omega_E = 7.2921158553e-5  # Earth rotation rate [rad/s]
        v_ecef_km_s = teme_to_ecef(np.array(v_teme_km_s), t_utc)
        v_ecef_km_s[0] += omega_E * r_sat_ecef_km[1]
        v_ecef_km_s[1] -= omega_E * r_sat_ecef_km[0]
        v_ecef = v_ecef_km_s * 1000.0  # to m/s
        
        # Local "up" unit vector at ground station
        R_gs_norm = np.linalg.norm(R_gs_ecef)
        if R_gs_norm < 1e-6:
            return None
        up = R_gs_ecef / R_gs_norm
        
        # Quadratic coefficients for elevation-angle constraint
        # (s · up)^2 = |s|^2 * sin^2(eps_min)  at the boundary
        sin_eps = np.sin(np.radians(minimum_elevation))
        sin2_eps = sin_eps**2
        
        s0_dot_v = np.dot(s0, v_ecef)
        s0z = np.dot(s0, up)
        vz = np.dot(v_ecef, up)
        s0_sq = np.dot(s0, s0)
        v_sq = np.dot(v_ecef, v_ecef)
        
        A = vz**2 - v_sq * sin2_eps
        B = 2 * (s0z * vz - s0_dot_v * sin2_eps)
        C = s0z**2 - s0_sq * sin2_eps
        
        # Check if currently visible
        if C <= 0:
            return None  # Already at or below minimum elevation
        
        # Solve for roots
        if abs(A) < 1e-12:
            # Degenerate linear case: B*t + C = 0
            if abs(B) < 1e-12:
                return None
            dt = -C / B
            return dt if 0 < dt <= max_time else max_time
        
        discriminant = B**2 - 4*A*C
        if discriminant < 0:
            # Parabola never crosses zero → stays above minimum elevation
            return max_time
        
        sqrt_d = np.sqrt(discriminant)
        dt1 = (-B - sqrt_d) / (2*A)
        dt2 = (-B + sqrt_d) / (2*A)
        
        # Pick the smallest positive root as the exit time
        candidates = [dt for dt in [dt1, dt2] if dt > 0]
        if not candidates:
            return None
        
        dt_exit = min(candidates)
        return dt_exit if dt_exit <= max_time else max_time

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
        import time as _t
        for visible_satellite in visible_satellites:
            tic = _t.perf_counter()
            time_until_disconnection = self.estimate_time_until_disconnection_2(
                ground_object=ground_object,
                satellite=visible_satellite.satellite,
                minimum_elevation=ground_object.ground_object_domain.elevation_above_horizon,
            )
            tac = _t.perf_counter()
            time_until_disconnection_2 = self.estimate_time_until_disconnection_2(
                ground_object=ground_object,
                satellite=visible_satellite.satellite,
                minimum_elevation=ground_object.ground_object_domain.elevation_above_horizon,
            )
            print(time_until_disconnection, time_until_disconnection_2)
            toc = _t.perf_counter()
            print(f"First method took {tac - tic:.4f} seconds, second method took {toc - tac:.4f} seconds for satellite {visible_satellite.satellite.satellite_name}")
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
