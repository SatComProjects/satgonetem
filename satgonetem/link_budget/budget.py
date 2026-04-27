"""High-level link budget calculator.

Provides a single entry-point for computing the complete link budget between
a satellite and a ground station.
"""

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from satgonetem.link_budget.antenna import Antenna
from satgonetem.link_budget.conversions import linear_to_db
from satgonetem.link_budget.geometry import get_elevation_angle
from satgonetem.link_budget.modcod import ModCod
from satgonetem.link_budget.modulation import (
    calculate_link_capacity,
    calculate_theoretical_link_capacity,
)
from satgonetem.link_budget.propagation import (
    calculate_atmospheric_attenuation_dB,
    calculate_free_space_loss_db,
)
from satgonetem.link_budget.receiver import (
    calculate_carrier_to_noise_power_spectral_density_ratio,
    calculate_carrier_to_noise_ratio,
)
from satgonetem.link_budget.transmitter import calculate_transmitter_eirp


@dataclass
class LinkBudgetResult:
    """Result container for a one-way link budget computation."""

    elevation_angle: float
    """Elevation angle above the horizon in degrees."""

    free_space_loss_db: float
    """Free-space path loss in dB."""

    atmospheric_attenuation_db: float
    """Total atmospheric attenuation in dB."""

    eirp_db: float
    """Transmitter EIRP in dBW."""

    g_over_t_db: float
    """Receiver figure of merit (G/T) in dB/K."""

    cn0_dbhz: float
    """Carrier-to-noise power spectral density in dB-Hz."""

    cn_db: Optional[float]
    """Carrier-to-noise ratio in dB (optional)."""

    best_modcod: Optional[ModCod]
    """Selected MODCOD, if applicable."""

    capacity_bps: Optional[float]
    """Link capacity in bits per second."""

    capacity_kbps: Optional[int]
    """Link capacity in kilobits per second (rounded down)."""


class LinkBudgetCalculator:
    """Orchestrates link budget calculations for satellite-ground links.

    The calculator can be used directly or as a building block inside higher-level
    models such as :class:`~satgonetem.models.link.Link`.
    """

    def __init__(
        self,
        frequency_ghz_downlink: float = 19.0,
        frequency_ghz_uplink: float = 14.25,
        bandwidth_hz_downlink: float = 500e6,
        bandwidth_hz_uplink: float = 500e6,
        rolloff_factor: float = 0.25,
        unavailability_percent: float = 0.1,
        rx_tsys_k: float = 100.0,
    ):
        self.frequency_ghz_downlink = frequency_ghz_downlink
        self.frequency_ghz_uplink = frequency_ghz_uplink
        self.bandwidth_hz_downlink = bandwidth_hz_downlink
        self.bandwidth_hz_uplink = bandwidth_hz_uplink
        self.rolloff_factor = rolloff_factor
        self.unavailability_percent = unavailability_percent
        self.rx_tsys_k = rx_tsys_k

    def compute_elevation_angle(
        self,
        sat_position: dict,
        gnd_position: dict,
    ) -> float:
        """Compute the elevation angle from ground station to satellite.

        Args:
            sat_position: Dict with ``latitude``, ``longitude``, ``altitude``.
            gnd_position: Dict with ``latitude``, ``longitude``, ``altitude``.

        Returns:
            Elevation angle above the horizon in degrees.
        """
        return get_elevation_angle(
            sat_coordinates=(
                sat_position["latitude"],
                sat_position["longitude"],
                sat_position["altitude"],
            ),
            gnd_coordinates=(
                gnd_position["latitude"],
                gnd_position["longitude"],
                gnd_position["altitude"],
            ),
        )

    def compute_one_way_shannon_capacity(
        self,
        tx_node: object,
        rx_node: object,
        frequency_ghz: float,
        distance_km: float,
        elevation_angle: float,
        gs_lat: float,
        gs_lon: float,
        gs_diameter: float,
        bandwidth_hz: float,
    ) -> int:
        """Compute one-way Shannon capacity in kbps.

        This is the simplified link budget used historically by
        :class:`~satgonetem.models.link.Link`.

        Args:
            tx_node: Transmitting node (must expose ``antenna`` attributes).
            rx_node: Receiving node (must expose ``antenna`` attributes).
            frequency_ghz: Carrier frequency in GHz.
            distance_km: Slant range in kilometres.
            elevation_angle: Elevation angle in degrees.
            gs_lat: Ground station latitude in degrees.
            gs_lon: Ground station longitude in degrees.
            gs_diameter: Ground station antenna diameter in metres.
            bandwidth_hz: Signal bandwidth in Hz.

        Returns:
            Capacity in kbps.
        """
        ant = getattr(tx_node, "antenna", None)
        if ant is None:
            return 0

        try:
            ant.calculate_gain_db(frequency_ghz)
        except (AttributeError, TypeError, ValueError):
            pass

        gdb = getattr(ant, "gain_db", 0.0)
        sspa = getattr(ant, "sspa_output_power_db", 0.0)
        losses = getattr(ant, "losses_db", 0.0)
        eirp_db = calculate_transmitter_eirp(sspa, gdb, losses)

        rx_ant = getattr(rx_node, "antenna", None)
        if rx_ant is not None:
            try:
                rx_ant.calculate_gain_db(frequency_ghz)
            except (AttributeError, TypeError, ValueError):
                pass
        rx_gain_db = getattr(rx_ant, "gain_db", 0.0)
        g_over_t_db = rx_gain_db - linear_to_db(self.rx_tsys_k)

        fsl_db = calculate_free_space_loss_db(frequency_ghz, distance_km)

        att = calculate_atmospheric_attenuation_dB(
            lat_GS=gs_lat,
            lon_GS=gs_lon,
            frequency_ghz=frequency_ghz,
            elevation_angle=elevation_angle,
            unavailability=self.unavailability_percent,
            antenna_diameter=gs_diameter,
        )
        other_losses_db = att[0] if isinstance(att, (list, tuple)) else float(att)

        cn0_dbhz = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=eirp_db,
            g_over_t_db=g_over_t_db,
            free_space_loss_db=fsl_db,
            other_losses_db=other_losses_db,
        )

        cn_db = calculate_carrier_to_noise_ratio(cn0_dbhz, bandwidth_hz)
        snr_lin = 10.0 ** (cn_db / 10.0)
        capacity_bps = bandwidth_hz * math.log2(1.0 + max(0.0, snr_lin))
        return int(capacity_bps / 1000.0)

    def compute_downlink_modcod(
        self,
        sat_node: object,
        gnd_node: object,
        distance_km: float,
        gs_lat: float,
        gs_lon: float,
        gs_diameter: float,
    ) -> LinkBudgetResult:
        """Compute a full downlink budget using MODCOD selection.

        Args:
            sat_node: Satellite node (transmitter).
            gnd_node: Ground station node (receiver).
            distance_km: Slant range in kilometres.
            gs_lat: Ground station latitude in degrees.
            gs_lon: Ground station longitude in degrees.
            gs_diameter: Ground station antenna diameter in metres.

        Returns:
            A :class:`LinkBudgetResult` with the computed budget.
        """

        sat_position = getattr(sat_node, "position", None)
        gnd_position = getattr(gnd_node, "position", None)
        if sat_position is None or gnd_position is None:
            raise ValueError(
                "Both nodes must have position attributes for elevation angle calculation"
            )
        elevation_angle = self.compute_elevation_angle(
            sat_position=sat_position,
            gnd_position=gnd_position,
        )

        attenuations = calculate_atmospheric_attenuation_dB(
            lat_GS=gs_lat,
            lon_GS=gs_lon,
            frequency_ghz=self.frequency_ghz_downlink,
            elevation_angle=elevation_angle,
            unavailability=self.unavailability_percent,
            antenna_diameter=gs_diameter,
        )
        atmospheric_db = (
            attenuations[0]
            if isinstance(attenuations, (list, tuple))
            else float(attenuations)
        )

        eirp_db = getattr(getattr(sat_node, "antenna", None), "eirp_db", None)
        if eirp_db is None:
            raise ValueError("Satellite antenna EIRP is not available")

        g_over_t_db: Optional[float] = getattr(gnd_node, "g_over_t_db", None)
        if g_over_t_db is None:
            antenna: Optional[Antenna] = getattr(gnd_node, "antenna", None)
            if antenna is None:
                raise ValueError("Ground station antenna is not available")
            rx_gain_db = antenna.calculate_gain_db(self.frequency_ghz_downlink)
            g_over_t_db = rx_gain_db - linear_to_db(self.rx_tsys_k)

        fsl_db = calculate_free_space_loss_db(self.frequency_ghz_downlink, distance_km)

        cn0_dbhz = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=eirp_db,
            g_over_t_db=g_over_t_db,
            free_space_loss_db=fsl_db,
            other_losses_db=atmospheric_db,
        )

        cn_db = calculate_carrier_to_noise_ratio(cn0_dbhz, self.bandwidth_hz_downlink)

        symbol_rate_hz = self.bandwidth_hz_downlink / (1.0 + self.rolloff_factor)
        metric_db = cn0_dbhz - linear_to_db(symbol_rate_hz)

        best_modcod = ModCod.best_for_csat_n0_rs(metric_db)

        if best_modcod is not None:
            capacity_bps = calculate_link_capacity(
                bandwidth_hz=self.bandwidth_hz_downlink,
                rolloff_factor=self.rolloff_factor,
                bits_per_symbol=best_modcod.spectral_efficiency,
            )
            capacity_kbps = int(capacity_bps / 1000.0)
        else:
            capacity_bps = None
            capacity_kbps = None

        return LinkBudgetResult(
            elevation_angle=elevation_angle,
            free_space_loss_db=fsl_db,
            atmospheric_attenuation_db=atmospheric_db,
            eirp_db=eirp_db,
            g_over_t_db=g_over_t_db,
            cn0_dbhz=cn0_dbhz,
            cn_db=cn_db,
            best_modcod=best_modcod,
            capacity_bps=capacity_bps,
            capacity_kbps=capacity_kbps,
        )
