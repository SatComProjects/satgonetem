"""Tests for iperf3 plot generation."""

from __future__ import annotations

import json

import pytest

from satgonetem.traffic.iperf3_plots import generate_iperf3_plots
from satgonetem.traffic.iperf3_results import Iperf3Results

pytestmark = pytest.mark.unit


def _tcp_json() -> dict:
    return {
        "start": {"test_start": {"protocol": "TCP", "duration": 2, "num_streams": 1}},
        "intervals": [
            {
                "streams": [
                    {
                        "socket": 5,
                        "start": 0.0,
                        "end": 1.0,
                        "bits_per_second": 100_000_000.0,
                        "bytes": 12_500_000,
                        "retransmits": 0,
                        "snd_cwnd": 100_000,
                        "snd_wnd": 200_000,
                        "rtt": 1000,
                        "rttvar": 100,
                        "pmtu": 1500,
                    }
                ],
                "sum": {"bits_per_second": 100_000_000.0, "bytes": 12_500_000},
            },
            {
                "streams": [
                    {
                        "socket": 5,
                        "start": 1.0,
                        "end": 2.0,
                        "bits_per_second": 90_000_000.0,
                        "bytes": 11_250_000,
                        "retransmits": 1,
                        "snd_cwnd": 110_000,
                        "snd_wnd": 210_000,
                        "rtt": 1100,
                        "rttvar": 120,
                        "pmtu": 1500,
                    }
                ],
                "sum": {"bits_per_second": 90_000_000.0, "bytes": 11_250_000},
            },
        ],
        "end": {
            "sum_sent": {"bits_per_second": 95_000_000.0, "bytes": 23_750_000},
            "sum_received": {"bits_per_second": 95_000_000.0, "bytes": 23_750_000},
        },
    }


def test_generate_iperf3_plots_tcp_writes_pngs(tmp_path):
    source = tmp_path / "tcp.json"
    source.write_text(json.dumps(_tcp_json()))

    files = generate_iperf3_plots(str(source), "sample", use_tmp_dir=False)

    assert files
    assert all(path.endswith(".pdf") for path in files)
    assert (tmp_path / "results").exists()
    assert (tmp_path / "results" / files[0].split("/")[-1]).exists()


def test_generate_iperf3_plots_empty_json_returns_no_files(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"start": {}, "intervals": [], "end": {}}))

    assert generate_iperf3_plots(str(source), "empty", use_tmp_dir=False) == []


def test_results_plot_metric_and_plot_all_write_pdfs(tmp_path):
    from satgonetem.traffic.iperf3_config import Iperf3Config

    results = Iperf3Results(json.dumps(_tcp_json()), Iperf3Config())

    metric_file = results.plot_metric(
        "bits_per_second", output_dir=str(tmp_path), file_stem="metric"
    )
    all_files = results.plot_all(output_dir=str(tmp_path), file_stem="all")

    assert metric_file
    assert all_files
    assert (tmp_path / metric_file).exists()
    assert all((tmp_path / name).exists() for name in all_files)
