"""Regression tests for the iperf2 log parser.

iperf2 reports transfer sizes in binary units (1 MBytes = 2**20 bytes) but
bandwidth in decimal bit units (1 Mbits/sec = 1e6 bit/s). The parser must use
the units actually printed, keep the summary out of the interval rows, and
read the bare Retr column.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from satgonetem.traffic.iperf2_utils import generate_iperf2_plots

pytestmark = pytest.mark.unit

_MIB = 1024 * 1024
_MBIT = 1_000_000


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


def test_mbytes_are_binary_and_mbits_are_decimal() -> None:
    protocol, rows, _summary = _parse(
        [
            "[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender\n",
            "[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender\n",
        ]
    )

    assert protocol == "TCP"
    assert len(rows) == 1
    assert rows[0]["bytes"] == pytest.approx(1.25 * _MIB)
    assert rows[0]["bits_per_second"] == pytest.approx(10.50 * _MBIT)


@pytest.mark.parametrize(
    "transfer,rate,expected_bytes,expected_bps",
    [
        ("1.50 KBytes", "1.50 Kbits/sec", 1.5 * 1024, 1.5 * 1_000),
        ("2.00 GBytes", "3.00 Gbits/sec", 2.0 * 1024**3, 3.0 * 1_000**3),
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


def test_gbit_rate_uses_decimal_units() -> None:
    """iperf2 prints bit rates in decimal units: 46.6 Gbits/sec = 46.6e9."""
    _protocol, rows, _summary = _parse(
        [
            "[  3]  0.00-0.23  sec  1.00 GBytes  46.6 Gbits/sec    0    sender\n",
            "[  3]  0.00-0.23  sec  1.00 GBytes  46.6 Gbits/sec    0    sender\n",
        ]
    )

    assert len(rows) == 1
    assert rows[0]["bits_per_second"] == pytest.approx(46.6e9)


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


def test_same_span_stream_rows_are_both_intervals(tmp_path, capsys) -> None:
    """Two genuine 1-second stream rows must not lose one as a summary.

    Both rows span the same interval, but the last one's transfer is its own
    stream's bytes, not the sum of the other rows, so it is an interval.
    """
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
[  4]  0.00-1.00  sec  2.50 MBytes  21.00 Mbits/sec    0    sender
""",
    )

    _protocol, rows, summary = _parse(log.read_text().splitlines())
    generate_iperf2_plots(str(log), use_tmp_dir=False)

    assert len(rows) == 2
    assert summary is None
    assert {row["_label"] for row in rows} == {"Socket 3", "Socket 4"}
    assert sum(row["bytes"] for row in rows) == pytest.approx(3.75 * _MIB)
    assert _printed_total_mb(capsys) == pytest.approx(3.75, abs=0.01)


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
