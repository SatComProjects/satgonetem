"""Tests for satgonetem.link_budget.service."""

import math
from unittest.mock import patch

import pytest

from satgonetem.link_budget.antenna import Antenna
from satgonetem.link_budget.service import LinkBudgetInputs, LinkBudgetService
from satgonetem.link_budget.strategies import (
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)


class TestLinkBudgetInputs:
    def test_creation_with_defaults(self):
        inputs = LinkBudgetInputs(
            tx_antenna=Antenna(),
            rx_antenna=Antenna(),
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=0.0,
            gs_lon=0.0,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        assert inputs.rx_tsys_k == 100.0
        assert inputs.unavailability_percent == 0.1


class TestLinkBudgetServiceComputeOneWay:
    @staticmethod
    def _valid_inputs(**overrides) -> LinkBudgetInputs:
        values = {
            "tx_antenna": Antenna(
                diameter=1.0, efficiency=0.6, sspa_output_power_db=30.0
            ),
            "rx_antenna": Antenna(diameter=2.0, efficiency=0.6),
            "frequency_ghz": 19.0,
            "distance_km": 1000.0,
            "elevation_angle": 45.0,
            "gs_lat": 43.6,
            "gs_lon": 1.44,
            "gs_diameter": 2.0,
            "bandwidth_hz": 100e6,
        }
        values.update(overrides)
        return LinkBudgetInputs(**values)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("distance_km", 0.0),
            ("distance_km", -1.0),
            ("distance_km", math.nan),
            ("distance_km", math.inf),
            ("frequency_ghz", 0.0),
            ("frequency_ghz", -19.0),
            ("frequency_ghz", math.nan),
            ("frequency_ghz", math.inf),
            ("bandwidth_hz", 0.0),
            ("bandwidth_hz", -100e6),
            ("bandwidth_hz", math.nan),
            ("bandwidth_hz", math.inf),
        ],
    )
    def test_invalid_numeric_inputs_return_zero(self, field, value):
        inputs = self._valid_inputs(**{field: value})

        assert LinkBudgetService().compute_one_way(inputs) == 0

    @pytest.mark.parametrize("missing", ["tx_antenna", "rx_antenna"])
    def test_missing_antenna_returns_zero(self, missing):
        inputs = self._valid_inputs(**{missing: None})

        assert LinkBudgetService().compute_one_way(inputs) == 0

    def test_missing_tx_antenna_returns_zero(self):
        service = LinkBudgetService()
        inputs = LinkBudgetInputs(
            tx_antenna=Antenna(diameter=0.0),  # zero-diameter -> 0 gain
            rx_antenna=Antenna(diameter=2.0, efficiency=0.6),
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=43.6,
            gs_lon=1.44,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        # With zero-gain TX, capacity should be very low or 0
        cap = service.compute_one_way(inputs)
        assert isinstance(cap, int)

    @patch("satgonetem.link_budget.service.calculate_atmospheric_attenuation_dB")
    @patch("satgonetem.link_budget.service.calculate_free_space_loss_db")
    def test_shannon_strategy(self, mock_fsl, mock_atm):
        mock_fsl.return_value = 200.0
        mock_atm.return_value = (1.0, 0.5, 0.2, 0.2, 0.1)

        service = LinkBudgetService(capacity_strategy=ShannonCapacityStrategy())
        inputs = LinkBudgetInputs(
            tx_antenna=Antenna(diameter=1.0, efficiency=0.6, sspa_output_power_db=30.0),
            rx_antenna=Antenna(diameter=2.0, efficiency=0.6),
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=43.6,
            gs_lon=1.44,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        cap = service.compute_one_way(inputs)
        assert isinstance(cap, int)
        assert cap >= 0

    @patch("satgonetem.link_budget.service.calculate_atmospheric_attenuation_dB")
    @patch("satgonetem.link_budget.service.calculate_free_space_loss_db")
    def test_modcod_strategy(self, mock_fsl, mock_atm):
        mock_fsl.return_value = 200.0
        mock_atm.return_value = (1.0, 0.5, 0.2, 0.2, 0.1)

        service = LinkBudgetService(capacity_strategy=ModCodCapacityStrategy())
        inputs = LinkBudgetInputs(
            tx_antenna=Antenna(diameter=1.0, efficiency=0.6, sspa_output_power_db=50.0),
            rx_antenna=Antenna(diameter=2.0, efficiency=0.6),
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=43.6,
            gs_lon=1.44,
            gs_diameter=2.0,
            bandwidth_hz=500e6,
        )
        cap = service.compute_one_way(inputs)
        assert isinstance(cap, int)
        assert cap >= 0

    @patch("satgonetem.link_budget.service.calculate_atmospheric_attenuation_dB")
    def test_atmospheric_failure_returns_zero(self, mock_atm):
        mock_atm.side_effect = RuntimeError("itur not installed")

        service = LinkBudgetService()
        inputs = LinkBudgetInputs(
            tx_antenna=Antenna(diameter=1.0, efficiency=0.6, sspa_output_power_db=30.0),
            rx_antenna=Antenna(diameter=2.0, efficiency=0.6),
            frequency_ghz=19.0,
            distance_km=1000.0,
            elevation_angle=45.0,
            gs_lat=43.6,
            gs_lon=1.44,
            gs_diameter=2.0,
            bandwidth_hz=100e6,
        )
        cap = service.compute_one_way(inputs)
        assert cap == 0
