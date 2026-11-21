"""Factories for domain objects used in tests.

The factories create real model instances with deterministic positions and
interfaces. They do not touch Docker, gRPC, or the filesystem.
"""

from __future__ import annotations

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.interface import Interface
from satgonetem.models.link import Link
from satgonetem.models.node import Node
from satgonetem.models.satellite import Satellite


def _position(latitude: float, longitude: float, altitude_km: float) -> dict:
    return {
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude_km,
    }


def make_satellite(
    name: str = "Sat0",
    *,
    latitude: float = 0.0,
    longitude: float = 0.0,
    altitude_km: float = 550.0,
) -> Satellite:
    """Create a satellite with a deterministic position."""
    satellite = Satellite(name)
    satellite.position = _position(latitude, longitude, altitude_km)
    return satellite


def make_ground_station(
    name: str = "Gnd0",
    *,
    latitude: float = 0.0,
    longitude: float = 0.0,
    altitude_km: float = 0.0,
) -> GroundStation:
    """Create a ground station with a deterministic position."""
    station = GroundStation(name)
    station.position = _position(latitude, longitude, altitude_km)
    return station


def make_link(
    source: Node,
    target: Node,
    *,
    link_type: str | None = None,
    distance_km: float = 1000.0,
    direction: str = "",
    is_active: bool = True,
    default_capacity_kbps: int = 1000,
    use_budget: bool = False,
) -> Link:
    """Create a link between two nodes.

    The link type defaults to ``GroundStationLink`` when either endpoint is a
    ground station, otherwise ``InterSatelliteLink``.
    """
    if link_type is None:
        link_type = (
            "GroundStationLink"
            if isinstance(source, GroundStation) or isinstance(target, GroundStation)
            else "InterSatelliteLink"
        )
    return Link(
        source=source,
        target=target,
        distance=distance_km,
        type=link_type,
        direction=direction,
        is_active=is_active,
        default_capacity_kbps=default_capacity_kbps,
        use_budget=use_budget,
    )


def add_interface(
    node: Node,
    name: str,
    *,
    ipv4: str | None = None,
    prefix_len: int | None = None,
    iface_type: str = "",
) -> Interface:
    """Attach an interface to a node and optionally assign an IPv4 address."""
    interface = Interface(name=name, iface_type=iface_type)
    if ipv4 is not None:
        interface.set_ipv4(ipv4, prefix_len=prefix_len)
    node.interfaces.append(interface)
    return interface
