"""A minimal topology harness for mixin and service tests."""

from __future__ import annotations

from typing import Any

from tests.support.factories import make_ground_station, make_link, make_satellite


class DummyTopology:
    """Dict-backed topology host with the state most mixins expect.

    Tests may attach additional attributes directly or pass them as keyword
    arguments. The class intentionally does not implement TopologyManager
    initialization, locking, or lifecycle behavior.
    """

    def __init__(self, **attributes: Any) -> None:
        self.satellites: dict[int, Any] = {}
        self.ground_stations: dict[int, Any] = {}
        self.links: dict[int, Any] = {}
        self._active_flows: list[Any] = []
        self.routing_daemon = None
        self.hil_manager = None
        self.launcher = None
        for name, value in attributes.items():
            setattr(self, name, value)

    def add_satellite(self, name: str = "Sat0", **kwargs: Any):
        satellite = make_satellite(name, **kwargs)
        self.satellites[satellite.id] = satellite
        return satellite

    def add_ground_station(self, name: str = "Gnd0", **kwargs: Any):
        station = make_ground_station(name, **kwargs)
        self.ground_stations[station.id] = station
        return station

    def add_link(self, source, target, **kwargs: Any):
        link = make_link(source, target, **kwargs)
        self.links[len(self.links)] = link
        return link

    def get_node_by_name(self, name: str):
        for node in self.satellites.values():
            if node.name == name:
                return node
        for node in self.ground_stations.values():
            if node.name == name:
                return node
        return None
