"""Tests for pure simulation timing and link-backend contracts."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from satgonetem.services.link_backend import LinkBackend
from satgonetem.services.mixins.link_ops import LinkOpsMixin
from satgonetem.services.simulation_timing import tick_delay
from tests.support import DummyTopology

pytestmark = pytest.mark.unit


def test_tick_delay_subtracts_elapsed_and_floors_at_ten_ms():
    assert tick_delay(5, 1.0, 0.0) == pytest.approx(5.0)
    assert tick_delay(5, 1.0, 2.5) == pytest.approx(2.5)
    assert tick_delay(5, 1.0, 10.0) == pytest.approx(0.01)


def test_link_backend_protocol_accepts_a_fake():
    backend = MagicMock(spec=LinkBackend)

    assert isinstance(backend, LinkBackend)


def test_link_ops_prefers_injected_backend_over_launcher():
    backend = MagicMock()
    launcher = MagicMock()
    host = DummyTopology(link_backend=backend, launcher=launcher)

    assert LinkOpsMixin._link_backend(host) is backend


def test_link_ops_falls_back_to_launcher():
    launcher = MagicMock()
    host = DummyTopology(launcher=launcher)

    assert LinkOpsMixin._link_backend(host) is launcher
