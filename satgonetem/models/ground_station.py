"""
GroundStation - terrestrial gateway node in the emulated constellation.

Extends Node with ground station specific state and behaviour:
  - Position synchronisation from sat_com_model.
  - IPv4/IPv6 address assignment for GSL interfaces and loopbacks.
  - Traffic model management.
  - /etc/hosts population inside the container.

Cross-cutting concerns are inherited via mixins:
  - QoSCapableMixin: TBF global QoS initialisation.
"""

from __future__ import annotations

import logging

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.mixins.qos_mixin import QoSCapableMixin
from satgonetem.utils.ip_utils import IPUtils

from sat_com_model.models import GroundStation as SatComGroundStation


class GroundStation(Node, QoSCapableMixin):
    """
    Represents a ground station node in the emulated constellation.

    Attributes:
        antenna (Antenna): Ground station dish model (2.86 m diameter, 0.6 efficiency).
        city (str | None): Human-readable city name for display purposes.
        traffic_models (list): Traffic model objects attached to this ground station.
        satcom_object (SatComGroundStation | None): Corresponding sat_com_model object.
    """

    def __init__(self, name: str) -> None:
        """
        Initialise a GroundStation node.

        Args:
            name: Node identifier (e.g. 'Gnd0'). The numeric suffix is used as
                the node ID.
        """
        super().__init__(name)

        self.city: str | None = None
        self.traffic_models: list = []

        self.satcom_object: SatComGroundStation

    def sync_position_from_satcom(self) -> None:
        """
        Update the ground station's geographic position from the sat_com_model object.

        Raises:
            ValueError: If satcom_object has not been assigned.

        Returns:
            None
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set for ground station " + self.name)

        lat, lon, alt = (
            self.satcom_object.spatial_position.to_latitude_longitude_altitude()
        )
        if lat is None or lon is None or alt is None:
            logging.warning(
                "Ground station %s has incomplete position data in sat_com_model, skipping sync.",
                self.name,
            )
            return
        self.position["latitude"] = lat
        self.position["longitude"] = lon
        self.position["altitude"] = alt / 1000

    @staticmethod
    def _set_ip_addresses_to_ground_stations(
        ground_stations: list[GroundStation], version: int
    ) -> None:
        """
        Assign IPv4 or IPv6 addresses to all ground stations in a list.

        Args:
            ground_stations: List of GroundStation objects to configure.
            version: IP version. 4 for IPv4, 6 for IPv6.

        Returns:
            None
        """
        if version == 4:
            get_addr = IPUtils.get_ipv4_address
            set_iface = lambda iface, ip: iface.set_ip(ip)
        else:
            get_addr = IPUtils.get_ipv6_address
            set_iface = lambda iface, ip: iface.set_ipv6(ip)

        for gs in ground_stations:
            gnd_id = gs.id
            for interface in gs.get_interfaces():
                parts = interface.name.split(".")
                if len(parts) < 2:
                    logging.warning(
                        "Unexpected interface name format '%s' for ground station %s, skipping.",
                        interface.name,
                        gs.name,
                    )
                    continue
                sat_id = int(parts[1])

                gnd_ip, sat_ip = get_addr(
                    node=gnd_id, peer=sat_id, type=gs.type, loopback=False
                )

                if not interface or not interface.peer:
                    logging.error(
                        "Ground station %s interface %s has no peer set.",
                        gs.name,
                        interface.name,
                    )
                    continue
                set_iface(interface, gnd_ip)
                set_iface(interface.peer, sat_ip)

            loopback_ip = get_addr(
                node=gnd_id, peer=gnd_id, type=gs.type, loopback=True
            )
            set_iface(gs.loopback, loopback_ip)

    @staticmethod
    def set_ipv4_addresses_to_ground_stations(
        ground_stations: list[GroundStation],
    ) -> None:
        """
        Assign IPv4 addresses to all ground stations in a list.

        Args:
            ground_stations: List of GroundStation objects.

        Returns:
            None
        """
        GroundStation._set_ip_addresses_to_ground_stations(ground_stations, version=4)

    @staticmethod
    def set_ipv6_addresses_to_ground_stations(
        ground_stations: list[GroundStation],
    ) -> None:
        """
        Assign IPv6 addresses to all ground stations in a list.

        Args:
            ground_stations: List of GroundStation objects.

        Returns:
            None
        """
        GroundStation._set_ip_addresses_to_ground_stations(ground_stations, version=6)

    def add_traffic(self, traffic_model) -> None:
        """
        Attach one or more traffic models to this ground station.

        Args:
            traffic_model: A single traffic model object or a list of them.

        Returns:
            None
        """
        if not isinstance(traffic_model, list):
            traffic_model = [traffic_model]
        self.traffic_models.extend(traffic_model)

    def add_hosts(self, hosts: list) -> None:
        """
        Append host entries to /etc/hosts inside the container.

        Each host's loopback IPv4 address is mapped to its lowercase name.

        Args:
            hosts: List of Node objects whose loopback IPs to register.

        Returns:
            None
        """
        for host in hosts:
            remote_ip = host.loopback.ipv4
            remote_name = host.name.lower()
            command = f"sh -c \"echo '{remote_ip} {remote_name}' >> /etc/hosts\""
            logging.info("Adding host %s to %s: %s", host, self.name, command)
            self.execute_command(command)
