"""
Satellite - LEO/MEO/GEO satellite node in the emulator.

Extends Node with satellite-specific state and behaviour:
  - Position synchronisation from sat_com_model.
  - IPv4 address assignment for ISL interfaces and loopbacks.
  - Network statistics logging (net_logger).
"""

from __future__ import annotations

import logging

from sat_com_model.models import Satellite as SatComSatellite

from satgonetem.models.mixins.qos_mixin import QoSCapableMixin
from satgonetem.models.node import Node
from satgonetem.utils.net_logger import (
    NET_LOGGER_DIR,
    NET_LOGGER_INTERVAL_S,
    build_start_command,
    build_stop_command,
    net_logger_path,
    net_logger_token,
)


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

    def start_net_logger(
        self,
        project_name: str,
        path: str = NET_LOGGER_DIR,
        interval_s: float = NET_LOGGER_INTERVAL_S,
    ) -> None:
        """
        Start the link-usage sampler for this satellite.

        The sampler writes one JSON object per sample to
        ``<path>/<project_name>.<node_name>``. Each object maps every
        ``eth*`` interface to a single bits-per-second value,
        ``max(rx_bps, tx_bps)``.

        Args:
            project_name: GoNetEm project name used in the monitor filename.
            path: Directory holding the monitor files. ``/tmp`` is shared
                between the host and every node container.
            interval_s: Sampling interval in seconds.

        Returns:
            None

        Raises:
            ValueError: If ``project_name``, ``path`` or ``interval_s`` is
                invalid.
        """
        destination = net_logger_path(project_name, self.name, path)
        token = net_logger_token(destination)
        command = build_start_command(destination, interval_s, token)
        self.execute_command(command, detach=True)

    def stop_net_logger(
        self,
        project_name: str,
        path: str = NET_LOGGER_DIR,
    ) -> None:
        """
        Stop this satellite's link-usage sampler and remove its samples.

        Args:
            project_name: GoNetEm project name used in the monitor filename.
            path: Directory holding the monitor files.

        Returns:
            None
        """
        destination = net_logger_path(project_name, self.name, path)
        token = net_logger_token(destination)
        self.execute_command(build_stop_command(destination, token), detach=False)
