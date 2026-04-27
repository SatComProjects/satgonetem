"""Antenna model for link budget calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass

from satgonetem.link_budget.constants import SPEED_OF_LIGHT
from satgonetem.link_budget.conversions import linear_to_db


@dataclass
class Antenna:
    """Physical antenna parameters used in link-budget computations.

    Attributes can be supplied directly (e.g. ``gain_db``) or derived from
    mechanical properties (``diameter`` + ``efficiency``) via
    :meth:`calculate_gain_db`.
    """

    diameter: float = 0.0
    """Antenna diameter in metres."""

    efficiency: float = 0.6
    """Aperture efficiency, 0 … 1."""

    sspa_output_power_db: float = 0.0
    """SSPA output power in dBW."""

    losses_db: float = 0.0
    """Transmit / receive losses in dB."""

    eirp_db: float | None = None
    """Optional pre-computed EIRP in dBW.  When *None* it is derived from
    ``sspa_output_power_db + gain_db - losses_db``."""

    gain_db: float | None = None
    """Optional pre-computed gain in dBi.  When *None* it is derived from
    ``diameter`` and ``efficiency`` at the requested frequency."""

    def calculate_gain_db(self, frequency_ghz: float) -> float:
        """Return the antenna gain in dBi.

        If :attr:`gain_db` was explicitly set, that cached value is returned.
        Otherwise the gain is computed from the physical aperture.

        Args:
            frequency_ghz: Carrier frequency in GHz.

        Returns:
            Gain in dBi.
        """
        if self.gain_db is not None:
            return self.gain_db

        frequency_hz = frequency_ghz * 1e9
        wavelength = SPEED_OF_LIGHT / frequency_hz
        gain_linear = (math.pi * self.diameter / wavelength) ** 2 * self.efficiency
        return linear_to_db(gain_linear)

    def get_eirp_db(self, frequency_ghz: float) -> float:
        """Return the Effective Isotropic Radiated Power in dBW.

        Uses the cached :attr:`eirp_db` when available, otherwise computes it
        from ``sspa + gain - losses``.

        Args:
            frequency_ghz: Carrier frequency in GHz (used to compute gain
                when :attr:`gain_db` is not set).

        Returns:
            EIRP in dBW.
        """
        if self.eirp_db is not None:
            return self.eirp_db

        gain = self.calculate_gain_db(frequency_ghz)
        return self.sspa_output_power_db + gain - self.losses_db
