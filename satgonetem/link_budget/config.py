"""Configuration classes for link budget and antenna parameters."""

from __future__ import annotations

from dataclasses import dataclass

from satgonetem.link_budget.antenna import Antenna


@dataclass
class LinkBudgetConfig:
    """RF link budget configuration applied to satellite-ground links.

    Attributes:
        downlink_freq_ghz: Carrier frequency for the downlink (satellite → ground)
            in GHz.
        uplink_freq_ghz: Carrier frequency for the uplink (ground → satellite)
            in GHz.
        bandwidth_hz_downlink: Signal bandwidth for the downlink in Hz.
        bandwidth_hz_uplink: Signal bandwidth for the uplink in Hz.
    """

    downlink_freq_ghz: float = 19.0
    uplink_freq_ghz: float = 14.25
    bandwidth_hz_downlink: float = 500e6
    bandwidth_hz_uplink: float = 500e6


@dataclass
class AntennaConfig:
    """Antenna parameters used to build :class:`~satgonetem.link_budget.antenna.Antenna`
    instances.

    Attributes mirror those of :class:`~satgonetem.link_budget.antenna.Antenna`.
    """

    diameter: float = 0.0
    efficiency: float = 0.6
    sspa_output_power_db: float = 0.0
    losses_db: float = 0.0
    eirp_db: float | None = None
    gain_db: float | None = None

    def to_antenna(self) -> Antenna:
        """Create an :class:`Antenna` from this configuration."""
        return Antenna(
            diameter=self.diameter,
            efficiency=self.efficiency,
            sspa_output_power_db=self.sspa_output_power_db,
            losses_db=self.losses_db,
            eirp_db=self.eirp_db,
            gain_db=self.gain_db,
        )
