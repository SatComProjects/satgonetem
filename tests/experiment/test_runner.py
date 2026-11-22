"""Tests for scenario parsing and experiment execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from satgonetem.experiment import ExperimentRunner, Scenario, ScenarioError


def _write_scenario(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    return path


def test_scenario_parses_traffic_events_and_network(tmp_path):
    path = _write_scenario(
        tmp_path,
        """
name: demo
topology: topology_files/test_topology.json
duration_s: 10
tick_s: 1
routing: static
network:
  ipv6_enabled: true
traffic:
  - {src: Gnd0, dst: Gnd1, tool: ping, start_s: 1, count: 3}
events:
  - {at_s: 2, action: link_down, src: Sat0, dst: Sat1, duration_s: 3}
metrics_interval_s: 2
connectivity: true
""",
    )

    scenario = Scenario.from_file(path)

    assert scenario.name == "demo"
    assert scenario.tick_s == 1
    assert scenario.network == {"ipv6_enabled": True}
    assert scenario.traffic[0].tool == "ping"
    assert scenario.events[0].action == "link_down"
    assert scenario.connectivity is True
    assert scenario.base_dir == str(tmp_path)


@pytest.mark.parametrize(
    "text, message",
    [
        ("name: x\ntopology: t.json\nduration_s: 1\nextra: 1\n", "unknown field"),
        ("name: x\ntopology: t.json\nduration_s: 1\ntick_s: 0\n", "tick_s"),
        (
            "name: x\ntopology: t.json\nduration_s: 1\n"
            "traffic:\n  - {src: Gnd0, dst: Gnd1, tool: nope}\n",
            "tool",
        ),
        (
            "name: x\ntopology: t.json\nduration_s: 1\nnetwork:\n  bogus: 1\n",
            "scenario.network is invalid",
        ),
        (
            "name: x\ntopology: t.json\nduration_s: 1\ntick_s: 2\n"
            "traffic:\n  - {src: Gnd0, dst: Gnd1, tool: ping}\n",
            "tick_s must be 1",
        ),
        (
            "name: x\ntopology: t.json\nduration_s: 1\n"
            "events:\n  - {at_s: 1, action: impairment, src: Sat0, dst: Sat1}\n",
            "requires loss_percent",
        ),
    ],
)
def test_scenario_rejects_invalid_fields(tmp_path, text, message):
    path = _write_scenario(tmp_path, text)
    with pytest.raises(ScenarioError, match=message):
        Scenario.from_file(path)


class _FakeResult:
    def to_json(self):
        return {"rtt_avg_ms": 1.5}


class _FakeFlow:
    def __init__(self, source, destination):
        self.source = source
        self.destination = destination
        self._alive = False

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive

    def stop(self):
        return None

    def results(self):
        return _FakeResult()


class _FakeTopology:
    def __init__(self):
        self.current_time_step = 0
        self.update_time = 1
        self.satellites = {}
        self.ground_stations = {}
        self.links = {}
        self.calls = []

    def start_gonetem(self):
        self.calls.append(("start",))

    def set_ip_addresses(self):
        self.calls.append(("addresses",))

    def init_routing(self, method):
        self.calls.append(("routing", method))
        return 1.0

    def stop(self):
        self.calls.append(("stop",))

    def stop_gonetem(self):
        self.calls.append(("stop_gonetem",))

    def next_step(self):
        self.current_time_step += 1
        return 0.0

    def get_node_by_name(self, name):
        return SimpleNamespace(name=name)

    def ping(self, source, destination, config):
        self.calls.append(("ping", source.name, destination.name, config.count))
        return [_FakeFlow(source, destination)]

    def set_link_state(self, link, up):
        self.calls.append(("state", link, up))

    def set_link_impairment(self, link, loss_percent=None, jitter_ms=None):
        self.calls.append(("impairment", link, loss_percent, jitter_ms))

    def restart_node(self, name):
        self.calls.append(("restart", name))


def _scenario(tmp_path: Path, **overrides):
    data = {
        "name": "demo",
        "topology": str(Path("topology_files/test_topology.json").resolve()),
        "duration_s": 3,
        "tick_s": 1,
        "traffic": [
            {"src": "Gnd0", "dst": "Gnd1", "tool": "ping", "start_s": 1, "count": 2}
        ],
        "events": [
            {
                "at_s": 1,
                "action": "link_down",
                "src": "Sat0",
                "dst": "Sat1",
                "duration_s": 1,
            },
            {
                "at_s": 2,
                "action": "impairment",
                "src": "Sat0",
                "dst": "Sat1",
                "loss_percent": 5.0,
            },
        ],
    }
    data.update(overrides)
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(data))
    return Scenario.from_file(path)


def test_runner_starts_backend_applies_events_and_writes_artifacts(tmp_path):
    topology = _FakeTopology()
    link = SimpleNamespace(
        source=SimpleNamespace(name="Sat0"), target=SimpleNamespace(name="Sat1")
    )
    topology.links["link"] = link
    scenario = _scenario(tmp_path)

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=True, realtime=False)

    assert result.run_dir == tmp_path / "run"
    assert ("start",) in topology.calls
    assert ("addresses",) in topology.calls
    assert ("ping", "Gnd0", "Gnd1", 2) in topology.calls
    assert ("state", link, False) in topology.calls
    assert ("state", link, True) in topology.calls
    assert ("impairment", link, 5.0, None) in topology.calls
    assert result.flows[0]["result"] == {"rtt_avg_ms": 1.5}
    assert (result.run_dir / "summary.json").is_file()
    assert (result.run_dir / "report.md").is_file()
    assert (result.run_dir / "metrics.jsonl").is_file()
    assert (
        json.loads((result.run_dir / "flows.json").read_text())[0]["source"] == "Gnd0"
    )


def test_runner_dry_run_does_not_load_topology(tmp_path):
    scenario = _scenario(tmp_path)
    called = []
    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: called.append(path),
    ).run(output_dir=tmp_path / "run", dry_run=True)

    assert called == []
    assert (result.run_dir / "manifest.yaml").is_file()
    assert result.summary["name"] == "demo"


def test_runner_offline_skips_traffic_but_samples_topology(tmp_path):
    topology = _FakeTopology()
    scenario = _scenario(tmp_path, traffic=[])

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=False)

    assert ("start",) not in topology.calls
    assert ("ping", "Gnd0", "Gnd1", 2) not in topology.calls
    assert result.samples[0]["step"] == 0


class _Collector:
    def sample(self):
        return {"step": 0, "links": [{"tx_bps": 1}], "topology": {}}
