"""Tests for satgonetem.link_budget.modulation."""

import math

import pytest

from satgonetem.link_budget.modulation import (
    calculate_link_capacity,
    calculate_theoretical_link_capacity,
)


class TestCalculateLinkCapacity:
    def test_rolloff_zero(self):
        # BW = 1 MHz, rolloff=0, 2 bits/symbol -> 2 Mbps
        assert calculate_link_capacity(1e6, 0.0, 2.0) == pytest.approx(2e6)

    def test_rolloff_025(self):
        # BW = 1 MHz, rolloff=0.25 -> Rs = 0.8 Msps -> 1.6 Mbps for 2 bits/symbol
        assert calculate_link_capacity(1e6, 0.25, 2.0) == pytest.approx(1.6e6)

    def test_default_parameters(self):
        result = calculate_link_capacity(1e6)
        assert result > 0.0


class TestCalculateTheoreticalLinkCapacity:
    def test_shannon_known_value(self):
        # BW = 1 MHz, C/N = 0 dB (linear = 1) -> 1 Mbps
        cap = calculate_theoretical_link_capacity(1e6, 0.0)
        assert cap == pytest.approx(1e6, rel=1e-3)

    def test_shannon_10_db(self):
        # BW = 1 MHz, C/N = 10 dB (linear = 10)
        cap = calculate_theoretical_link_capacity(1e6, 10.0)
        expected = 1e6 * math.log2(11)
        assert cap == pytest.approx(expected)
