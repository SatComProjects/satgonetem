"""Tests for satgonetem.link_budget.receiver."""


import pytest

from satgonetem.link_budget.constants import BOLTZMANN_CONSTANT
from satgonetem.link_budget.conversions import linear_to_db
from satgonetem.link_budget.receiver import (
    calculate_carrier_to_noise_power_spectral_density_ratio,
    calculate_carrier_to_noise_ratio,
    noise_temperature,
)


class TestCalculateCarrierToNoisePowerSpectralDensityRatio:
    def test_basic_calculation(self):
        # EIRP=40 dBW, G/T=10 dB/K, FSL=200 dB, losses=1 dB
        k_db = linear_to_db(BOLTZMANN_CONSTANT)
        expected = 40.0 + 10.0 - 201.0 - k_db
        result = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=40.0,
            g_over_t_db=10.0,
            free_space_loss_db=200.0,
            other_losses_db=1.0,
        )
        assert result == pytest.approx(expected)

    def test_zero_losses(self):
        k_db = linear_to_db(BOLTZMANN_CONSTANT)
        expected = 50.0 + 20.0 - 100.0 - k_db
        result = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=50.0,
            g_over_t_db=20.0,
            free_space_loss_db=100.0,
            other_losses_db=0.0,
        )
        assert result == pytest.approx(expected)


class TestCalculateCarrierToNoiseRatio:
    def test_basic(self):
        # C/N0 = 80 dB-Hz, BW = 1 MHz -> C/N = 80 - 60 = 20 dB
        result = calculate_carrier_to_noise_ratio(80.0, 1e6)
        assert result == pytest.approx(20.0, abs=0.1)

    def test_narrow_bandwidth(self):
        result = calculate_carrier_to_noise_ratio(80.0, 1.0)
        assert result == pytest.approx(80.0, abs=0.1)


class TestNoiseTemperature:
    def test_zero_db_noise_figure(self):
        # NF = 0 dB -> factor = 1 -> T = 290 * 0 = 0
        assert noise_temperature(0.0) == pytest.approx(0.0, abs=1e-9)

    def test_3_db_noise_figure(self):
        # NF = 3 dB -> factor = 10^(0.3) ≈ 1.995 -> T ≈ 288.6 K
        assert noise_temperature(3.0) == pytest.approx(288.6, abs=0.1)
