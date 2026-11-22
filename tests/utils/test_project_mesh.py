"""Tests for mesh/constellation link construction helpers.

The module is exercised through duck-typed fakes only: no SGP4 propagation,
Astropy, Docker, root privileges, or network access.  The fakes feed the TEME
state and geodetic accessors that ``_get_orbital_elements`` consumes, so the
grouping and link-creation behaviour can be asserted deterministically.
"""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import pytest
from sat_com_model.models import InterSatelliteLinkDirection

from satgonetem.utils.project_mesh import (
    _get_orbital_elements,
    _group_satellites_by_orbital_plane,
    add_mesh_links,
)


def _teme_state(
    raan_deg: float,
    inc_deg: float,
    arg_lat_deg: float,
    radius_km: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return a circular-orbit TEME (r, v) pair with the given elements."""
    raan = math.radians(raan_deg)
    inc = math.radians(inc_deg)
    arg_lat = math.radians(arg_lat_deg)

    x_hat = (math.cos(raan), math.sin(raan), 0.0)
    y_hat = (
        -math.cos(inc) * math.sin(raan),
        math.cos(inc) * math.cos(raan),
        math.sin(inc),
    )

    r = tuple(
        radius_km * (math.cos(arg_lat) * x_hat[i] + math.sin(arg_lat) * y_hat[i])
        for i in range(3)
    )
    speed_km_s = math.sqrt(398600.4418 / radius_km)
    v = tuple(
        speed_km_s * (-math.sin(arg_lat) * x_hat[i] + math.cos(arg_lat) * y_hat[i])
        for i in range(3)
    )
    return r, v


class _FakeMovementModel:
    """Minimal SGP4-like model: TLE elements plus TEME/geodetic accessors."""

    def __init__(
        self,
        raan_deg: float,
        phase_deg: float,
        altitude_km: float,
        *,
        inc_deg: float = 53.0,
        radius_km: float | None = None,
        nodeo_deg: float | None = None,
    ) -> None:
        self.orbital_object = SimpleNamespace(
            nodeo=math.radians(nodeo_deg if nodeo_deg is not None else raan_deg),
            inclo=math.radians(inc_deg),
        )
        radius = radius_km if radius_km is not None else 6371.0 + altitude_km
        self._r, self._v = _teme_state(raan_deg, inc_deg, phase_deg, radius)
        self._altitude_m = altitude_km * 1000.0

    def get_position_earth_general_inertial(self):
        return self._r

    def get_velocity_earth_general_inertial(self):
        return self._v

    def get_longitude_latitude_altitude(self):
        return SimpleNamespace(altitude=self._altitude_m)


class _PyOrbitalModel:
    """pyorbital-like model: no ``nodeo``/``inclo``, state-vector derivation."""

    def __init__(
        self,
        raan_deg: float,
        inc_deg: float,
        phase_deg: float,
        altitude_km: float,
    ) -> None:
        self.orbital_object = SimpleNamespace(
            inclination=math.radians(inc_deg),
            right_ascension=math.radians(raan_deg),
        )
        self._r, self._v = _teme_state(
            raan_deg, inc_deg, phase_deg, 6371.0 + altitude_km
        )
        self._altitude_m = altitude_km * 1000.0

    def get_position_earth_general_inertial(self):
        return self._r

    def get_velocity_earth_general_inertial(self):
        return self._v

    def get_longitude_latitude_altitude(self):
        return SimpleNamespace(altitude=self._altitude_m)


class _FakeSatellite:
    def __init__(
        self,
        movement_model,
        *,
        raan_deg: float | None = None,
        phase_deg: float | None = None,
        altitude_km: float | None = None,
    ) -> None:
        self._movement_model = movement_model
        self.raan_deg = raan_deg
        self.phase_deg = phase_deg
        self.altitude_km = altitude_km

    def get_movement_model(self):
        return self._movement_model


def _sat(
    raan_deg: float,
    phase_deg: float = 0.0,
    altitude_km: float = 550.0,
    *,
    radius_km: float | None = None,
    nodeo_deg: float | None = None,
) -> _FakeSatellite:
    """Build a satellite whose movement model reports the requested elements."""
    model = _FakeMovementModel(
        raan_deg,
        phase_deg,
        altitude_km,
        radius_km=radius_km,
        nodeo_deg=nodeo_deg,
    )
    return _FakeSatellite(
        model,
        raan_deg=raan_deg,
        phase_deg=phase_deg,
        altitude_km=altitude_km,
    )


class _FakeSimulationManager:
    """Records link-creation calls without touching the real simulation stack."""

    def __init__(
        self,
        satellites,
        ground_stations=(),
        *,
        time_manager=None,
        fail_labels=(),
    ) -> None:
        self.time_manager = time_manager
        self._satellites = list(satellites)
        self._ground_stations = list(ground_stations)
        self.fail_labels = set(fail_labels)
        self.closest_satellite = self._satellites[0] if self._satellites else None
        self.isl_calls: list[tuple[object, object]] = []
        self.gsl_calls: list[tuple[object, object]] = []
        self.isl_links: list[SimpleNamespace] = []
        self.gsl_links: list[SimpleNamespace] = []

    def get_satellites(self):
        return list(self._satellites)

    def get_ground_stations(self):
        return list(self._ground_stations)

    def create_and_add_inter_satellite_link_connection(self, sat_a, sat_b):
        link = SimpleNamespace(inter_satellite_direction=None)
        self.isl_calls.append((sat_a, sat_b))
        self.isl_links.append(link)
        return link

    def create_and_add_ground_station_link_connection(self, satellite, ground_station):
        link = SimpleNamespace(satellite=satellite, ground_station=ground_station)
        self.gsl_calls.append((satellite, ground_station))
        self.gsl_links.append(link)
        return link

    def get_closest_satellite_to_a_ground_station(self, ground_station):
        if ground_station.label in self.fail_labels:
            raise RuntimeError("no visible satellite")
        return self.closest_satellite


def _plane_raans(planes) -> list[set[float]]:
    """Return the set of RAANs (degrees) contained in each grouped plane."""
    return [{sat.raan_deg for sat, _phase in plane} for plane in planes]


# ---------------------------------------------------------------------------
# _get_orbital_elements
# ---------------------------------------------------------------------------


def test_no_movement_model_yields_no_elements() -> None:
    assert _get_orbital_elements(_FakeSatellite(None), None) is None


def test_sgp4_elements_wrap_raan_into_range_and_convert_altitude() -> None:
    # nodeo is 725 deg; the returned RAAN must be folded into [0, 360).
    sat = _sat(
        raan_deg=5.0,
        phase_deg=20.0,
        altitude_km=600.0,
        nodeo_deg=725.0,
    )

    elements = _get_orbital_elements(sat, None)

    assert elements is not None
    raan, phase, altitude = elements
    assert raan == pytest.approx(5.0)
    assert phase == pytest.approx(20.0)
    assert altitude == pytest.approx(600.0)


def test_pyorbital_elements_are_derived_from_teme_state() -> None:
    model = _PyOrbitalModel(
        raan_deg=210.0, inc_deg=60.0, phase_deg=25.0, altitude_km=123.456
    )

    elements = _get_orbital_elements(_FakeSatellite(model), None)

    assert elements is not None
    raan, phase, altitude = elements
    assert raan == pytest.approx(210.0, abs=1e-9)
    assert phase == pytest.approx(25.0, abs=1e-9)
    # Metres are converted to kilometres, using the geodetic altitude.
    assert altitude == pytest.approx(123.456)


def test_missing_position_accessor_yields_no_elements() -> None:
    model = SimpleNamespace(orbital_object=SimpleNamespace(inclination=0.9))

    assert _get_orbital_elements(_FakeSatellite(model), None) is None


def test_missing_velocity_accessor_yields_no_elements() -> None:
    class _NoVelocityModel:
        orbital_object = SimpleNamespace(inclination=0.9)

        def get_position_earth_general_inertial(self):
            return (7000.0, 0.0, 0.0)

    assert _get_orbital_elements(_FakeSatellite(_NoVelocityModel()), None) is None


def test_zero_angular_momentum_yields_no_elements() -> None:
    class _RadialModel:
        orbital_object = SimpleNamespace(inclination=0.9)

        def get_position_earth_general_inertial(self):
            return (7000.0, 0.0, 0.0)

        def get_velocity_earth_general_inertial(self):
            # Parallel to r: h = r x v is the zero vector.
            return (7.0, 0.0, 0.0)

    assert _get_orbital_elements(_FakeSatellite(_RadialModel()), None) is None


def test_missing_geodetic_accessor_yields_no_elements() -> None:
    class _NoGeodeticModel:
        orbital_object = SimpleNamespace(inclination=0.9)

        def __init__(self) -> None:
            self._r, self._v = _teme_state(45.0, 53.0, 10.0, 6921.0)

        def get_position_earth_general_inertial(self):
            return self._r

        def get_velocity_earth_general_inertial(self):
            return self._v

    assert _get_orbital_elements(_FakeSatellite(_NoGeodeticModel()), None) is None


# ---------------------------------------------------------------------------
# _group_satellites_by_orbital_plane
# ---------------------------------------------------------------------------


def test_grouping_empty_input_returns_no_planes() -> None:
    assert _group_satellites_by_orbital_plane([], None) == []


def test_grouping_skips_satellites_without_usable_orbital_model() -> None:
    usable = _sat(10.0, 0.0)
    unusable = _FakeSatellite(None)

    planes = _group_satellites_by_orbital_plane([unusable, usable], None)

    assert _plane_raans(planes) == [{10.0}]
    assert len(planes) == 1
    ((grouped_sat, phase),) = planes[0]
    assert grouped_sat is usable
    assert phase == pytest.approx(0.0)
    assert _group_satellites_by_orbital_plane([unusable], None) == []


def test_grouping_single_satellite_returns_one_plane() -> None:
    sat = _sat(42.0, 15.0)

    planes = _group_satellites_by_orbital_plane([sat], None)

    assert _plane_raans(planes) == [{42.0}]
    assert len(planes) == 1
    ((grouped_sat, phase),) = planes[0]
    assert grouped_sat is sat
    assert phase == pytest.approx(15.0)


def test_grouping_wraps_across_zero_degrees() -> None:
    # 358/1/4 form one plane across the 0/360 seam; 180/184 form another.
    # The largest gap (184 -> 358) is where the circle is cut.
    sats = [_sat(raan) for raan in (358.0, 1.0, 4.0, 180.0, 184.0)]

    planes = _group_satellites_by_orbital_plane(sats, None)

    raans = _plane_raans(planes)
    assert raans == [{1.0, 4.0, 358.0}, {180.0, 184.0}]
    assert [len(plane) for plane in planes] == [3, 2]


def test_grouping_raan_threshold_is_strict() -> None:
    # A 5.0 deg gap does not join; 4.9 deg does.
    separated = [_sat(raan) for raan in (0.0, 5.0, 10.1)]
    joined = [_sat(raan) for raan in (0.0, 4.9, 10.0)]

    assert _plane_raans(_group_satellites_by_orbital_plane(separated, None)) == [
        {0.0},
        {5.0},
        {10.1},
    ]
    assert _plane_raans(_group_satellites_by_orbital_plane(joined, None)) == [
        {0.0, 4.9},
        {10.0},
    ]


def test_grouping_splits_altitude_outliers_into_sub_planes() -> None:
    # Same RAAN cluster, but the 700 km satellite is more than 50 km above
    # the two 550 km satellites and therefore forms its own sub-plane.
    sats = [
        _sat(10.0, 0.0, 550.0),
        _sat(10.4, 0.0, 550.4),
        _sat(10.8, 0.0, 700.0),
    ]

    planes = _group_satellites_by_orbital_plane(sats, None)

    assert [sorted(sat.altitude_km for sat, _ in plane) for plane in planes] == [
        [550.0, 550.4],
        [700.0],
    ]

    # A wider allowed difference keeps the whole RAAN cluster together.
    merged = _group_satellites_by_orbital_plane(
        sats, None, max_altitude_difference=200.0
    )
    assert len(merged) == 1
    assert len(merged[0]) == 3


def test_grouping_does_not_apply_min_plane_size() -> None:
    # The parameter is documented but unused by this helper; the size filter
    # is applied later, for ADJACENT links only, in add_mesh_links.
    sats = [_sat(0.0), _sat(1.0), _sat(100.0), _sat(101.0)]

    planes = _group_satellites_by_orbital_plane(sats, None, min_plane_size=100)

    assert [len(plane) for plane in planes] == [2, 2]


def test_grouping_sorts_planes_by_average_raan() -> None:
    # The 350/351 plane is discovered first after the wrap cut but must be
    # returned after the 100/101 plane because its average RAAN is higher.
    sats = [_sat(350.0), _sat(351.0), _sat(100.0), _sat(101.0)]

    planes = _group_satellites_by_orbital_plane(sats, None)

    assert _plane_raans(planes) == [{100.0, 101.0}, {350.0, 351.0}]


# ---------------------------------------------------------------------------
# add_mesh_links
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("satellite_count", [0, 1])
def test_add_mesh_links_requires_two_satellites(satellite_count: int, caplog) -> None:
    manager = _FakeSimulationManager(
        [_sat(0.0) for _ in range(satellite_count)],
        [SimpleNamespace(label="gs-1")],
    )

    with caplog.at_level(logging.INFO):
        add_mesh_links(manager)

    assert "Not enough satellites to create mesh links" in caplog.text
    assert manager.isl_calls == []
    assert manager.gsl_calls == []


def test_add_mesh_links_warns_when_no_planes_can_be_formed(caplog) -> None:
    manager = _FakeSimulationManager(
        [_FakeSatellite(None), _FakeSatellite(None)],
        [SimpleNamespace(label="gs-1")],
    )

    with caplog.at_level(logging.INFO):
        add_mesh_links(manager)

    assert "Could not group satellites into orbital planes" in caplog.text
    assert manager.isl_calls == []
    assert manager.gsl_calls == []


def test_add_mesh_links_creates_intra_plane_ring_and_skips_singleton(caplog) -> None:
    # Three satellites at nearly identical RAAN form a ring ordered by phase;
    # the fourth satellite is a singleton plane and gets no ORBITAL link.
    ring = [_sat(0.0, 30.0), _sat(1.0, 10.0), _sat(2.0, 20.0)]
    singleton = _sat(100.0, 0.0)
    manager = _FakeSimulationManager(ring + [singleton])

    add_mesh_links(
        manager,
        add_orbital=True,
        add_adjacent=False,
        add_ground=False,
    )

    assert manager.isl_calls == [
        (ring[1], ring[2]),
        (ring[2], ring[0]),
        (ring[0], ring[1]),
    ]
    assert all(
        link.inter_satellite_direction is InterSatelliteLinkDirection.ORBITAL
        for link in manager.isl_links
    )
    assert singleton not in {sat for pair in manager.isl_calls for sat in pair}


def test_add_mesh_links_connects_adjacent_planes_by_sorted_phase() -> None:
    plane_a = [_sat(0.0, 270.0), _sat(0.0, 30.0), _sat(0.0, 150.0)]
    plane_b = [_sat(10.0, 150.0), _sat(10.0, 270.0), _sat(10.0, 30.0)]
    manager = _FakeSimulationManager(plane_a + plane_b)

    add_mesh_links(
        manager,
        add_orbital=False,
        add_adjacent=True,
        add_ground=False,
        min_plane_size=3,
    )

    assert manager.isl_calls == [
        (plane_a[1], plane_b[2]),
        (plane_a[2], plane_b[0]),
        (plane_a[0], plane_b[1]),
    ]
    assert [(sat_a.raan_deg, sat_b.raan_deg) for sat_a, sat_b in manager.isl_calls] == [
        (0.0, 10.0),
        (0.0, 10.0),
        (0.0, 10.0),
    ]
    assert all(
        link.inter_satellite_direction is InterSatelliteLinkDirection.ADJACENT
        for link in manager.isl_links
    )


def test_add_mesh_links_skips_adjacent_planes_below_min_plane_size() -> None:
    plane_a = [_sat(0.0, 0.0), _sat(0.0, 180.0)]
    plane_b = [_sat(10.0, 0.0), _sat(10.0, 180.0)]
    manager = _FakeSimulationManager(plane_a + plane_b)

    add_mesh_links(
        manager,
        add_orbital=False,
        add_adjacent=True,
        add_ground=False,
        min_plane_size=3,
    )

    assert manager.isl_calls == []


def test_add_mesh_links_skips_adjacent_pairs_farther_than_4000_km() -> None:
    # Opposite sides of a 7000 km-radius orbit are ~14000 km apart.
    plane_a = [_sat(0.0, 0.0, radius_km=7000.0), _sat(0.0, 90.0, radius_km=7000.0)]
    plane_b = [
        _sat(180.0, 0.0, radius_km=7000.0),
        _sat(180.0, 90.0, radius_km=7000.0),
    ]
    manager = _FakeSimulationManager(plane_a + plane_b)

    add_mesh_links(
        manager,
        add_orbital=False,
        add_adjacent=True,
        add_ground=False,
        min_plane_size=2,
    )

    assert manager.isl_calls == []


def test_add_mesh_links_does_not_wrap_last_plane_to_first() -> None:
    sats = [
        _sat(0.0, 0.0, radius_km=7000.0),
        _sat(10.0, 0.0, radius_km=7000.0),
        _sat(20.0, 0.0, radius_km=7000.0),
    ]
    manager = _FakeSimulationManager(sats)

    add_mesh_links(
        manager,
        add_orbital=False,
        add_adjacent=True,
        add_ground=False,
        min_plane_size=1,
    )

    # 20 -> 0 would be only ~2430 km apart, so a wrap-around link would have
    # been created if the implementation connected the last plane to the first.
    assert [(sat_a.raan_deg, sat_b.raan_deg) for sat_a, sat_b in manager.isl_calls] == [
        (0.0, 10.0),
        (10.0, 20.0),
    ]


def test_add_mesh_links_connects_ground_stations_to_closest_satellite(caplog) -> None:
    satellites = [_sat(0.0, 0.0), _sat(1.0, 180.0)]
    ground_station = SimpleNamespace(label="gs-1")
    manager = _FakeSimulationManager(satellites, [ground_station])

    with caplog.at_level(logging.INFO):
        add_mesh_links(
            manager,
            add_orbital=False,
            add_adjacent=False,
            add_ground=True,
        )

    assert manager.gsl_calls == [(satellites[0], ground_station)]
    assert "Mesh links created: 0 ISLs, 1 GSLs" in caplog.text


def test_add_mesh_links_warns_and_continues_when_ground_link_fails(caplog) -> None:
    satellites = [_sat(0.0, 0.0), _sat(1.0, 180.0)]
    good_gs = SimpleNamespace(label="gs-good")
    bad_gs = SimpleNamespace(label="gs-bad")
    manager = _FakeSimulationManager(
        satellites,
        [bad_gs, good_gs],
        fail_labels={"gs-bad"},
    )

    with caplog.at_level(logging.INFO):
        add_mesh_links(
            manager,
            add_orbital=False,
            add_adjacent=False,
            add_ground=True,
        )

    assert manager.gsl_calls == [(satellites[0], good_gs)]
    assert "Failed to connect ground station gs-bad" in caplog.text
    assert "no visible satellite" in caplog.text
    assert "Mesh links created: 0 ISLs, 1 GSLs" in caplog.text


def test_add_mesh_links_toggles_disable_link_creation(caplog, capsys) -> None:
    satellites = [_sat(0.0, 0.0), _sat(1.0, 180.0)]
    manager = _FakeSimulationManager(satellites, [SimpleNamespace(label="gs-1")])

    with caplog.at_level(logging.INFO):
        add_mesh_links(
            manager,
            add_orbital=False,
            add_adjacent=False,
            add_ground=False,
        )

    assert manager.isl_calls == []
    assert manager.gsl_calls == []
    assert "Mesh links created: 0 ISLs, 0 GSLs" in caplog.text
    assert capsys.readouterr().out.strip() == "Created 0 ISLs, 0 GSLs"


def test_add_mesh_links_reports_combined_isl_and_gsl_counts(caplog, capsys) -> None:
    # Two planes of three satellites plus one ground station:
    # 3 + 3 ORBITAL + 3 ADJACENT = 9 ISLs, 1 GSL.
    plane_a = [_sat(0.0, 30.0), _sat(0.0, 150.0), _sat(0.0, 270.0)]
    plane_b = [_sat(10.0, 30.0), _sat(10.0, 150.0), _sat(10.0, 270.0)]
    ground_station = SimpleNamespace(label="gs-1")
    manager = _FakeSimulationManager(plane_a + plane_b, [ground_station])

    with caplog.at_level(logging.INFO):
        add_mesh_links(manager, min_plane_size=3)

    orbital = [
        link
        for link in manager.isl_links
        if link.inter_satellite_direction is InterSatelliteLinkDirection.ORBITAL
    ]
    adjacent = [
        link
        for link in manager.isl_links
        if link.inter_satellite_direction is InterSatelliteLinkDirection.ADJACENT
    ]
    assert len(orbital) == 6
    assert len(adjacent) == 3
    assert manager.gsl_calls == [(plane_a[0], ground_station)]
    assert "Mesh links created: 9 ISLs, 1 GSLs" in caplog.text
    assert capsys.readouterr().out.strip() == "Created 9 ISLs, 1 GSLs"
