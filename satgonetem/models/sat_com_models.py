from pydantic import BaseModel
from typing import List, Optional, Literal

# ==============================
# Pydantic models
# ==============================


class ConstellationProperty(BaseModel):
    identifier: str
    amount_of_orbit_plane: int
    amount_of_satellite_per_orbit_plane: int
    inclination: float
    phase_difference_between_satellites: bool
    mean_revolution_per_day: float


class OrbitalConnectivityProperty(BaseModel):
    adjacent_inter_satellite_shifting: int
    maximum_inter_satellite_count: int
    maximum_inter_satellite_range_distance: float
    maximum_ground_station_range: float
    maximum_user_terminal_range: float
    maximum_connected_ground_object: int
    maximum_connected_user_terminal: int
    maximum_connected_ground_station: int


class WalkerShell(BaseModel):
    type: Literal["delta", "star"]
    constellation_property: ConstellationProperty
    orbital_connectivity_property: OrbitalConnectivityProperty
    ground_object_white_list: List[str]


class ConnectivityProperties(BaseModel):
    ground_to_space_connections_strategy: str
    elevation_above_horizon: int
    maximum_satellite_range_distance: float
    shell_white_lists: List[str]
    maximum_connected_satellites: int


class GroundObject(BaseModel):
    identifier: str
    data_file: str
    type: Literal["ground_station", "user_terminal"]
    connectivity_properties: ConnectivityProperties


class CustomSatellite(BaseModel):
    name: str
    tle_line1: str
    tle_line2: str


class MPLSConfiguration(BaseModel):
    """MPLS/LSR routing configuration"""

    label_range_start: int = 16
    label_range_end: int = 1048575
    use_ldp: bool = False
    php: bool = True  # Penultimate Hop Popping
    ttl: int = 64


class NetworkProperties(BaseModel):
    inter_satellite_link_capacity: int
    ground_station_link_capacity: int
    routing_method: str  # 'Static', 'Dynamic', 'MPLS'
    protocol: str
    mpls_config: Optional[MPLSConfiguration] = None


class Configuration(BaseModel):
    simulation_name: str
    start_date: str
    end_date: str
    movement_model: str
    distance_model: str
    ground_objects_properties: List[GroundObject]
    walker_shells: List[WalkerShell]
    disable_ground_station_link_preload: bool
    static_ground_station_link_mode: bool
