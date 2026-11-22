"""Tests for the shared iperf3 unit conversion constants."""

from __future__ import annotations

import pytest

from satgonetem.traffic.iperf3_units import (
    BPS_TO_MBPS,
    BYTES_TO_KB,
    BYTES_TO_MB,
    USEC_TO_MS,
)

pytestmark = pytest.mark.unit


def test_unit_constants_keep_decimal_si_values() -> None:
    # iperf3 reports SI units; these must not become 1024-based binary units.
    assert BPS_TO_MBPS == 1_000_000.0
    assert BYTES_TO_MB == 1_000_000.0
    assert BYTES_TO_KB == 1_000.0
    assert USEC_TO_MS == 1_000.0


def test_unit_constants_are_floats() -> None:
    assert all(
        isinstance(value, float)
        for value in (BPS_TO_MBPS, BYTES_TO_MB, BYTES_TO_KB, USEC_TO_MS)
    )
