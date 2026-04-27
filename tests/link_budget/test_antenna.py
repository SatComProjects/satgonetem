"""Tests for satgonetem.link_budget.antenna."""

import math

import pytest

from satgonetem.link_budget.antenna import Antenna
from satgonetem.link_budget.constants import SPEED_OF_LIGHT


def _expected_gain_db(diameter: float, frequency_ghz: float, efficiency: float) -> float:
    """Compute expected parabolic antenna gain analytically."""
    wavelength = SPEED_OF_LIGHT / (frequency_ghz * 1e9)
    gain_linear = (math.pi * diameter / wavelength) ** 2 * efficiency
    return 10.0 * math.log10(gain_linear)


class TestAntennaCalculateGainDb:
    def test_1m_dish_12ghz_ideal_efficiency(self):
        """1 m dish at 12 GHz with 100% efficiency: ~42.0 dBi."""
        ant = Antenna(diameter=1.0, efficiency=1.0)
        expected = _expected_gain_db(1.0, 12.0, 1.0)
        assert ant.calculate_gain_db(12.0) == pytest.approx(expected, abs=0.01)
        assert ant.calculate_gain_db(12.0) == pytest.approx(42.0, abs=0.2)

    def test_3m_dish_12ghz_typical_efficiency(self):
        """3 m dish at 12 GHz with 60% efficiency: ~9.5 dB above 1m ideal."""
        ant = Antenna(diameter=3.0, efficiency=0.6)
        expected = _expected_gain_db(3.0, 12.0, 0.6)
        assert ant.calculate_gain_db(12.0) == pytest.approx(expected, abs=0.01)

    def test_gain_scales_with_diameter_squared(self):
        """Doubling the diameter increases gain by ~6 dB."""
        ant1 = Antenna(diameter=1.0, efficiency=0.6)
        ant2 = Antenna(diameter=2.0, efficiency=0.6)
        gain1 = ant1.calculate_gain_db(10.0)
        gain2 = ant2.calculate_gain_db(10.0)
        assert gain2 - gain1 == pytest.approx(6.0 * math.log10(4.0) / math.log10(4.0), abs=0.05)

    def test_gain_scales_with_frequency_squared(self):
        """Doubling the frequency increases gain by ~6 dB."""
        ant = Antenna(diameter=1.0, efficiency=0.6)
        gain_low = ant.calculate_gain_db(10.0)
        gain_high = ant.calculate_gain_db(20.0)
        assert gain_high - gain_low == pytest.approx(6.02, abs=0.05)

    def test_cached_gain_db_overrides_formula(self):
        """When gain_db is pre-set, formula is not used."""
        ant = Antenna(diameter=1.0, efficiency=1.0, gain_db=30.0)
        assert ant.calculate_gain_db(12.0) == 30.0

    def test_efficiency_scales_gain_linearly_in_db(self):
        """Halving efficiency reduces gain by 10*log10(2) ~ 3.01 dB."""
        ant_full = Antenna(diameter=1.0, efficiency=1.0)
        ant_half = Antenna(diameter=1.0, efficiency=0.5)
        diff = ant_full.calculate_gain_db(12.0) - ant_half.calculate_gain_db(12.0)
        assert diff == pytest.approx(10.0 * math.log10(2.0), abs=0.01)


class TestAntennaGetEirpDb:
    def test_eirp_from_components(self):
        """EIRP = sspa_output_power + gain - losses."""
        ant = Antenna(diameter=1.0, efficiency=1.0, sspa_output_power_db=10.0, losses_db=2.0)
        expected = 10.0 + ant.calculate_gain_db(12.0) - 2.0
        assert ant.get_eirp_db(12.0) == pytest.approx(expected, abs=0.01)

    def test_cached_eirp_db_overrides_computation(self):
        """When eirp_db is pre-set, no computation is performed."""
        ant = Antenna(eirp_db=50.0)
        assert ant.get_eirp_db(12.0) == 50.0
