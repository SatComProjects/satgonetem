"""Characterization tests for general helpers in :mod:`satgonetem.utils.utils`.

Covers the TLE checksum, pair-ID, distance, interface lookup, and local TLE file
reader helpers documented in ``docs/utils.md``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from satgonetem.models.interface import Interface
from satgonetem.utils.utils import (
    _tle_line_checksum,
    distance_3d_km,
    distance_3d_km_many,
    get_interface_from_name,
    read_satellites_data,
    unique_pair_id,
)

pytestmark = pytest.mark.unit

_STATION_TLE_LINE_1 = (
    "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
)
_STATION_TLE_LINE_2 = (
    "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"
)
_SECOND_TLE_LINE_1 = (
    "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992"
)
_SECOND_TLE_LINE_2 = (
    "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123"
)


def _write_tle_file(tmp_path: Path, lines: list[str]) -> Path:
    path = tmp_path / "satellites.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_tle_line_checksum_matches_known_iss_lines() -> None:
    assert _tle_line_checksum(_STATION_TLE_LINE_1[:68]) == 7
    assert _STATION_TLE_LINE_1[68] == "7"
    assert _tle_line_checksum(_STATION_TLE_LINE_2[:68]) == 7
    assert _STATION_TLE_LINE_2[68] == "7"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("0" * 68, 0),
        ("9" * 68, 2),
        ("-" * 68, 8),
        ("0" * 67 + "-", 1),
        ("A" * 68, 0),
        ("+" * 68, 0),
    ],
)
def test_tle_line_checksum_counts_digits_and_minus_signs(
    line: str, expected: int
) -> None:
    assert _tle_line_checksum(line) == expected


@pytest.mark.parametrize("line", ["", "x" * 67, "x" * 69])
def test_tle_line_checksum_requires_exactly_68_characters(line: str) -> None:
    with pytest.raises(ValueError, match="Must have exactly 68 characters"):
        _tle_line_checksum(line)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (0, 0),
        (1, 0),
        (0, 1),
        (3, 1),
        (1, 3),
        (7, 2),
        (2, 7),
        (10, 10),
        (42, 17),
    ],
)
def test_unique_pair_id_is_symmetric(a: int, b: int) -> None:
    assert unique_pair_id(a, b) == unique_pair_id(b, a)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (0, 0, 0),
        (0, 1, 1),
        (1, 1, 2),
        (0, 2, 3),
        (1, 2, 4),
        (2, 2, 5),
        (0, 3, 6),
        (1, 3, 7),
        (2, 3, 8),
        (3, 3, 9),
    ],
)
def test_unique_pair_id_matches_cantor_pairing_values(
    a: int, b: int, expected: int
) -> None:
    assert unique_pair_id(a, b) == expected


def test_unique_pair_id_is_unique_for_small_pairs() -> None:
    ids = {unique_pair_id(a, b) for a in range(20) for b in range(20)}

    # Unordered pairs with repetition: 20 * 21 / 2.
    assert len(ids) == 20 * 21 // 2
    assert min(ids) == 0
    assert all(isinstance(pair_id, int) and pair_id >= 0 for pair_id in ids)


def test_distance_3d_km_is_zero_for_coincident_points() -> None:
    assert distance_3d_km(12.0, 34.0, 0.5, 12.0, 34.0, 0.5) == pytest.approx(0.0)


def test_distance_3d_km_one_degree_latitude_at_equator() -> None:
    # Spherical Earth with R = 6371 km: 2*R*sin(0.5 deg) = 111.1935 km.
    distance = distance_3d_km(0.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    assert distance == pytest.approx(111.19351532028065, abs=1e-9)
    assert distance == pytest.approx(6371.0 * np.pi / 180.0, rel=1e-4)


def test_distance_3d_km_one_degree_longitude_at_equator() -> None:
    distance = distance_3d_km(0.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    assert distance == pytest.approx(111.19351532028065, abs=1e-9)


def test_distance_3d_km_many_matches_scalar_values() -> None:
    lat1 = [0.0, 12.0, 45.0, 0.0]
    lon1 = [0.0, 34.0, 10.0, 0.0]
    alt1 = [0.0, 0.5, 0.0, 550.0]
    lat2 = [1.0, 12.0, 45.0, 10.0]
    lon2 = [0.0, 34.0, 10.0, 0.0]
    alt2 = [0.0, 0.5, 2.0, 0.0]

    batch = distance_3d_km_many(lat1, lon1, alt1, lat2, lon2, alt2)
    scalar = [
        distance_3d_km(a, b, c, d, e, f)
        for a, b, c, d, e, f in zip(lat1, lon1, alt1, lat2, lon2, alt2, strict=False)
    ]

    assert isinstance(batch, np.ndarray)
    assert batch.shape == (4,)
    assert np.issubdtype(batch.dtype, np.floating)
    assert np.all(np.isfinite(batch))
    assert batch == pytest.approx(scalar)


def test_distance_3d_km_many_preserves_batch_shape_through_broadcasting() -> None:
    lat1 = np.zeros((2, 3))
    lon1 = np.zeros((2, 3))
    lat2 = np.full((2, 3), 1.0)

    batch = distance_3d_km_many(lat1, lon1, 0.0, lat2, lon1, 0.0)

    assert batch.shape == (2, 3)
    assert np.allclose(batch, 111.19351532028065)


def test_get_interface_from_name_matches_name_suffix() -> None:
    interfaces = [
        Interface(name="gnd0"),
        Interface(name="gnd1"),
        Interface(name="isl2"),
    ]

    found = get_interface_from_name(interfaces, "1")

    assert found is interfaces[1]


def test_get_interface_from_name_returns_none_when_absent() -> None:
    interfaces = [Interface(name="gnd0"), Interface(name="gnd1")]

    assert get_interface_from_name(interfaces, "9") is None
    assert get_interface_from_name([], "0") is None


def test_get_interface_from_name_uses_slice_not_prefix() -> None:
    interfaces = [Interface(name="gnd0")]

    assert get_interface_from_name(interfaces, "gnd0") is None
    assert get_interface_from_name(interfaces, "0") is interfaces[0]


def test_get_interface_from_name_short_name_matches_empty_suffix() -> None:
    # ``name[3:]`` is empty for names shorter than three characters.
    short = Interface(name="ab")

    assert get_interface_from_name([short], "") is short


def test_read_satellites_data_parses_three_line_groups(tmp_path: Path) -> None:
    path = _write_tle_file(
        tmp_path,
        [
            "ISS (ZARYA)",
            _STATION_TLE_LINE_1,
            _STATION_TLE_LINE_2,
            "",
            "ONEWEB-0012",
            _SECOND_TLE_LINE_1,
            _SECOND_TLE_LINE_2,
        ],
    )

    satellites = read_satellites_data(str(path))

    assert satellites == [
        {
            "name": "ISS (ZARYA)",
            "tle_line1": _STATION_TLE_LINE_1,
            "tle_line2": _STATION_TLE_LINE_2,
        },
        {
            "name": "ONEWEB-0012",
            "tle_line1": _SECOND_TLE_LINE_1,
            "tle_line2": _SECOND_TLE_LINE_2,
        },
    ]


def test_read_satellites_data_strips_surrounding_whitespace(tmp_path: Path) -> None:
    path = _write_tle_file(
        tmp_path,
        [
            "  ISS (ZARYA)  ",
            f"  {_STATION_TLE_LINE_1}  ",
            f"  {_STATION_TLE_LINE_2}  ",
        ],
    )

    satellites = read_satellites_data(str(path))

    assert satellites == [
        {
            "name": "ISS (ZARYA)",
            "tle_line1": _STATION_TLE_LINE_1,
            "tle_line2": _STATION_TLE_LINE_2,
        }
    ]


def test_read_satellites_data_returns_empty_list_for_empty_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")

    assert read_satellites_data(str(path)) == []


def test_read_satellites_data_rejects_line_count_not_multiple_of_three(
    tmp_path: Path,
) -> None:
    path = _write_tle_file(
        tmp_path,
        ["ISS (ZARYA)", _STATION_TLE_LINE_1, _STATION_TLE_LINE_2, "EXTRA"],
    )

    with pytest.raises(RuntimeError, match="multiple of 3"):
        read_satellites_data(str(path))


def test_read_satellites_data_rejects_bad_tle_prefixes(tmp_path: Path) -> None:
    path = _write_tle_file(
        tmp_path,
        ["ISS (ZARYA)", "3 not-line-1", "2 not-line-2"],
    )

    with pytest.raises(RuntimeError, match="must start with '1 ' and '2 '"):
        read_satellites_data(str(path))


def test_read_satellites_data_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.txt"

    with pytest.raises(RuntimeError, match="Failed to read satellite TLE file"):
        read_satellites_data(str(missing))


def test_read_satellites_data_does_not_validate_tle_checksums(
    tmp_path: Path,
) -> None:
    bad_line_1 = _STATION_TLE_LINE_1[:68] + "0"
    bad_line_2 = _STATION_TLE_LINE_2[:68] + "0"
    path = _write_tle_file(tmp_path, ["ISS (ZARYA)", bad_line_1, bad_line_2])

    satellites = read_satellites_data(str(path))

    assert satellites == [
        {
            "name": "ISS (ZARYA)",
            "tle_line1": bad_line_1,
            "tle_line2": bad_line_2,
        }
    ]
