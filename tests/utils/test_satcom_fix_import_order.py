"""Regression tests for the import-order independence of ``apply_satcom_fix``.

``sat_com_trajectopy.satellites.generate_satellite`` binds ``PyOrbitalModel``
at import time. Importing ``satgonetem.utils.project_builder`` used to import
that module before the patch ran, so walker constellations were built with the
stock pyorbital model instead of the patched SGP4/GMST model.
"""

from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_python(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_late_apply_rebinds_already_imported_generate_satellite():
    """A late apply_satcom_fix call must update existing module globals."""
    _run_python(
        """
import importlib.util

import sat_com_trajectopy.orbital_models as orbital_models
import sat_com_trajectopy.satellites.generate_satellite as generate_satellite

spec = importlib.util.spec_from_file_location(
    "satcom_fix_late", "satgonetem/utils/satcom_fix.py"
)
satcom_fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(satcom_fix)

assert generate_satellite.PyOrbitalModel is not satcom_fix.PyOrbitalModel
assert orbital_models.PyOrbitalModel is not satcom_fix.PyOrbitalModel

satcom_fix.apply_satcom_fix()

assert orbital_models.PyOrbitalModel is satcom_fix.PyOrbitalModel
assert generate_satellite.PyOrbitalModel is satcom_fix.PyOrbitalModel
"""
    )


def test_project_builder_import_applies_fix_before_builder_imports():
    """Importing project_builder directly must install the patch first."""
    _run_python(
        """
import satgonetem.utils.project_builder  # noqa: F401

import sat_com_trajectopy.orbital_models as orbital_models
import sat_com_trajectopy.satellites.generate_satellite as generate_satellite
import satgonetem.utils.satcom_fix as satcom_fix

assert orbital_models.PyOrbitalModel is satcom_fix.PyOrbitalModel
assert generate_satellite.PyOrbitalModel is satcom_fix.PyOrbitalModel
"""
    )


def test_walker_constellation_uses_patched_orbital_model():
    """Satellites produced by the walker factory must use the patched model."""
    import sat_com_trajectopy.satellites.generate_satellite as generate_satellite

    import satgonetem.utils.satcom_fix as satcom_fix

    from sat_com_application.time_managers import SimulationTimeManager
    from sat_com_constellation.models import (
        WalkerConstellation,
        WalkerConstellationProperty,
    )

    constellation = WalkerConstellation(
        WalkerConstellationProperty(
            identifier="import-order-regression",
            amount_of_orbit_plane=2,
            amount_of_satellite_per_orbit_plane=2,
            inclination=53.0,
            phase_difference_between_satellites=True,
            mean_revolution_per_day=15.06,
        )
    )
    time_manager = SimulationTimeManager(
        datetime.datetime(2026, 1, 1),
        datetime.datetime(2026, 1, 2),
    )

    satellites = generate_satellite.generate_satellites_from_walker_constellation(
        constellation, time_manager
    )

    assert len(satellites) == 4
    for satellite in satellites:
        assert type(satellite.get_movement_model()) is satcom_fix.PyOrbitalModel
