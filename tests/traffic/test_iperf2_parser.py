"""Regression tests for the iperf2 log parser.

iperf2 reports transfer sizes in binary units (1 MBytes = 2**20 bytes) and
prints a final summary row that shares the per-stream line format. The parser
must use the units actually printed, keep the summary out of the interval
rows, and read the bare Retr column.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from satgonetem.traffic.iperf2_utils import generate_iperf2_plots

pytestmark = pytest.mark.unit

_MIB = 1024 * 1024


def _parse(lines: list[str]):
    from satgonetem.traffic.iperf2_utils import _parse_iperf2_log

    return _parse_iperf2_log(lines)


def _write_log(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "iperf2.log"
    path.write_text(content)
    return path


def _printed_total_mb(capsys) -> float:
    out = capsys.readouterr().out
    match = re.search(r"Total Data Transferred:\s+([\d.]+) MB", out)
    assert match, f"missing total MB in summary output:\n{out}"
    return float(match.group(1))


def _printed_retransmits(capsys) -> int:
    out = capsys.readouterr().out
    match = re.search(r"Total Retransmits:\s+(\d+)", out)
    assert match, f"missing retransmits in summary output:\n{out}"
    return int(match.group(1))


def test_mbytes_and_mbits_are_binary() -> None:
    protocol, rows, _summary = _parse(
        [
            "[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender\n",
            "[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender\n",
        ]
    )

    assert protocol == "TCP"
    assert len(rows) == 1
    assert rows[0]["bytes"] == pytest.approx(1.25 * _MIB)
    assert rows[0]["bits_per_second"] == pytest.approx(10.50 * _MIB)


@pytest.mark.parametrize(
    "transfer,rate,expected_bytes,expected_bps",
    [
        ("1.50 KBytes", "1.50 Kbits/sec", 1.5 * 1024, 1.5 * 1024),
        ("2.00 GBytes", "3.00 Gbits/sec", 2.0 * 1024**3, 3.0 * 1024**3),
    ],
)
def test_kilo_and_giga_units_are_parsed(
    transfer: str, rate: str, expected_bytes: float, expected_bps: float
) -> None:
    _protocol, rows, _summary = _parse(
        [
            f"[  3]  0.00-1.00  sec  {transfer}  {rate}    0    sender\n",
            f"[  3]  0.00-1.00  sec  {transfer}  {rate}    0    sender\n",
        ]
    )

    assert len(rows) == 1
    assert rows[0]["bytes"] == pytest.approx(expected_bytes)
    assert rows[0]["bits_per_second"] == pytest.approx(expected_bps)


def test_tcp_summary_row_is_not_counted_as_interval(tmp_path, capsys) -> None:
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
[  3]  1.00-2.00  sec  2.50 MBytes  21.00 Mbits/sec    1    sender
[  3]  0.00-2.00  sec  3.75 MBytes  15.75 Mbits/sec    1    sender
""",
    )

    _protocol, rows, summary = _parse(log.read_text().splitlines())
    generate_iperf2_plots(str(log), use_tmp_dir=False)

    assert len(rows) == 2
    assert summary is not None
    assert summary["bytes"] == pytest.approx(3.75 * _MIB)
    assert _printed_total_mb(capsys) == pytest.approx(3.75, abs=0.01)


def test_intervals_longer_than_one_second_are_not_summaries(tmp_path, capsys) -> None:
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-5.00   sec  1.25 MBytes  2.10 Mbits/sec    0    sender
[  3]  5.00-10.00  sec  1.25 MBytes  2.10 Mbits/sec    0    sender
[  3]  0.00-10.00  sec  2.50 MBytes  2.10 Mbits/sec    0    sender
""",
    )

    _protocol, rows, summary = _parse(log.read_text().splitlines())
    generate_iperf2_plots(str(log), use_tmp_dir=False)

    assert len(rows) == 2
    assert summary is not None
    assert _printed_total_mb(capsys) == pytest.approx(2.50, abs=0.01)


def test_one_second_summary_row_is_detected(tmp_path, capsys) -> None:
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
""",
    )

    _protocol, rows, summary = _parse(log.read_text().splitlines())
    generate_iperf2_plots(str(log), use_tmp_dir=False)

    assert len(rows) == 1
    assert summary is not None
    assert _printed_total_mb(capsys) == pytest.approx(1.25, abs=0.01)


def test_bare_retr_column_is_parsed(tmp_path, capsys) -> None:
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
[  3]  1.00-2.00  sec  1.25 MBytes  10.50 Mbits/sec    5    sender
[  3]  0.00-2.00  sec  2.50 MBytes  10.50 Mbits/sec    5    sender
""",
    )

    _protocol, rows, _summary = _parse(log.read_text().splitlines())
    generate_iperf2_plots(str(log), use_tmp_dir=False)

    assert [row["retransmits"] for row in rows] == [0, 5]
    assert _printed_retransmits(capsys) == 5
