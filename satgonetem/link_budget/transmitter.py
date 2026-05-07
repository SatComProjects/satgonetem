"""Transmitter-side link budget calculations."""


def calculate_transmitter_eirp(
    sspa_output_power_db: float,
    antenna_gain_db: float,
    tx_losses_db: float,
) -> float:
    """Calculate the Effective Isotropic Radiated Power (EIRP) in dBW.

    Args:
        sspa_output_power_db: SSPA output power in dBW.
        antenna_gain_db: Antenna gain in dBi.
        tx_losses_db: Transmitter losses in dB.

    Returns:
        EIRP in dBW.
    """
    return sspa_output_power_db + antenna_gain_db - tx_losses_db
