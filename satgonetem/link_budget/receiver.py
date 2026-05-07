"""Receiver-side link budget calculations."""

from satgonetem.link_budget.constants import BOLTZMANN_CONSTANT
from satgonetem.link_budget.conversions import db_to_linear, linear_to_db


def calculate_carrier_to_noise_power_spectral_density_ratio(
    eirp_db: float,
    g_over_t_db: float,
    free_space_loss_db: float,
    other_losses_db: float,
) -> float:
    """Calculate :math:`C/N_0` in dB-Hz.

    Args:
        eirp_db: Equivalent isotropic radiated power in dBW.
        g_over_t_db: Receiver figure of merit (G/T) in dB/K.
        free_space_loss_db: Free-space path loss in dB.
        other_losses_db: Additional losses (atmospheric, pointing, etc.) in dB.

    Returns:
        Carrier-to-noise power spectral density ratio in dB-Hz.
    """
    k_db = linear_to_db(BOLTZMANN_CONSTANT)
    total_losses_db = other_losses_db + free_space_loss_db
    return eirp_db + g_over_t_db - total_losses_db - k_db


def calculate_carrier_to_noise_ratio(
    c_over_n0_dbhz: float,
    bandwidth_hz: float,
) -> float:
    """Calculate :math:`C/N` in dB.

    Args:
        c_over_n0_dbhz: Carrier-to-noise power spectral density in dB-Hz.
        bandwidth_hz: Noise bandwidth in Hz.

    Returns:
        Carrier-to-noise ratio in dB.
    """
    bandwidth_dbhz = linear_to_db(bandwidth_hz)
    return c_over_n0_dbhz - bandwidth_dbhz


def noise_temperature(
    noise_figure_db: float,
    reference_temperature: float = 290.0,
) -> float:
    """Calculate receiver noise temperature from noise figure.

    Args:
        noise_figure_db: Noise figure in dB.
        reference_temperature: Reference temperature in Kelvin (default 290 K).

    Returns:
        Noise temperature in Kelvin.
    """
    noise_factor = db_to_linear(noise_figure_db)
    return reference_temperature * (noise_factor - 1.0)
