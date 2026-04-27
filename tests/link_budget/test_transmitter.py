"""Tests for satgonetem.link_budget.transmitter."""

from satgonetem.link_budget.transmitter import calculate_transmitter_eirp


class TestCalculateTransmitterEirp:
    def test_basic_calculation(self):
        assert calculate_transmitter_eirp(10.0, 30.0, 2.0) == 38.0

    def test_no_losses(self):
        assert calculate_transmitter_eirp(5.0, 20.0, 0.0) == 25.0

    def test_negative_result(self):
        """EIRP can be negative if power is very low."""
        assert calculate_transmitter_eirp(-10.0, 5.0, 20.0) == -25.0
