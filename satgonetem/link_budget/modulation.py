"""Modulation and capacity calculations."""

import math

from satgonetem.link_budget.conversions import db_to_linear


def calculate_link_capacity(
    bandwidth_hz: float,
    rolloff_factor: float = 0.35,
    bits_per_symbol: float = 1.98,
) -> float:
    """Calculate link capacity in bps using a given spectral efficiency.

    The occupied bandwidth relationship ``BW = Rs * (1 + rolloff)`` is used,
    therefore the symbol rate is ``Rs = BW / (1 + rolloff)``.

    Args:
        bandwidth_hz: Signal bandwidth in Hz.
        rolloff_factor: RRC roll-off factor (default 0.35).
        bits_per_symbol: Spectral efficiency in bits per symbol.

    Returns:
        Link capacity in bits per second.
    """
    symbol_rate = bandwidth_hz / (1.0 + rolloff_factor)
    return symbol_rate * bits_per_symbol


def calculate_theoretical_link_capacity(
    bandwidth_hz: float,
    cn_db: float,
) -> float:
    """Calculate the Shannon theoretical capacity in bps.

    Args:
        bandwidth_hz: Bandwidth in Hz.
        cn_db: Carrier-to-noise ratio in dB.

    Returns:
        Theoretical capacity in bits per second.
    """
    cn_linear = db_to_linear(cn_db)
    return bandwidth_hz * math.log2(1.0 + cn_linear)
