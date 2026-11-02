"""Regression tests for TRAFFIC-19.

``generate_iperf2_plots()`` used to raise ``UnboundLocalError: total_mb``
for TCP iperf2 logs with data rows because ``total_mb`` was only assigned
inside the UDP summary branch but printed unconditionally.  These tests use
small synthetic logs and the non-GUI matplotlib backend; no real iperf2
traffic is generated.
"""

import math
import re
from pathlib import Path

from satgonetem.traffic.iperf2_utils import generate_iperf2_plots

_TCP_LOG = """\
------------------------------------------------------------
Client connecting to 10.0.0.2, TCP port 5001
TCP window size: 85.3 KByte (default)
------------------------------------------------------------
[  3] local 10.0.0.1 port 54123 connected with 10.0.0.2 port 5001
[ ID] Interval       Transfer     Bandwidth       Retr
[  3]  0.00-1.00  sec  1.25 MBytes  10.50 Mbits/sec    0             sender
[  3]  1.00-2.00  sec  2.50 MBytes  21.00 Mbits/sec    1             sender
"""

_UDP_LOG = """\
------------------------------------------------------------
Client connecting to 10.0.0.2, UDP port 5001
Sending 1470 byte datagrams, IPG target: 1120.00 us (kaiman)
UDP buffer size: 208 KByte (default)
------------------------------------------------------------
[  3] local 10.0.0.1 port 54124 connected with 10.0.0.2 port 5001
[ ID] Interval       Transfer     Bandwidth        Jitter   Lost/Total Datagrams
[  3]  0.00-1.00  sec  1.25 MBytes  10.49 Mbits/sec  0.050 ms  0/892 (0%)
[  3]  1.00-2.00  sec  1.25 MBytes  10.49 Mbits/sec  0.040 ms  0/892 (0%)
[  3]  0.00-2.00  sec  2.50 MBytes  10.49 Mbits/sec  0.045 ms  0/1784 (0%)
"""


def _write_log(tmp_path: Path, name: str, content: str) -> Path:
    log_path = tmp_path / name
    log_path.write_text(content)
    return log_path


def _printed_total_mb(capsys) -> float:
    out = capsys.readouterr().out
    match = re.search(r"Total Data Transferred:\s+([\d.]+) MB", out)
    assert match, f"missing total MB in summary output:\n{out}"
    return float(match.group(1))


def test_tcp_log_returns_plots_and_finite_total(tmp_path, capsys):
    """TCP logs with data rows must not crash and must print a finite total."""
    log_path = _write_log(tmp_path, "iperf2_tcp.log", _TCP_LOG)

    files = generate_iperf2_plots(str(log_path), use_tmp_dir=False)

    expected_names = {
        "iperf2_tcp_throughput.png",
        "iperf2_tcp_bytes.png",
        "iperf2_tcp_retransmits.png",
        "iperf2_tcp_cumulative.png",
    }
    assert {Path(f).name for f in files} == expected_names
    assert all(Path(f).is_file() for f in files)
    assert all(Path(f).parent == tmp_path / "results" for f in files)

    total_mb = _printed_total_mb(capsys)
    assert math.isfinite(total_mb)
    # The two TCP interval rows carry 1.25 + 2.50 MBytes.
    assert math.isclose(total_mb, 3.75, abs_tol=0.01)


def test_udp_summary_log_uses_summary_total(tmp_path, capsys):
    """UDP summary rows still drive the printed total and keep all plots."""
    log_path = _write_log(tmp_path, "iperf2_udp.log", _UDP_LOG)

    files = generate_iperf2_plots(str(log_path), use_tmp_dir=False)

    expected_names = {
        "iperf2_udp_throughput.png",
        "iperf2_udp_bytes.png",
        "iperf2_udp_jitter.png",
        "iperf2_udp_loss.png",
        "iperf2_udp_cumulative.png",
    }
    assert {Path(f).name for f in files} == expected_names
    assert all(Path(f).is_file() for f in files)

    total_mb = _printed_total_mb(capsys)
    assert math.isfinite(total_mb)
    assert math.isclose(total_mb, 2.50, abs_tol=0.01)
