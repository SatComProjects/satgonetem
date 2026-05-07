"""Capacity-calculation strategies for link-budget analysis.

This module implements the **Strategy** pattern so that different capacity
models (Shannon limit, DVB-S2X MODCOD, etc.) can be plugged into
:class:`~satgonetem.link_budget.service.LinkBudgetService` without modifying
existing code.
"""

from __future__ import annotations

import math
from typing import Protocol

from satgonetem.link_budget.conversions import db_to_linear, linear_to_db
from satgonetem.link_budget.modcod import ModCod
from satgonetem.link_budget.modulation import calculate_link_capacity
from satgonetem.link_budget.receiver import calculate_carrier_to_noise_ratio


class CapacityStrategy(Protocol):
    """Protocol for capacity-calculation strategies."""

    def compute_capacity(self, cn0_dbhz: float, bandwidth_hz: float) -> float:
        """Return capacity in bits per second.

        Args:
            cn0_dbhz: Carrier-to-noise power spectral density in dB-Hz.
            bandwidth_hz: Signal bandwidth in Hz.

        Returns:
            Capacity in bps.
        """
        ...


class ShannonCapacityStrategy:
    """Shannon theoretical capacity: ``C = B · log₂(1 + SNR)``."""

    def compute_capacity(self, cn0_dbhz: float, bandwidth_hz: float) -> float:
        """Compute Shannon capacity in bps.

        Args:
            cn0_dbhz: Carrier-to-noise power spectral density in dB-Hz.
            bandwidth_hz: Signal bandwidth in Hz.

        Returns:
            Theoretical capacity in bits per second.
        """
        # C/N [dB] = C/N0 [dB-Hz] - 10·log10(B)
        cn_db = calculate_carrier_to_noise_ratio(cn0_dbhz, bandwidth_hz)
        snr_linear = db_to_linear(cn_db)
        return bandwidth_hz * math.log2(1.0 + max(0.0, snr_linear))


class ModCodCapacityStrategy:
    r"""DVB-S2X MODCOD-based capacity.

    Selects the best MODCOD that fits the available
    :math:`C_{sat}/(N_0 \cdot R_s)` and returns the corresponding capacity.
    """

    def __init__(self, rolloff_factor: float = 0.25):
        self.rolloff_factor = rolloff_factor

    def compute_capacity(self, cn0_dbhz: float, bandwidth_hz: float) -> float:
        """Compute MODCOD-based capacity in bps.

        Args:
            cn0_dbhz: Carrier-to-noise power spectral density in dB-Hz.
            bandwidth_hz: Signal bandwidth in Hz.

        Returns:
            Capacity in bits per second.  Raises :exc:`RuntimeError` when no
            MODCOD can close the link.
        """
        symbol_rate_hz = bandwidth_hz / (1.0 + self.rolloff_factor)
        metric_db = cn0_dbhz - linear_to_db(symbol_rate_hz)

        best_modcod = ModCod.best_for_csat_n0_rs(metric_db)
        if best_modcod is None:
            raise RuntimeError(f"No suitable ModCod for metric {metric_db:.2f} dB")

        return calculate_link_capacity(
            bandwidth_hz=bandwidth_hz,
            rolloff_factor=self.rolloff_factor,
            bits_per_symbol=best_modcod.spectral_efficiency,
        )
