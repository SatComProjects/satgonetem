"""Regression tests through the public iperf2 plotting entry point.

These use only ``generate_iperf2_plots`` (the API that existed before the
parser fix) and assert the values users see. They fail against the pre-fix
implementation because it assumed decimal units, double-counted the final
TCP summary row, and ignored KBytes/Gbits rows.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from satgonetem.traffic.iperf2_utils import generate_iperf2_plots

pytestmark = pytest.mark.unit


def _write_log(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "iperf2.log"
    path.write_text(content)
    return path


def _out(capsys) -> str:
    return capsys.readouterr().out


def _printed(pattern: str, capsys) -> float:
    match = re.search(pattern, _out(capsys))
    assert match, f"missing {pattern!r} in summary output"
    return float(match.group(1))


def test_public_total_does_not_double_count_tcp_summary(tmp_path, capsys):
    """The final TCP summary row must not be added to the interval rows."""
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0    sender
[  3]  1.00-2.00  sec  2.50 MBytes  21.00 Mbits/sec    1    sender
[  3]  0.00-2.00  sec  3.75 MBytes  15.75 Mbits/sec    1    sender
""",
    )

    files = generate_iperf2_plots(str(log), use_tmp_dir=False)
    total = _printed(r"Total Data Transferred:\s+([\d.]+) MB", capsys)

    assert files
    assert total == pytest.approx(3.75, abs=0.01)


def test_public_mbytes_is_binary_when_mixed_with_kbytes(tmp_path, capsys):
    """1024 KBytes + 1 MBytes is 2 MiB only if MBytes means 2**20 bytes."""
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  1024.00 KBytes  10.50 Mbits/sec    0    sender
[  3]  1.00-2.00  sec  1.00 MBytes  10.50 Mbits/sec    0    sender
""",
    )

    files = generate_iperf2_plots(str(log), use_tmp_dir=False)
    total = _printed(r"Total Data Transferred:\s+([\d.]+) MB", capsys)

    assert files
    assert total == pytest.approx(2.00, abs=0.01)


def test_public_kbytes_and_gbits_rows_are_recognized(tmp_path, capsys):
    """KBytes transfers and Gbits/sec bandwidth must produce plots/stats."""
    log = _write_log(
        tmp_path,
        """\
[  3]  0.00-1.00  sec  2048.00 KBytes  1.00 Gbits/sec    0    sender
[  3]  1.00-2.00  sec  2048.00 KBytes  1.00 Gbits/sec    0    sender
[  3]  0.00-2.00  sec  4096.00 KBytes  1.00 Gbits/sec    0    sender
""",
    )

    files = generate_iperf2_plots(str(log), use_tmp_dir=False)
    out = _out(capsys)
    total_match = re.search(r"Total Data Transferred:\s+([\d.]+) MB", out)
    throughput_match = re.search(r"Average Throughput:\s+([\d.]+) Mbps", out)

    assert files
    assert total_match is not None
    assert float(total_match.group(1)) == pytest.approx(4.00, abs=0.01)
    assert throughput_match is not None
    assert float(throughput_match.group(1)) == pytest.approx(1024.00, abs=0.01)
