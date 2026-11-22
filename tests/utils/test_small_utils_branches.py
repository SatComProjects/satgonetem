"""Branch-coverage tests for small utility helpers.

These exercise the module ``__main__`` example blocks with :mod:`runpy` and the
remaining invalid-input branch in :meth:`IPUtils.summarize_ipv4_address`.
"""

from __future__ import annotations

import math
import runpy
import sys
from pathlib import Path

import pytest

import satgonetem.utils.geodesy as geodesy_module
import satgonetem.utils.node_terminal_bridge as node_terminal_bridge_module
from satgonetem.utils.ip_utils import IPUtils

pytestmark = pytest.mark.unit


def test_geodesy_module_main_prints_example_orbital_elements(capsys) -> None:
    script = Path(geodesy_module.__file__)
    runpy.run_path(str(script), run_name="__main__")

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2

    mean_motion = float(lines[0])
    assert math.isfinite(mean_motion)
    assert mean_motion > 0.0

    raan, arg_perigee, mean_anomaly = (float(value) for value in lines[1].split())
    assert 0.0 <= raan < 2.0 * math.pi
    assert arg_perigee == pytest.approx(0.0)
    assert 0.0 <= mean_anomaly < 2.0 * math.pi


def test_node_terminal_bridge_module_main_requires_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "argv", ["node_terminal_bridge"])
    script = Path(node_terminal_bridge_module.__file__)

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(script), run_name="__main__")

    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Usage:" in captured.err
    assert "node_terminal_bridge <server> <project_id> <node_name>" in captured.err


def test_summarize_ipv4_address_rejects_non_numeric_octets() -> None:
    with pytest.raises(ValueError, match="Invalid IPv4 address"):
        IPUtils.summarize_ipv4_address("10.0.0.x", 24)
