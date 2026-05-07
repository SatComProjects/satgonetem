"""High-level link-budget orchestrator.

:class:`LinkBudgetService` ties together geometry, propagation, transmitter,
receiver and capacity-strategy components to produce a one-way capacity
estimate.  It follows the **Dependency-Inversion** principle: callers depend on
the high-level service, not on individual `itur` calls or low-level formulae.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from satgonetem.link_budget.antenna import Antenna
from satgonetem.link_budget.conversions import linear_to_db
from satgonetem.link_budget.propagation import (
    calculate_atmospheric_attenuation_dB,
    calculate_free_space_loss_db,
)
from satgonetem.link_budget.receiver import (
    calculate_carrier_to_noise_power_spectral_density_ratio,
)
from satgonetem.link_budget.strategies import CapacityStrategy, ShannonCapacityStrategy

logger = logging.getLogger(__name__)


@dataclass
class LinkBudgetInputs:
    """Inputs required for a one-way link-budget computation."""

    tx_antenna: Antenna
    """Transmitting antenna."""

    rx_antenna: Antenna
    """Receiving antenna."""

    frequency_ghz: float
    """Carrier frequency in GHz."""

    distance_km: float
    """Slant range in kilometres."""

    elevation_angle: float
    """Elevation angle above the horizon in degrees."""

    gs_lat: float
    """Ground-station latitude in degrees."""

    gs_lon: float
    """Ground-station longitude in degrees."""

    gs_diameter: float
    """Ground-station antenna diameter in metres (used by the rain model)."""

    bandwidth_hz: float
    """Signal bandwidth in Hz."""

    rx_tsys_k: float = 100.0
    """Receiver system noise temperature in Kelvin."""

    unavailability_percent: float = 0.1
    """Time percentage of unavailability in %."""


class LinkBudgetService:
    """Orchestrates one-way link-budget calculations.

    Example::

        inputs = LinkBudgetInputs(
            tx_antenna=sat.antenna,
            rx_antenna=gs.antenna,
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=43.6,
            gs_lon=1.44,
            gs_diameter=1.2,
            bandwidth_hz=500e6,
        )
        service = LinkBudgetService(capacity_strategy=ModCodCapacityStrategy())
        capacity_kbps = service.compute_one_way(inputs)
    """

    def __init__(self, capacity_strategy: CapacityStrategy | None = None) -> None:
        self.capacity_strategy = capacity_strategy or ShannonCapacityStrategy()

    def compute_one_way(self, inputs: LinkBudgetInputs) -> int:
        """Compute one-way capacity in kbps.

        The calculation follows the classic chain:
        **EIRP → FSL + Atm → C/N₀ → Strategy → Capacity**.

        Args:
            inputs: All RF and geometric parameters for the link.

        Returns:
            Capacity in kbps (rounded down).  Returns *0* when an antenna is
            missing, when gain/EIRP computation fails, or when atmospheric
            attenuation cannot be computed.
        """
        # --- Transmitter EIRP ---
        try:
            tx_eirp_db = inputs.tx_antenna.get_eirp_db(inputs.frequency_ghz)
        except (ValueError, TypeError) as exc:
            logger.debug("TX EIRP computation failed: %s", exc)
            return 0

        # --- Receiver G/T ---
        try:
            rx_gain_db = inputs.rx_antenna.calculate_gain_db(inputs.frequency_ghz)
            g_over_t_db = rx_gain_db - linear_to_db(inputs.rx_tsys_k)
        except (ValueError, TypeError) as exc:
            logger.debug("RX G/T computation failed: %s", exc)
            return 0

        # --- Free-space loss ---
        fsl_db = calculate_free_space_loss_db(
            inputs.frequency_ghz, inputs.distance_km
        )

        # --- Atmospheric attenuation ---
        try:
            attenuations = calculate_atmospheric_attenuation_dB(
                lat_GS=inputs.gs_lat,
                lon_GS=inputs.gs_lon,
                frequency_ghz=inputs.frequency_ghz,
                elevation_angle=inputs.elevation_angle,
                unavailability=inputs.unavailability_percent,
                antenna_diameter=inputs.gs_diameter,
            )
            other_losses_db = (
                attenuations[0]
                if isinstance(attenuations, (list, tuple))
                else float(attenuations)
            )
        except Exception as exc:
            logger.warning(
                "Atmospheric attenuation failed (itur may be missing): %s", exc
            )
            return 0

        # --- C/N₀ ---
        cn0_dbhz = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=tx_eirp_db,
            g_over_t_db=g_over_t_db,
            free_space_loss_db=fsl_db,
            other_losses_db=other_losses_db,
        )

        # --- Capacity via strategy ---
        try:
            capacity_bps = self.capacity_strategy.compute_capacity(
                cn0_dbhz, inputs.bandwidth_hz
            )
        except RuntimeError as exc:
            logger.debug("Capacity strategy failed: %s", exc)
            return 0

        return int(capacity_bps / 1000.0)
