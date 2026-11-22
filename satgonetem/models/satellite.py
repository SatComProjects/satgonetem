"""
Satellite - LEO/MEO/GEO satellite node in the emulator.

Extends Node with satellite-specific state and behaviour:
  - Position synchronisation from sat_com_model.
  - IPv4 address assignment for ISL interfaces and loopbacks.
  - Network statistics logging (net_logger).
"""

from __future__ import annotations

import logging
import shlex

from sat_com_model.models import Satellite as SatComSatellite

from satgonetem.models.mixins.qos_mixin import QoSCapableMixin
from satgonetem.models.node import Node


class Satellite(Node, QoSCapableMixin):
    """
    Represents a satellite node in the emulated constellation.

    Attributes:
        antenna (Antenna): Satellite antenna model with downlink EIRP.
        handover (bool): True if a handover occurred in the last timestep.
        default_qos_is_on (bool): Whether the default TBF queuing is active.
        program_specific_qos_is_on (bool): Whether program-specific HTB is active.
        qos_configuration_count (int): Number of times QoS has been reconfigured.
        satcom_object (SatComSatellite | None): Corresponding sat_com_model object.
        shell: Legacy shell reference (unused).
    """

    def __init__(self, name: str = "") -> None:
        """
        Initialise a Satellite node.

        Args:
            name: Node identifier (e.g. 'Sat0'). The numeric suffix is used as
                the node ID.
        """
        super().__init__(name)

        self.shell = None

        self.default_qos_is_on: bool = True
        self.program_specific_qos_is_on: bool = False
        self.qos_configuration_count: int = 0

        self.satcom_object: SatComSatellite | None = None

        self.type = "Satellite"

        ## Routing
        self.addressable = False

    def is_addressable(self) -> bool:
        """
        Get addressable flag
        """

        return self.addressable

    def set_addressable(self, status: bool) -> None:
        """
        Get addressable flag
        """
        self.addressable = status

    def sync_position_from_satcom(self, altitude_unit: str = "m") -> None:
        """
        Update the satellite's geographic position from the sat_com_model object.

        Args:
            altitude_unit: Unit of the altitude reported by
                ``to_latitude_longitude_altitude``. ``"m"`` (default) matches
                the movement-model contract and is converted to kilometres;
                ``"km"`` is stored as-is.  The unit must be explicit because a
                magnitude heuristic cannot distinguish a 50 km altitude in
                metres from a 50,000 km altitude in kilometres.

        Raises:
            ValueError: If satcom_object has not been assigned or
                altitude_unit is not ``"m"`` or ``"km"``.

        Returns:
            None
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set for satellite " + self.name)

        unit = altitude_unit.lower()
        if unit not in ("m", "km"):
            raise ValueError(
                f"Unsupported altitude_unit {altitude_unit!r}; expected 'm' or 'km'"
            )

        lat, lon, alt = (
            self.satcom_object.spatial_position.to_latitude_longitude_altitude()
        )
        if lat is None or lon is None or alt is None:
            logging.warning(
                "Satellite %s: could not retrieve position from sat_com_model",
                self.name,
            )
            return
        self.position["latitude"] = lat
        self.position["longitude"] = lon
        self.position["altitude"] = alt / 1000.0 if unit == "m" else float(alt)

    def start_net_logger(self, path: str) -> None:
        """
        Start net_logger on every interface of the satellite.

        Args:
            path: Host directory path for log files.

        Returns:
            None
        """
        for interface in self.interfaces:
            iname = interface.get_iname()
            destination = path + f"/{self.name}_{iname}.log"
            command = (
                f"net_logger --interface {shlex.quote(iname)} "
                f"--log {shlex.quote(destination)} --interval 1"
            )
            self.execute_command(command, detach=True)
