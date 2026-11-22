"""Tests for experiment telemetry collection and reporting."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from satgonetem.telemetry import (
    JsonlMetricsWriter,
    MetricsCollector,
    build_report,
    write_json,
)


class _Link:
    def __init__(self, source, target, *, delay=10, capacity=1000, loss=0.0, jitter=0):
        self.source = source
        self.target = target
        self.delay = delay
        self._capacity = capacity
        self.loss = loss
        self.jitter = jitter
        self.is_active = True
        self.tx = 0
        self.rx = 0

    def get_capacity(self):
        return self._capacity


def _topology():
    sat = SimpleNamespace(name="Sat0")
    gnd = SimpleNamespace(name="Gnd0")
    link = _Link(sat, gnd, delay=12, capacity=5000, loss=2.5, jitter=7)
    topology = SimpleNamespace(
        satellites={"Sat0": sat},
        ground_stations={"Gnd0": gnd},
        links={"link": link},
        current_time_step=3,
        update_time=5,
    )
    return topology, link


def test_sample_includes_topology_and_link_state():
    topology, _ = _topology()
    sample = MetricsCollector(topology).sample()

    assert sample["step"] == 3
    assert sample["sim_time_s"] == 15.0
    assert sample["topology"] == {"satellites": 1, "ground_stations": 1, "links": 1}
    assert sample["links"] == [
        {
            "src": "Sat0",
            "dst": "Gnd0",
            "type": "",
            "active": True,
            "admin_up": True,
            "delay_ms": 12,
            "capacity_kbps": 5000,
            "loss_percent": 2.5,
            "jitter_ms": 7,
            "tx_bps": 0,
            "rx_bps": 0,
        }
    ]


def test_usage_provider_overrides_tx_and_failures_are_contained():
    topology, link = _topology()
    link.tx = 99

    def provider():
        return [{"src": "Sat0", "dst": "Gnd0", "value": 1234}]

    sample = MetricsCollector(topology, usage_provider=provider).sample()
    assert sample["links"][0]["tx_bps"] == 1234

    def broken():
        raise RuntimeError("no files")

    sample = MetricsCollector(topology, usage_provider=broken).sample()
    assert sample["links"][0]["tx_bps"] == 99


def test_sample_records_last_error():
    topology, _ = _topology()
    topology.get_last_simulation_error = lambda: RuntimeError("tick failed")
    sample = MetricsCollector(topology).sample()
    assert sample["last_error"] == "tick failed"


def test_jsonl_writer_appends_samples(tmp_path):
    path = tmp_path / "metrics.jsonl"
    with JsonlMetricsWriter(path) as writer:
        writer.write({"step": 1})
        writer.write({"step": 2})

    lines = path.read_text().splitlines()
    assert [json.loads(line) for line in lines] == [{"step": 1}, {"step": 2}]


def test_write_json_creates_parent_directories(tmp_path):
    target = write_json(tmp_path / "nested" / "summary.json", {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


def _run_dir(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "manifest.yaml").write_text(
        "name: demo\ntopology: test.json\nduration_s: 10\ntick_s: 1\n"
    )
    (root / "metrics.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step": 1,
                        "sim_time_s": 1,
                        "links": [{"tx_bps": 10, "rx_bps": 5}],
                    }
                ),
                json.dumps(
                    {
                        "step": 2,
                        "sim_time_s": 2,
                        "links": [{"tx_bps": 30, "rx_bps": 15}],
                        "last_error": "boom",
                    }
                ),
            ]
        )
    )
    (root / "flows.json").write_text(
        json.dumps(
            [
                {
                    "source": "Gnd0",
                    "destination": "Gnd1",
                    "tool": "ping",
                    "error": None,
                }
            ]
        )
    )
    return root


def test_build_report_writes_summary_and_markdown(tmp_path):
    root = _run_dir(tmp_path)
    summary = build_report(root)

    assert summary["metrics"]["samples"] == 2
    assert summary["metrics"]["max_tx_bps"] == 30
    assert summary["metrics"]["avg_tx_bps"] == 20
    assert summary["metrics"]["last_errors"] == ["boom"]
    assert summary["flows"]["count"] == 1
    assert (root / "summary.json").is_file()
    report = (root / "report.md").read_text()
    assert "# Run report: demo" in report
    assert "Max / average TX" in report
    assert "boom" in report


def test_build_report_rejects_missing_manifest(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_report(tmp_path)


def test_build_report_rejects_malformed_manifest_as_value_error(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "manifest.yaml").write_text("name: [unclosed\n")

    with pytest.raises(ValueError, match="manifest"):
        build_report(root)


def test_build_report_tolerates_non_list_link_values(tmp_path):
    root = _run_dir(tmp_path)
    (root / "metrics.jsonl").write_text(json.dumps({"step": 1, "links": 5}) + "\n")

    summary = build_report(root)

    assert summary["metrics"]["samples"] == 1
    assert (root / "report.md").is_file()


def test_build_report_includes_topology_counts(tmp_path):
    root = _run_dir(tmp_path)
    (root / "metrics.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step": 0,
                        "sim_time_s": 0,
                        "topology": {
                            "satellites": 9,
                            "ground_stations": 1,
                            "links": 12,
                        },
                        "links": [],
                    }
                ),
                json.dumps(
                    {
                        "step": 1,
                        "sim_time_s": 1,
                        "topology": {
                            "satellites": 9,
                            "ground_stations": 2,
                            "links": 13,
                        },
                        "links": [],
                    }
                ),
            ]
        )
    )

    summary = build_report(root)

    assert summary["topology"]["first"] == {
        "satellites": 9,
        "ground_stations": 1,
        "links": 12,
    }
    assert summary["topology"]["last"] == {
        "satellites": 9,
        "ground_stations": 2,
        "links": 13,
    }
    report = (root / "report.md").read_text()
    assert "Satellites" in report
    assert "Ground stations" in report
    assert "Links" in report


def test_build_report_tolerates_truncated_flows_json(tmp_path):
    root = _run_dir(tmp_path)
    (root / "flows.json").write_text('[{"source": "Gnd0", "destina')

    summary = build_report(root)

    assert summary["flows"]["count"] == 0
    assert summary["flows"]["read_error"]
    assert "flows.json" in summary["flows"]["read_error"]
    report = (root / "report.md").read_text()
    assert "flows.json" in report


class _Unserializable:
    def __str__(self):
        raise RuntimeError("cannot serialize")


def test_write_json_is_atomic_on_failure(tmp_path):
    target = tmp_path / "summary.json"
    target.write_text("original\n")

    with pytest.raises(RuntimeError, match="cannot serialize"):
        write_json(target, {"bad": _Unserializable()})

    assert target.read_text() == "original\n"
    assert list(tmp_path.glob(".summary.json*")) == []
