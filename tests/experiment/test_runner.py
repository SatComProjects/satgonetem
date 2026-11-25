"""Tests for scenario parsing and experiment execution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from satgonetem.experiment import (
    EventSpec,
    ExperimentRunner,
    Scenario,
    ScenarioError,
    TrafficSpec,
)


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


@pytest.mark.parametrize(
    "name",
    ["../escape", "../../escape", "/tmp/escape", "a/b", "a\\b", "..", "."],
)
def test_scenario_name_rejects_path_traversal(name):
    with pytest.raises(ScenarioError, match="name"):
        Scenario(name=name, topology="t.json", duration_s=1)


@pytest.mark.parametrize("name", ["demo", "demo run-1", "scenario_v2"])
def test_scenario_name_keeps_normal_names(name):
    scenario = Scenario(name=name, topology="t.json", duration_s=1)
    assert scenario.name == name


def test_default_run_dir_stays_under_runs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.json").write_text("{}")
    scenario = Scenario(name="safe name", topology="t.json", duration_s=1)

    result = ExperimentRunner(scenario).run(dry_run=True)

    assert result.run_dir.parent == Path("runs")
    assert result.run_dir.name.startswith("safe name_")


@pytest.mark.parametrize(
    "body, label",
    [
        ("duration_s: .nan\n", "scenario.duration_s"),
        ("duration_s: 1\nmetrics_interval_s: .inf\n", "metrics_interval_s"),
        (
            "duration_s: 1\ntraffic:\n  - {src: a, dst: b, start_s: .nan}\n",
            "traffic[0].start_s",
        ),
        (
            "duration_s: 1\ntraffic:\n  - {src: a, dst: b, duration_s: .inf}\n",
            "traffic[0].duration_s",
        ),
        (
            "duration_s: 1\ntraffic:\n  - {src: a, dst: b, bandwidth_mbps: .nan}\n",
            "traffic[0].bandwidth_mbps",
        ),
        (
            "duration_s: 1\nevents:\n"
            "  - {at_s: .inf, action: link_up, src: a, dst: b}\n",
            "events[0].at_s",
        ),
        (
            "duration_s: 1\nevents:\n"
            "  - {at_s: 1, action: link_down, src: a, dst: b, duration_s: .nan}\n",
            "events[0].duration_s",
        ),
        (
            "duration_s: 1\nevents:\n"
            "  - {at_s: 1, action: impairment, src: a, dst: b, "
            "loss_percent: .nan}\n",
            "events[0].loss_percent",
        ),
    ],
)
def test_scenario_rejects_non_finite_numbers(tmp_path, body, label):
    path = _write_scenario(tmp_path, f"name: x\ntopology: t.json\n{body}")

    with pytest.raises(ScenarioError) as excinfo:
        Scenario.from_file(path)
    assert label in str(excinfo.value)


def test_direct_specs_reject_non_finite_numbers():
    with pytest.raises(ScenarioError):
        Scenario(name="x", topology="t.json", duration_s=float("nan"))
    with pytest.raises(ScenarioError):
        Scenario(
            name="x",
            topology="t.json",
            duration_s=1,
            metrics_interval_s=float("inf"),
        )
    with pytest.raises(ScenarioError):
        TrafficSpec(src="a", dst="b", start_s=float("nan"))
    with pytest.raises(ScenarioError):
        TrafficSpec(src="a", dst="b", duration_s=float("inf"))
    with pytest.raises(ScenarioError):
        TrafficSpec(src="a", dst="b", bandwidth_mbps=float("nan"))
    with pytest.raises(ScenarioError):
        EventSpec(at_s=float("inf"), action="link_up", src="a", dst="b")
    with pytest.raises(ScenarioError):
        EventSpec(
            at_s=1,
            action="link_up",
            src="a",
            dst="b",
            loss_percent=float("nan"),
        )


def test_event_loss_percent_must_be_between_zero_and_hundred(tmp_path):
    for loss in (-1, 150):
        path = _write_scenario(
            tmp_path,
            f"name: x\ntopology: t.json\nduration_s: 1\n"
            f"events:\n  - {{at_s: 1, action: impairment, src: a, dst: b, "
            f"loss_percent: {loss}}}\n",
        )
        with pytest.raises(ScenarioError, match="loss_percent"):
            Scenario.from_file(path)


def test_run_manifest_reloads_as_scenario(tmp_path):
    topology_path = tmp_path / "t.json"
    topology_path.write_text("{}")
    path = _write_scenario(
        tmp_path,
        "name: demo\ntopology: t.json\nduration_s: 2\ntick_s: 1\n",
    )
    scenario = Scenario.from_file(path)

    result = ExperimentRunner(scenario).run(output_dir=tmp_path / "run", dry_run=True)

    manifest_data = yaml.safe_load((result.run_dir / "manifest.yaml").read_text())
    assert manifest_data["run_dir"] == str(result.run_dir)
    assert "started_at" in manifest_data
    assert manifest_data["topology"] == str(topology_path.resolve())

    reloaded = Scenario.from_file(result.run_dir / "manifest.yaml")
    assert reloaded.name == "demo"
    assert reloaded.duration_s == 2.0
    assert Path(reloaded.topology).is_file()


def test_dry_run_keeps_bundled_topology_name_replayable(tmp_path):
    scenario = Scenario(
        name="bundled",
        topology="test_topology.json",
        duration_s=1,
        tick_s=1,
    )

    result = ExperimentRunner(scenario).run(output_dir=tmp_path / "run", dry_run=True)

    manifest_data = yaml.safe_load((result.run_dir / "manifest.yaml").read_text())
    assert manifest_data["topology"] == "test_topology.json"
    reloaded = Scenario.from_file(result.run_dir / "manifest.yaml")
    assert reloaded.topology == "test_topology.json"


class _StartBoomTopology(_FakeTopology):
    def start_gonetem(self):
        raise RuntimeError("start boom")


def test_startup_failure_still_attempts_backend_teardown(tmp_path):
    topology = _StartBoomTopology()
    scenario = _scenario(tmp_path, traffic=[], events=[])

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=True, realtime=False)

    assert any("start boom" in error for error in result.errors)
    assert ("stop_gonetem",) in topology.calls


def test_topology_preparation_failure_writes_error_artifacts(tmp_path):
    scenario = _scenario(tmp_path, traffic=[], events=[])

    def fail_factory(path, config):
        raise RuntimeError("load boom")

    result = ExperimentRunner(
        scenario,
        topology_factory=fail_factory,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=False, realtime=False)

    assert any("load boom" in error for error in result.errors)
    for name in (
        "manifest.yaml",
        "metrics.jsonl",
        "flows.json",
        "errors.json",
        "summary.json",
        "report.md",
    ):
        assert (result.run_dir / name).is_file(), name
    summary = json.loads((result.run_dir / "summary.json").read_text())
    assert any("load boom" in error for error in summary["errors"])


class _BoomTopology(_FakeTopology):
    def next_step(self):
        raise RuntimeError("tick boom")


def test_failing_tick_still_writes_artifacts(tmp_path):
    topology = _BoomTopology()
    scenario = _scenario(tmp_path, traffic=[], events=[])

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=False, realtime=False)

    for name in (
        "manifest.yaml",
        "metrics.jsonl",
        "flows.json",
        "summary.json",
        "report.md",
    ):
        assert (result.run_dir / name).is_file(), name
    assert any("tick boom" in error for error in result.errors)
    summary = json.loads((result.run_dir / "summary.json").read_text())
    assert any("tick boom" in error for error in summary["errors"])
    assert "tick boom" in (result.run_dir / "report.md").read_text()


class _InterruptTopology(_FakeTopology):
    def next_step(self):
        raise KeyboardInterrupt()


def test_interrupt_still_finalizes_artifacts_and_stops_backend(tmp_path):
    topology = _InterruptTopology()
    scenario = _scenario(tmp_path, traffic=[], events=[])

    with pytest.raises(KeyboardInterrupt):
        ExperimentRunner(
            scenario,
            topology_factory=lambda path, config: topology,
            collector_factory=lambda topo: _Collector(),
        ).run(output_dir=tmp_path / "run", live=True, realtime=False)

    for name in (
        "manifest.yaml",
        "metrics.jsonl",
        "flows.json",
        "errors.json",
        "summary.json",
        "report.md",
    ):
        assert (tmp_path / "run" / name).is_file(), name
    assert ("stop",) in topology.calls
    assert ("stop_gonetem",) in topology.calls


def test_reused_runner_resets_per_run_state(tmp_path):
    topology = _FakeTopology()
    link = SimpleNamespace(
        source=SimpleNamespace(name="Sat0"),
        target=SimpleNamespace(name="Sat1"),
    )
    topology.links["link"] = link
    scenario = _scenario(tmp_path)
    runner = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    )

    runner.run(output_dir=tmp_path / "run1", live=True, realtime=False)
    topology.current_time_step = 0
    result = runner.run(output_dir=tmp_path / "run2", live=True, realtime=False)

    ping_calls = [call for call in topology.calls if call[0] == "ping"]
    state_calls = [call for call in topology.calls if call[0] == "state"]
    assert len(ping_calls) == 2
    assert len(state_calls) == 4
    assert result.errors == []


class _JoinBoomFlow(_FakeFlow):
    def __init__(self, source, destination):
        super().__init__(source, destination)
        self.stopped = False

    def join(self, timeout=None):
        raise RuntimeError("join boom")

    def stop(self):
        self.stopped = True


class _JoinBoomTopology(_FakeTopology):
    def __init__(self):
        super().__init__()
        self.flow = None

    def ping(self, source, destination, config):
        self.flow = _JoinBoomFlow(source, destination)
        return [self.flow]


def test_flow_is_stopped_when_join_raises(tmp_path):
    topology = _JoinBoomTopology()
    scenario = _scenario(tmp_path)

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=True, realtime=False)

    assert topology.flow.stopped is True
    assert result.flows[0]["error"] == "join boom"
    assert any("join boom" in error for error in result.errors)
    summary = json.loads((result.run_dir / "summary.json").read_text())
    assert any("join boom" in error for error in summary["errors"])


class _CleanupBoomTopology(_FakeTopology):
    def stop(self):
        self.calls.append(("stop",))
        raise RuntimeError("stop boom")

    def stop_gonetem(self):
        self.calls.append(("stop_gonetem",))
        raise RuntimeError("cleanup boom")


def test_backend_teardown_errors_are_recorded_in_artifacts(tmp_path):
    topology = _CleanupBoomTopology()
    scenario = _scenario(tmp_path, traffic=[], events=[])

    result = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    ).run(output_dir=tmp_path / "run", live=True, realtime=False)

    assert any("stop boom" in error for error in result.errors)
    assert any("cleanup boom" in error for error in result.errors)
    summary = json.loads((result.run_dir / "summary.json").read_text())
    assert any("cleanup boom" in error for error in summary["errors"])
    assert "cleanup boom" in (result.run_dir / "report.md").read_text()


def test_write_manifest_is_atomic_on_failure(tmp_path):
    target = tmp_path / "scenario.yaml"
    target.write_text("original\n")
    scenario = Scenario(
        name="atomic",
        topology="t.json",
        duration_s=1,
        network={"bad": object()},
    )

    with pytest.raises(yaml.representer.RepresenterError):
        scenario.write_manifest(target)

    assert target.read_text() == "original\n"
    assert list(tmp_path.glob(".scenario.yaml*")) == []


def test_keyboard_interrupt_stops_active_flows_without_blocking_on_join(tmp_path):
    join_timeouts: list[float | None] = []
    stop_called = False

    class _SlowFlow:
        source = type("N", (), {"name": "Gnd0"})()
        destination = type("N", (), {"name": "Gnd1"})()

        def join(self, timeout=None):
            join_timeouts.append(timeout)

        def is_alive(self):
            return not stop_called

        def stop(self):
            nonlocal stop_called
            stop_called = True

    class _InterruptTopology(_FakeTopology):
        def next_step(self):
            runner._flows.append(_SlowFlow())
            raise KeyboardInterrupt()

    topology = _InterruptTopology()
    scenario = _scenario(tmp_path, traffic=[], events=[])
    runner = ExperimentRunner(
        scenario,
        topology_factory=lambda path, config: topology,
        collector_factory=lambda topo: _Collector(),
    )

    with pytest.raises(KeyboardInterrupt):
        runner.run(output_dir=tmp_path / "run", live=False, realtime=False)

    assert stop_called is True
    assert not any(t is not None and t > 10.0 for t in join_timeouts)
