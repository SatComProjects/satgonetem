"""Coverage-focused characterization tests for decorators and network helpers.

Covers :func:`satgonetem.utils.utils.time_`, TLE generation, and the fetch
helpers backed by ``urlopen``. Network access is mocked at the module boundary
with :func:`pytest.MonkeyPatch`, so no real HTTP request is made.
"""

from __future__ import annotations

import math
import re

import pytest
from sgp4.api import Satrec

from satgonetem.utils import utils as utils_module
from satgonetem.utils.utils import (
    _tle_line_checksum,
    fetch_iss_data,
    fetch_satellites_data,
    generate_satellite_tle,
    time_,
)

pytestmark = pytest.mark.unit

_ISS_URL = "https://live.ariss.org/iss.txt"
_SATELLITES_URL = "https://celestrak.example/tle.txt"

_ISS_LINE1 = "1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927"
_ISS_LINE2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537"
_SECOND_LINE1 = "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992"
_SECOND_LINE2 = "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123"


class _FakeResponse:
    """Minimal context-manager response exposing ``read``."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    body: str = "",
    error: Exception | None = None,
) -> list[tuple[str, int | None]]:
    """Replace ``utils.urlopen`` and record every call."""
    calls: list[tuple[str, int | None]] = []

    def fake_urlopen(url: str, timeout: int | None = None) -> _FakeResponse:
        calls.append((url, timeout))
        if error is not None:
            raise error
        return _FakeResponse(body)

    monkeypatch.setattr(utils_module, "urlopen", fake_urlopen)
    return calls


def test_time_decorator_preserves_plain_function_result_and_name(capsys) -> None:
    @time_
    def combine(left: int, right: int, *, scale: int = 1) -> int:
        return (left + right) * scale

    assert combine(2, right=3, scale=2) == 10
    assert combine.__name__ == "combine"

    output = capsys.readouterr().out
    assert re.fullmatch(r"\[time\] combine executed in \d+\.\d{6}s\n", output)


def test_time_decorator_preserves_classmethod_result_and_name(capsys) -> None:
    class Factory:
        prefix = "node"

        @time_
        @classmethod
        def describe(cls, index: int) -> str:
            return f"{cls.prefix}-{index}"

    assert Factory.describe(7) == "node-7"
    assert Factory.describe.__name__ == "describe"

    output = capsys.readouterr().out
    assert re.fullmatch(r"\[time\] describe executed in \d+\.\d{6}s\n", output)


def test_time_decorator_preserves_staticmethod_result_and_name(capsys) -> None:
    class Maths:
        @time_
        @staticmethod
        def double(value: int) -> int:
            return value * 2

    assert Maths.double(21) == 42
    assert Maths.double.__name__ == "double"

    output = capsys.readouterr().out
    assert re.fullmatch(r"\[time\] double executed in \d+\.\d{6}s\n", output)


def test_generate_satellite_tle_emits_checksum_valid_parseable_lines() -> None:
    tle = generate_satellite_tle(
        "TEST-SAT",
        inclination_degree=53.0,
        mean_motion_revolution_per_day=15.5,
        eccentricity=0.0001,
        argument_of_perigee_degree=40.0,
        mean_anomaly_degree=120.0,
        raan_degree=210.0,
    )

    assert set(tle) == {"satellite_name", "line1", "line2"}
    assert tle["satellite_name"] == "TEST-SAT"

    line1 = tle["line1"]
    line2 = tle["line2"]

    assert len(line1) == 69
    assert len(line2) == 69
    assert line1.startswith("1 00000U 00000ABC 00001.00000000")
    assert line1[68].isdigit()
    assert line2[68].isdigit()
    assert _tle_line_checksum(line1[:68]) == int(line1[68])
    assert _tle_line_checksum(line2[:68]) == int(line2[68])

    # sgp4 must be able to consume the generated pair and propagate it.
    satrec = Satrec.twoline2rv(line1, line2)
    assert satrec.error == 0
    assert satrec.inclo == pytest.approx(math.radians(53.0), abs=1e-6)
    assert satrec.ecco == pytest.approx(0.0001, abs=5e-8)
    assert satrec.no_kozai * 1440.0 / (2.0 * math.pi) == pytest.approx(15.5, abs=5e-9)

    error, position, velocity = satrec.sgp4(satrec.jdsatepoch, satrec.jdsatepochF)
    assert error == 0
    assert all(math.isfinite(component) for component in (*position, *velocity))


def test_generate_satellite_tle_rejects_line1_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first call computes the checksum appended to line 1; the second call
    # is the validation re-computation, which must disagree with that digit.
    calls = 0

    def corrupt_checksum(line: str) -> int:
        nonlocal calls
        calls += 1
        checksum = _tle_line_checksum(line)
        return (checksum + 1) % 10 if calls > 1 else checksum

    monkeypatch.setattr(utils_module, "_tle_line_checksum", corrupt_checksum)

    with pytest.raises(ValueError, match="TLE line 1 checksum failed"):
        generate_satellite_tle("TEST-SAT", 53.0, 15.5)


def test_generate_satellite_tle_rejects_line2_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def corrupt_line2_checksum(line: str) -> int:
        checksum = _tle_line_checksum(line)
        return (checksum + 1) % 10 if line.startswith("2 ") else checksum

    monkeypatch.setattr(utils_module, "_tle_line_checksum", corrupt_line2_checksum)

    with pytest.raises(ValueError, match="TLE line 2 checksum failed"):
        generate_satellite_tle("TEST-SAT", 53.0, 15.5)


def test_fetch_iss_data_parses_body_and_ignores_blank_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = f"\n  ISS (ZARYA)  \n\n {_ISS_LINE1} \n{_ISS_LINE2} \n\n"
    calls = _patch_urlopen(monkeypatch, body=body)

    assert fetch_iss_data() == {
        "name": "ISS (ZARYA)",
        "tle_line1": _ISS_LINE1,
        "tle_line2": _ISS_LINE2,
    }
    assert calls == [(_ISS_URL, 10)]


def test_fetch_iss_data_wraps_fetch_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    error = OSError("network down")
    calls = _patch_urlopen(monkeypatch, error=error)

    with pytest.raises(RuntimeError, match="Failed to fetch ISS TLE data") as excinfo:
        fetch_iss_data()

    assert excinfo.value.__cause__ is error
    assert calls == [(_ISS_URL, 10)]


@pytest.mark.parametrize(
    "body",
    [
        "",
        "\n \n\t\n",
        f"ISS (ZARYA)\n{_ISS_LINE1}\n",
    ],
)
def test_fetch_iss_data_rejects_bodies_with_fewer_than_three_lines(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _patch_urlopen(monkeypatch, body=body)

    with pytest.raises(RuntimeError, match="Unexpected ISS TLE format"):
        fetch_iss_data()


def test_fetch_satellites_data_parses_three_line_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        f"\nISS (ZARYA)\n{_ISS_LINE1}\n{_ISS_LINE2}\n\n"
        f"ONEWEB-0012\n{_SECOND_LINE1}\n{_SECOND_LINE2}\n\n"
    )
    calls = _patch_urlopen(monkeypatch, body=body)

    assert fetch_satellites_data(_SATELLITES_URL) == [
        {
            "name": "ISS (ZARYA)",
            "tle_line1": _ISS_LINE1,
            "tle_line2": _ISS_LINE2,
        },
        {
            "name": "ONEWEB-0012",
            "tle_line1": _SECOND_LINE1,
            "tle_line2": _SECOND_LINE2,
        },
    ]
    assert calls == [(_SATELLITES_URL, 10)]


def test_fetch_satellites_data_returns_empty_list_for_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, body="\n \n")

    assert fetch_satellites_data(_SATELLITES_URL) == []


def test_fetch_satellites_data_wraps_fetch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = TimeoutError("slow server")
    calls = _patch_urlopen(monkeypatch, error=error)

    with pytest.raises(
        RuntimeError, match="Failed to fetch satellite TLE data"
    ) as excinfo:
        fetch_satellites_data(_SATELLITES_URL)

    assert _SATELLITES_URL in str(excinfo.value)
    assert excinfo.value.__cause__ is error
    assert calls == [(_SATELLITES_URL, 10)]


@pytest.mark.parametrize(
    "body",
    [
        "one line\n",
        "name\n1 line\n",
        "name\n1 line\n2 line\n3 line\n",
    ],
)
def test_fetch_satellites_data_rejects_line_count_not_multiple_of_three(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    _patch_urlopen(monkeypatch, body=body)
    expected_count = len([line for line in body.splitlines() if line.strip()])

    with pytest.raises(RuntimeError, match="expected a multiple of 3 lines") as excinfo:
        fetch_satellites_data(_SATELLITES_URL)

    assert f"got {expected_count}" in str(excinfo.value)


@pytest.mark.parametrize(
    ("line1", "line2"),
    [
        ("3 " + _ISS_LINE1[2:], _ISS_LINE2),
        (_ISS_LINE1, "9 " + _ISS_LINE2[2:]),
    ],
)
def test_fetch_satellites_data_rejects_invalid_tle_prefixes(
    monkeypatch: pytest.MonkeyPatch, line1: str, line2: str
) -> None:
    _patch_urlopen(monkeypatch, body=f"ISS (ZARYA)\n{line1}\n{line2}\n")

    with pytest.raises(
        RuntimeError, match="Invalid TLE lines for satellite 'ISS \\(ZARYA\\)'"
    ) as excinfo:
        fetch_satellites_data(_SATELLITES_URL)

    assert "must start with '1 ' and '2 '" in str(excinfo.value)
