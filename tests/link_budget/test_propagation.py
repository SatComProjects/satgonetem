"""Tests for satgonetem.link_budget.propagation."""

import pytest

from satgonetem.link_budget.propagation import (
    calculate_atmospheric_attenuation_dB,
    calculate_free_space_loss_db,
)


class TestCalculateFreeSpaceLossDb:
    def test_increases_with_distance(self):
        fsl_100 = calculate_free_space_loss_db(14.0, 100.0)
        fsl_200 = calculate_free_space_loss_db(14.0, 200.0)
        assert fsl_200 > fsl_100

    def test_increases_with_frequency(self):
        fsl_low = calculate_free_space_loss_db(10.0, 1000.0)
        fsl_high = calculate_free_space_loss_db(20.0, 1000.0)
        assert fsl_high > fsl_low

    def test_known_value(self):
        # FSPL at 1 GHz, 1 km should be ~92.45 dB
        fsl = calculate_free_space_loss_db(1.0, 1.0)
        assert fsl == pytest.approx(92.45, abs=0.1)


class TestCalculateAtmosphericAttenuationDb:
    def test_input_validation_unavailability(self):
        with pytest.raises(ValueError, match="Unavailability"):
            calculate_atmospheric_attenuation_dB(
                lat_GS=0.0,
                lon_GS=0.0,
                frequency_ghz=14.0,
                elevation_angle=45.0,
                unavailability=-1.0,
                antenna_diameter=2.0,
            )

    def test_input_validation_elevation(self):
        with pytest.raises(ValueError, match="Elevation angle"):
            calculate_atmospheric_attenuation_dB(
                lat_GS=0.0,
                lon_GS=0.0,
                frequency_ghz=14.0,
                elevation_angle=95.0,
                unavailability=0.1,
                antenna_diameter=2.0,
            )

    def test_input_validation_frequency(self):
        with pytest.raises(ValueError, match="Frequency"):
            calculate_atmospheric_attenuation_dB(
                lat_GS=0.0,
                lon_GS=0.0,
                frequency_ghz=2000.0,
                elevation_angle=45.0,
                unavailability=0.1,
                antenna_diameter=2.0,
            )

    def test_returns_tuple_when_itur_available(self):
        pytest.importorskip("itur")
        result = calculate_atmospheric_attenuation_dB(
            lat_GS=43.6,
            lon_GS=1.44,
            frequency_ghz=19.0,
            elevation_angle=30.0,
            unavailability=0.1,
            antenna_diameter=1.2,
        )
        assert isinstance(result, tuple)
        assert len(result) == 5
        total, gaseous, cloud, rain, scintillation = result
        assert total >= 0.0
        assert isinstance(total, float)
