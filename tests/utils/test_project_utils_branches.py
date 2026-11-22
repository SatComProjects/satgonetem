"""Branch-coverage tests for the satcom project utilities.

These tests pin the documented error and fallback behavior that the regular
module tests do not exercise: inconsistent simulation-manager bookkeeping when
adding a satellite, empty satellite lists, ground-object domain attachment
fallbacks, reading ground-station CSV files, and the defensive empty-grid
branches of the coverage estimators.

File I/O uses ``tmp_path``; the project fixtures API itself always writes its
generated ground-station file under ``/tmp``, which is asserted and cleaned up.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from sat_com_model.exceptions import GroundObjectDomainNotFoundError
from sat_com_model.models import GroundObjectDomain
from sat_com_model.models import Satellite as SatComSatellite

from satgonetem.utils import project_builder
from satgonetem.utils.project_builder import (
    _attach_ground_object_domain,
    add_custom_ground_station,
    add_custom_satellites,
    create_custom_ground_station,
    create_custom_satellite,
)
from satgonetem.utils.project_fixtures import create_test_project
from satgonetem.utils.project_ground_objects import GroundObjectFile, GroundStationEntry
from satgonetem.utils.satellite_coverage import (
    coverage_percentage,
    coverage_percentage_fast,
    coverage_ring_from_elevation,
)

pytestmark = pytest.mark.unit

_ISS_TLE = {
    "name": "BranchSat-001",
    "tle_line1": (
        "1 25544U 98067A   24150.50000000  .00020000  00000-0  28000-4 0  9999"
    ),
    "tle_line2": (
        "2 25544  51.6416 247.4627 0006703 130.5360 229.5775 15.509955193 12345"
    ),
}


def _satellite(
    latitude: float, longitude: float, altitude_km: float = 550.0
) -> SimpleNamespace:
    """Minimal satellite stand-in exposing the ``position`` mapping."""
    return SimpleNamespace(
        position={
            "latitude": latitude,
            "longitude": longitude,
            "altitude": altitude_km,
        }
    )


class _SatelliteManagerStub:
    """Simulation manager whose satellite bookkeeping is deliberately wrong.

    ``get_satellites`` reports an empty constellation until something is added,
    then returns the configured ``resulting_ids`` instead of the added object.
    This models a manager whose add/get round trip does not yield exactly one
    new identifier.
    """

    def __init__(self, resulting_ids: tuple[int, ...] = ()) -> None:
        self.time_manager = object()
        self._resulting_ids = resulting_ids
        self.added_satellites: list[SatComSatellite] = []
        self.link_update_calls = 0

    def get_satellites(self) -> list[SimpleNamespace]:
        if not self.added_satellites:
            return []
        return [
            SimpleNamespace(topology_uniq_id=satellite_id)
            for satellite_id in self._resulting_ids
        ]

    def add_satellite(self, satellite: SatComSatellite) -> None:
        self.added_satellites.append(satellite)

    def update_ground_station_links(self) -> None:
        self.link_update_calls += 1


class _DomainManagerStub:
    """Simulation-manager stand-in for ground-object domain resolution."""

    def __init__(
        self,
        *,
        existing_domain: GroundObjectDomain | None = None,
        candidate_domains: tuple[GroundObjectDomain, ...] = (),
    ) -> None:
        self.time_manager = object()
        self.simulation = SimpleNamespace(ground_object_domains=list(candidate_domains))
        self.existing_domain = existing_domain
        self.requested_identifiers: list[str] = []
        self.added_domains: list[GroundObjectDomain] = []
        self.added_ground_stations: list[object] = []

    def get_ground_object_domain(self, identifier: str) -> GroundObjectDomain:
        self.requested_identifiers.append(identifier)
        if self.existing_domain is None:
            raise GroundObjectDomainNotFoundError()
        return self.existing_domain

    def add_ground_object_domain(self, domain: GroundObjectDomain) -> None:
        self.added_domains.append(domain)
        self.simulation.ground_object_domains.append(domain)

    def add_ground_station(self, ground_station: object) -> None:
        self.added_ground_stations.append(ground_station)


class _UntouchableDomainManager(_DomainManagerStub):
    """Manager that fails the test if domain resolution is attempted."""

    def get_ground_object_domain(self, identifier: str) -> GroundObjectDomain:
        raise AssertionError("existing ground-object domain must not be resolved")


class _DuplicateDomainManager(_DomainManagerStub):
    """Manager whose identifier lookup is ambiguous for duplicate domains."""

    def get_ground_object_domain(self, identifier: str) -> GroundObjectDomain:
        self.requested_identifiers.append(identifier)
        raise ValueError(f"duplicate ground-object domain identifier: {identifier}")


class _NoAccessManager:
    """Manager that must not be touched at all."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"manager attribute {name!r} must not be accessed")


@pytest.mark.parametrize("resulting_ids", [(), (101, 102)])
def test_create_custom_satellite_requires_exactly_one_new_id(
    resulting_ids: tuple[int, ...],
) -> None:
    manager = _SatelliteManagerStub(resulting_ids)

    with pytest.raises(
        RuntimeError,
        match=(
            "Expected exactly one new satellite id, "
            rf"found {len(resulting_ids)}:"
        ),
    ):
        create_custom_satellite(_ISS_TLE, manager)  # type: ignore[arg-type]

    assert len(manager.added_satellites) == 1
    assert isinstance(manager.added_satellites[0], SatComSatellite)
    assert manager.added_satellites[0].satellite_name == "BranchSat-001"
    assert manager.link_update_calls == 1


def test_add_custom_satellites_with_empty_list_is_a_noop() -> None:
    assert add_custom_satellites(_NoAccessManager(), []) is None  # type: ignore[arg-type]


def test_add_custom_satellites_delegates_every_entry(monkeypatch) -> None:
    calls: list[tuple[dict, object]] = []

    def record_create(custom_sat: dict, simulation_manager: object) -> int:
        calls.append((custom_sat, simulation_manager))
        return len(calls)

    monkeypatch.setattr(project_builder, "create_custom_satellite", record_create)
    manager = object()
    custom_satellites = [
        {"name": "A", "tle_line1": "line1-a", "tle_line2": "line2-a"},
        {"name": "B", "tle_line1": "line1-b", "tle_line2": "line2-b"},
    ]

    assert (
        add_custom_satellites(manager, custom_satellites)  # type: ignore[arg-type]
        is None
    )
    assert calls == [
        (custom_satellites[0], manager),
        (custom_satellites[1], manager),
    ]


@pytest.mark.parametrize(
    ("domain", "expected_identifier"),
    [("public", "public"), ("", "default")],
)
def test_attach_ground_object_domain_creates_default_when_none_exist(
    domain: str, expected_identifier: str
) -> None:
    manager = _DomainManagerStub()
    ground_object = SimpleNamespace(domain=domain, ground_object_domain=None)

    _attach_ground_object_domain(ground_object, manager)  # type: ignore[arg-type]

    created_domain = ground_object.ground_object_domain
    assert created_domain is not None
    assert created_domain.identifier == expected_identifier
    assert created_domain.type == "ground_station"
    assert (
        created_domain.ground_to_space_connections_strategy
        == "best-angle-until-disconnection"
    )
    assert manager.requested_identifiers == [domain]
    assert manager.added_domains == [created_domain]


def test_add_custom_ground_station_creates_default_domain() -> None:
    manager = _DomainManagerStub()
    ground_station = create_custom_ground_station("Outpost", 10.0, 20.0, 0.5)

    add_custom_ground_station(ground_station, manager)  # type: ignore[arg-type]

    assert ground_station.ground_object_domain is not None
    assert ground_station.ground_object_domain.identifier == "public"
    assert ground_station.ground_object_domain.type == "ground_station"
    assert manager.added_domains == [ground_station.ground_object_domain]
    assert manager.added_ground_stations == [ground_station]


def test_attach_domain_prefers_matching_identifier_on_duplicates() -> None:
    """A duplicate-identifier lookup must not attach an unrelated station domain."""
    generic_public = GroundObjectDomain(identifier="public", type="generic")
    alpha_station = GroundObjectDomain(identifier="alpha", type="ground_station")
    public_station = GroundObjectDomain(identifier="public", type="ground_station")
    manager = _DuplicateDomainManager(
        candidate_domains=(generic_public, alpha_station, public_station)
    )
    ground_object = SimpleNamespace(domain="public", ground_object_domain=None)

    _attach_ground_object_domain(ground_object, manager)  # type: ignore[arg-type]

    assert ground_object.ground_object_domain is public_station
    assert manager.requested_identifiers == ["public"]
    assert manager.added_domains == []


def test_add_custom_ground_station_preserves_existing_domain() -> None:
    existing_domain = GroundObjectDomain(
        identifier="Existing Domain", type="ground_station"
    )
    manager = _UntouchableDomainManager()
    ground_station = create_custom_ground_station("Outpost", 10.0, 20.0, 0.5)
    ground_station.ground_object_domain = existing_domain

    add_custom_ground_station(ground_station, manager)  # type: ignore[arg-type]

    assert ground_station.ground_object_domain is existing_domain
    assert manager.added_domains == []
    assert manager.added_ground_stations == [ground_station]


def test_create_test_project_reads_supplied_ground_stations_file(
    tmp_path: pathlib.Path,
) -> None:
    csv_path = tmp_path / "stations.csv"
    csv_path.write_text(
        "7,Alice Springs,-23.698,133.88,0.545\n9,Nuuk,64.18,-51.72,0.032\n"
    )
    generated_path = pathlib.Path("/tmp/ground_stations.txt")

    try:
        project = create_test_project(ground_stations_file=str(csv_path))

        ground_object_property = project.ground_objects_properties[0]
        assert ground_object_property.identifier == "Ground Stations"
        assert pathlib.Path(ground_object_property.data_file) == generated_path
        assert generated_path.read_text() == (
            "7,Alice Springs,-23.698,133.88,0.545\n9,Nuuk,64.18,-51.72,0.032\n"
        )
    finally:
        generated_path.unlink(missing_ok=True)


def test_ground_object_file_from_csv_parses_entries(tmp_path: pathlib.Path) -> None:
    csv_path = tmp_path / "stations.csv"
    csv_path.write_text("0,Berlin,52.52,13.405,0.034\n1,London,51.507,-0.127,0.011\n")

    ground_object_file = GroundObjectFile.from_csv(
        identifier="Ground Stations", csv_path=str(csv_path)
    )

    assert ground_object_file.identifier == "Ground Stations"
    assert ground_object_file.entries == [
        GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
        GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
    ]


def test_ground_object_file_from_csv_accepts_empty_file(tmp_path: pathlib.Path) -> None:
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("")

    ground_object_file = GroundObjectFile.from_csv(
        identifier="Empty", csv_path=str(csv_path)
    )

    assert ground_object_file.entries == []


@pytest.mark.parametrize(
    "line",
    [
        "0,Berlin,52.52,13.405",
        "0,Berlin,52.52,13.405,0.034,extra",
    ],
)
def test_ground_object_file_from_csv_rejects_wrong_field_count(
    tmp_path: pathlib.Path, line: str
) -> None:
    csv_path = tmp_path / "malformed.csv"
    csv_path.write_text(f"{line}\n")

    with pytest.raises(ValueError, match="Malformed line in CSV"):
        GroundObjectFile.from_csv(identifier="Ground Stations", csv_path=str(csv_path))


def test_ground_object_file_from_csv_rejects_non_numeric_field(
    tmp_path: pathlib.Path,
) -> None:
    csv_path = tmp_path / "bad-number.csv"
    csv_path.write_text("0,Berlin,north,13.405,0.034\n")

    with pytest.raises(ValueError):
        GroundObjectFile.from_csv(identifier="Ground Stations", csv_path=str(csv_path))


def test_ground_object_file_from_csv_raises_oserror_for_missing_file(
    tmp_path: pathlib.Path,
) -> None:
    missing_path = tmp_path / "does-not-exist.csv"

    with pytest.raises(FileNotFoundError):
        GroundObjectFile.from_csv(
            identifier="Ground Stations", csv_path=str(missing_path)
        )


def test_polygon_coverage_handles_dateline_wrapped_ring() -> None:
    satellite = _satellite(latitude=0.0, longitude=-179.0)
    lon_deg, _, _ = coverage_ring_from_elevation(0.0, -179.0, 550.0, 10.0, 36)
    assert lon_deg is not None
    assert bool((lon_deg < -180.0).any())

    percentage = coverage_percentage([satellite], n_ring_pts=36)

    assert 0.0 < percentage <= 100.0


def test_polygon_coverage_returns_zero_for_empty_sampling_grid() -> None:
    satellite = _satellite(latitude=0.0, longitude=0.0)

    assert coverage_percentage([satellite], max_latitude_deg=-1.0) == 0.0


def test_fast_coverage_returns_zero_for_empty_sampling_grid() -> None:
    satellite = _satellite(latitude=0.0, longitude=0.0)

    assert coverage_percentage_fast([satellite], max_latitude_deg=-1.0) == 0.0
