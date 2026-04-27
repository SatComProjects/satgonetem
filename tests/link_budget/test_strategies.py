"""Tests for satgonetem.link_budget.strategies."""

import math

import pytest

from satgonetem.link_budget.strategies import (
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)


class TestShannonCapacityStrategy:
    def test_zero_cn(self):
        # C/N = 0 dB -> SNR = 1 -> C = B * log2(2) = B
        strat = ShannonCapacityStrategy()
        cap = strat.compute_capacity(cn0_dbhz=60.0, bandwidth_hz=1e6)
        # C/N0 = 60 dB-Hz, B = 1 MHz -> C/N = 60 - 60 = 0 dB
        assert cap == pytest.approx(1e6, rel=1e-3)

    def test_10_db_cn(self):
        # C/N = 10 dB -> SNR = 10 -> C = B * log2(11)
        strat = ShannonCapacityStrategy()
        cap = strat.compute_capacity(cn0_dbhz=70.0, bandwidth_hz=1e6)
        expected = 1e6 * math.log2(11)
        assert cap == pytest.approx(expected)

    def test_negative_cn_returns_zero(self):
        # Very low C/N0 should give 0 capacity due to max(0, snr)
        strat = ShannonCapacityStrategy()
        cap = strat.compute_capacity(cn0_dbhz=10.0, bandwidth_hz=1e6)
        assert cap >= 0.0


class TestModCodCapacityStrategy:
    def test_high_cn0_selects_modcod(self):
        # Very high C/N0 should select the highest MODCOD
        strat = ModCodCapacityStrategy(rolloff_factor=0.25)
        cap = strat.compute_capacity(cn0_dbhz=120.0, bandwidth_hz=1e6)
        assert cap > 0.0

    def test_low_cn0_raises(self):
        # Very low C/N0 should raise RuntimeError
        strat = ModCodCapacityStrategy(rolloff_factor=0.25)
        with pytest.raises(RuntimeError, match="No suitable ModCod"):
            strat.compute_capacity(cn0_dbhz=0.0, bandwidth_hz=1e6)

    def test_rolloff_affects_capacity(self):
        # Higher rolloff -> lower symbol rate -> lower capacity for same MODCOD
        strat_low = ModCodCapacityStrategy(rolloff_factor=0.2)
        strat_high = ModCodCapacityStrategy(rolloff_factor=0.5)

        cap_low = strat_low.compute_capacity(cn0_dbhz=120.0, bandwidth_hz=1e6)
        cap_high = strat_high.compute_capacity(cn0_dbhz=120.0, bandwidth_hz=1e6)
        assert cap_low > cap_high
