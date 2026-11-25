"""Tests for the satgonetem command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from satgonetem.cli import main as cli
from satgonetem.cli.main import DoctorCheck, build_parser, main
from satgonetem.experiment import Scenario

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_parser_has_expected_commands():
    parser = build_parser()
    assert parser.prog == "satgonetem"
    help_text = parser.format_help()
    for command in ("version", "doctor", "summary", "run", "report"):
        assert command in help_text


def test_version_command(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def test_no_command_prints_help(capsys):
    assert main([]) == 1
    assert "usage:" in capsys.readouterr().out


def test_summary_works_offline(capsys):
    assert main(["summary", TEST_TOPOLOGY, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["satellites"] > 0
    assert payload["ground_stations"] > 0
    assert payload["ipv4_plan"] is True


def test_doctor_reports_checks(monkeypatch, capsys):
    checks = [
        DoctorCheck("python", True, "3.14"),
        DoctorCheck("docker", False, "not available"),
    ]
    monkeypatch.setattr(cli, "doctor_checks", lambda server: checks)
    assert main(["doctor", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "python"
    assert payload[1]["ok"] is False


def test_doctor_exit_zero_when_all_checks_pass(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "doctor_checks", lambda server: [DoctorCheck("python", True, "3.14")]
    )
    assert main(["doctor"]) == 0
    assert "[OK" in capsys.readouterr().out


def _scenario(tmp_path: Path) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        f"""
name: cli-demo
topology: {TEST_TOPOLOGY}
duration_s: 2
tick_s: 1
events:
  - {{at_s: 1, action: impairment, src: Sat0, dst: Sat1, loss_percent: 1.0}}
"""
    )
    return path


def test_run_dry_run_then_report(tmp_path, capsys):
    scenario = _scenario(tmp_path)
    run_dir = tmp_path / "run"

    assert main(["run", str(scenario), "--out", str(run_dir), "--dry-run"]) == 0
    assert (run_dir / "manifest.yaml").is_file()
    capsys.readouterr()

    assert main(["report", str(run_dir), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["name"] == "cli-demo"


def test_run_missing_scenario_returns_error(capsys):
    assert main(["run", "/does/not/exist.yaml"]) == 2
    assert "error:" in capsys.readouterr().err


def _feed(monkeypatch, lines):
    iterator = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(iterator)
        except StopIteration as exc:
            raise EOFError from exc

    monkeypatch.setattr("builtins.input", fake_input)


def test_create_opens_shell_and_writes_scenario(tmp_path, monkeypatch, capsys):
    target = tmp_path / "demo"
    _feed(
        monkeypatch,
        [
            "gnd add Berlin 52.52 13.405",
            "gnd add Paris 48.8566 2.3522",
            "traffic add Gnd66 Gnd67 ping start=5 count=3",
            "save",
            "quit",
        ],
    )

    assert main(["create", str(target)]) == 0

    path = tmp_path / "demo.yaml"
    scenario = Scenario.from_file(path)
    assert scenario.name == "demo"
    assert scenario.topology is None
    assert scenario.routing == "static"
    assert len(scenario.shells) == 1
    assert scenario.shells[0].identifier == "LEO"
    assert [station.name for station in scenario.ground_stations] == [
        "Berlin",
        "Paris",
    ]
    assert scenario.traffic[0].src == "Gnd66"
    assert scenario.traffic[0].count == 3
    assert "Created" in capsys.readouterr().out


def test_create_appends_yaml_suffix_and_accepts_name(tmp_path, monkeypatch):
    _feed(monkeypatch, ["save", "quit"])
    target = tmp_path / "lab"

    assert main(["create", str(target), "--name", "lab-study"]) == 0

    scenario = Scenario.from_file(tmp_path / "lab.yaml")
    assert scenario.name == "lab-study"


def test_create_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    _feed(monkeypatch, ["quit"])
    target = tmp_path / "demo"
    assert main(["create", str(target)]) == 0
    assert main(["create", str(target)]) == 2

    _feed(monkeypatch, ["quit"])
    assert main(["create", str(target), "--force"]) == 0


def test_edit_modifies_existing_scenario(tmp_path, monkeypatch):
    target = tmp_path / "demo.yaml"
    _feed(monkeypatch, ["quit"])
    assert main(["create", str(target)]) == 0

    _feed(
        monkeypatch,
        [
            "duration 120",
            "connectivity on",
            "network set ipv6_enabled true",
            "shell add MEO 3 4 55 12.5 delta",
            "gnd add Rome 41.9 12.5 0.02",
            "event add action=impairment at=30 src=Sat0 dst=Sat1 loss=1.5 jitter=2",
            "save",
            "quit",
        ],
    )
    assert main(["edit", str(target)]) == 0

    scenario = Scenario.from_file(target)
    assert scenario.duration_s == 120.0
    assert scenario.connectivity is True
    assert scenario.network["ipv6_enabled"] is True
    assert [shell.identifier for shell in scenario.shells] == ["LEO", "MEO"]
    assert scenario.ground_stations[-1].name == "Rome"
    assert scenario.events[0].loss_percent == 1.5
    assert scenario.events[0].jitter_ms == 2


def test_edit_defaults_to_last_created_scenario(tmp_path, monkeypatch):
    target = tmp_path / "demo.yaml"
    _feed(monkeypatch, ["quit"])
    assert main(["create", str(target)]) == 0

    _feed(monkeypatch, ["duration 45", "save", "quit"])
    assert main(["edit"]) == 0
    assert Scenario.from_file(target).duration_s == 45.0


def test_edit_defaults_to_scenario_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _feed(monkeypatch, ["quit"])
    assert main(["create", "scenario.yaml"]) == 0

    _feed(monkeypatch, ["duration 30", "save", "quit"])
    assert main(["edit"]) == 0
    assert Scenario.from_file(tmp_path / "scenario.yaml").duration_s == 30.0


def test_edit_falls_back_to_scenario_yaml_without_state(tmp_path, monkeypatch):
    from satgonetem.experiment.interactive import default_scenario

    monkeypatch.chdir(tmp_path)
    default_scenario("fallback").write_manifest(tmp_path / "scenario.yaml")
    _feed(monkeypatch, ["quit"])

    assert main(["edit"]) == 0


def test_edit_rejects_an_invalid_scenario(tmp_path, capsys):
    target = tmp_path / "broken.yaml"
    target.write_text("name: broken\n")

    assert main(["edit", str(target)]) == 2
    assert "error:" in capsys.readouterr().err


def test_edit_missing_file_returns_error(capsys):
    assert main(["edit", "/does/not/exist.yaml"]) == 2
    assert "error:" in capsys.readouterr().err


def test_shell_reports_unknown_command(tmp_path):
    from satgonetem.experiment.interactive import (
        ScenarioDraft,
        ScenarioShell,
        default_scenario,
    )

    output: list[str] = []
    shell = ScenarioShell(
        ScenarioDraft.from_scenario(default_scenario("demo")),
        tmp_path / "demo.yaml",
        input_func=lambda prompt="": "bogus",
        output=output.append,
    )
    # The public loop stops on EOF; exercise one line then quit.
    responses = iter(["bogus", "quit"])
    shell._input = lambda prompt="": next(responses)
    shell.run()
    assert any("unknown command" in line for line in output)


@pytest.mark.parametrize("addr", ["localhost:99999", "localhost:-1", "localhost:0"])
def test_check_server_rejects_out_of_range_ports_without_crashing(addr):
    from satgonetem.cli.main import _check_server

    check = _check_server(addr)
    assert check.ok is False
    assert "port" in check.detail.lower() or "invalid" in check.detail.lower()


def test_check_server_strips_brackets_for_ipv6_literal(monkeypatch):
    import socket

    from satgonetem.cli.main import _check_server

    seen = []

    class _DummySock:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_connect(addr, timeout=None):
        seen.append(addr)
        return _DummySock()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    check = _check_server("[::1]:10110")
    assert check.ok is True
    assert seen == [("::1", 10110)]
