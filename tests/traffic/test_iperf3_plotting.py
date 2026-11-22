"""Tests for the Iperf3Plotter presentation object."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from satgonetem.traffic.iperf3_plotting import Iperf3Plotter

pytestmark = pytest.mark.unit


def test_importing_the_facade_keeps_eager_plotting_dependencies() -> None:
    code = (
        "import sys; import satgonetem.traffic.iperf3_results; "
        "print('matplotlib' in sys.modules, 'matplotlib.pyplot' in sys.modules, "
        "'pandas' in sys.modules, sys.modules['matplotlib'].get_backend())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "True True True Agg"


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    streams = pd.DataFrame(
        {
            "socket": [5, 5],
            "start": [0.0, 1.0],
            "end": [1.0, 2.0],
            "bits_per_second": [100_000_000.0, 90_000_000.0],
            "bytes": [12_500_000, 11_250_000],
            "retransmits": [0, 1],
            "snd_cwnd": [100_000, 110_000],
            "snd_wnd": [200_000, 210_000],
            "rtt": [1000, 1100],
            "rttvar": [100, 120],
            "pmtu": [1500, 1500],
            "_t": [1.0, 2.0],
            "_label": ["Socket 5", "Socket 5"],
        }
    )
    summaries = pd.DataFrame(
        {
            "start": [0.0, 1.0],
            "end": [1.0, 2.0],
            "bits_per_second": [100_000_000.0, 90_000_000.0],
            "bytes": [12_500_000, 11_250_000],
            "_t": [1.0, 2.0],
            "_label": ["sum", "sum"],
        }
    )
    return streams, summaries


def test_tcp_metric_map_is_unchanged() -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    metric_map = plotter._metric_map()

    assert list(metric_map) == [
        "bits_per_second",
        "bytes",
        "retransmits",
        "snd_cwnd",
        "snd_wnd",
        "rtt",
        "rttvar",
        "pmtu",
    ]
    assert metric_map["bits_per_second"][0] == "Throughput (Mbps)"
    assert metric_map["bytes"][0] == "Bytes (MB)"
    assert metric_map["retransmits"][0] == "Retransmits (count)"
    assert metric_map["snd_cwnd"][0] == "Send CWnd (KB)"
    assert metric_map["snd_wnd"][0] == "Send Window (KB)"
    assert metric_map["rtt"][0] == "RTT (ms)"
    assert metric_map["rttvar"][0] == "RTT Var (us)"
    assert metric_map["pmtu"][0] == "Path MTU (bytes)"


def test_udp_metric_map_is_unchanged() -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("UDP", streams, summaries)

    metric_map = plotter._metric_map()

    assert list(metric_map) == [
        "bits_per_second",
        "bytes",
        "packets",
        "jitter_ms",
        "lost_packets",
        "lost_percent",
        "out_of_order",
        "pmtu",
    ]
    assert metric_map["jitter_ms"][0] == "Jitter (ms)"
    assert metric_map["lost_packets"][0] == "Lost Packets"
    assert metric_map["lost_percent"][0] == "Loss (%)"
    assert metric_map["out_of_order"][0] == "Out-of-Order Packets"


def test_metric_transforms_keep_their_units() -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    metric_map = plotter._metric_map()

    assert metric_map["bits_per_second"][1](1_000_000.0) == pytest.approx(1.0)
    assert metric_map["bytes"][1](1_000_000.0) == pytest.approx(1.0)
    assert metric_map["snd_cwnd"][1](1_000.0) == pytest.approx(1.0)
    assert metric_map["snd_wnd"][1](1_000.0) == pytest.approx(1.0)
    assert metric_map["rtt"][1](1_000.0) == pytest.approx(1.0)
    assert metric_map["rttvar"][1](7) == 7


def test_unknown_metric_returns_none(tmp_path) -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    assert plotter.plot_metric("not_a_metric", output_dir=str(tmp_path)) is None


def test_absent_or_all_null_metric_returns_none(tmp_path) -> None:
    streams, summaries = _frames()
    streams["rtt"] = [None, None]
    plotter = Iperf3Plotter("TCP", streams, summaries)

    assert plotter.plot_metric("packets", output_dir=str(tmp_path)) is None
    assert plotter.plot_metric("rtt", output_dir=str(tmp_path)) is None


def test_plot_metric_writes_the_requested_file(tmp_path) -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    path = plotter.plot_metric(
        "bits_per_second", output_dir=str(tmp_path), file_stem="sample", fmt="pdf"
    )

    assert path == str(Path(tmp_path) / "sample_bits_per_second.pdf")
    assert Path(path).exists()


def test_plot_metric_falls_back_to_stream_rows_for_tcp_only_metrics(tmp_path) -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    path = plotter.plot_metric(
        "rtt", output_dir=str(tmp_path), file_stem="stream", use_summary=True
    )

    assert path is not None
    assert Path(path).exists()


def test_plot_all_writes_every_available_metric(tmp_path) -> None:
    streams, summaries = _frames()
    plotter = Iperf3Plotter("TCP", streams, summaries)

    paths = plotter.plot_all(output_dir=str(tmp_path), file_stem="all", fmt="png")

    stems = {Path(path).stem for path in paths}
    for metric in (
        "bits_per_second",
        "bytes",
        "retransmits",
        "snd_cwnd",
        "snd_wnd",
        "rtt",
        "rttvar",
        "pmtu",
    ):
        assert f"all_{metric}" in stems, f"missing plot for {metric}: {stems!r}"
    assert all(Path(path).exists() for path in paths)
