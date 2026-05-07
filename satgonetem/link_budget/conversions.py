"""Decibel / linear scale conversions."""

import math


def db_to_linear(dB: float) -> float:
    """Convert a decibel value to linear scale.

    Args:
        dB: Value in decibels.

    Returns:
        The equivalent linear value.
    """
    return 10.0 ** (dB / 10.0)


def linear_to_db(linear: float) -> float:
    """Convert a linear value to decibels.

    Args:
        linear: Positive linear value.

    Returns:
        The equivalent decibel value.

    Raises:
        ValueError: If *linear* is not strictly positive.
    """
    if linear <= 0.0:
        raise ValueError("Linear value must be greater than 0")
    return 10.0 * math.log10(linear)
