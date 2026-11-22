"""Tests for the satgonetem command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

from satgonetem.cli import main as cli
from satgonetem.cli.main import DoctorCheck, build_parser, main

TEST_TOPOLOGY = str(
    Path(__file__).resolve().parents[2] / "topology_files/test_topology.json"
)


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
