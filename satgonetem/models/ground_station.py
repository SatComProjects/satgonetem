"""
GroundStation - terrestrial gateway node in the emulated constellation.

Extends Node with ground station specific state and behaviour:
  - Position synchronisation from sat_com_model.
  - IPv4 address assignment for GSL interfaces and loopbacks.
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

        self.type = "GroundStation"

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
