"""Tests for satgonetem.link_budget.conversions."""

import math

import pytest

from satgonetem.link_budget.conversions import db_to_linear, linear_to_db


class TestDbToLinear:
    def test_zero_db(self):
        assert db_to_linear(0.0) == pytest.approx(1.0)

    def test_positive_db(self):
        assert db_to_linear(10.0) == pytest.approx(10.0)

    def test_negative_db(self):
        assert db_to_linear(-10.0) == pytest.approx(0.1)


class TestLinearToDb:
    def test_one(self):
        assert linear_to_db(1.0) == pytest.approx(0.0)

    def test_ten(self):
        assert linear_to_db(10.0) == pytest.approx(10.0)

    def test_raises_on_zero(self):
        with pytest.raises(ValueError, match="greater than 0"):
            linear_to_db(0.0)

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="greater than 0"):
            linear_to_db(-1.0)

    def test_round_trip(self):
        for value in [0.001, 0.1, 1.0, 10.0, 1000.0]:
            assert db_to_linear(linear_to_db(value)) == pytest.approx(value)
