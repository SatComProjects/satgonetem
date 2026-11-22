"""Tests for the interactive scenario shell's validation."""

from __future__ import annotations

import pytest

from satgonetem.experiment import Scenario, ScenarioError
from satgonetem.experiment.interactive import (
    ScenarioDraft,
    ScenarioShell,
    default_scenario,
)


def _shell(tmp_path) -> ScenarioShell:
    return ScenarioShell(
        ScenarioDraft.from_scenario(default_scenario("demo")),
        tmp_path / "demo.yaml",
        output=lambda message: None,
    )


@pytest.mark.parametrize(
    "parts, message",
    [
        (["traffic", "add", "Gnd0", "Gnd1", "ping", "start=-5"], "start_s"),
        (["traffic", "add", "", "Gnd1", "ping"], "src"),
        (["gnd", "add", "", "0", "0"], "name"),
        (["shell", "add", "", "3", "3", "53", "15"], "identifier"),
    ],
)
def test_shell_rejects_invalid_specs(tmp_path, parts, message):
    shell = _shell(tmp_path)

    with pytest.raises(ScenarioError, match=message):
        shell._dispatch(parts)


def test_shell_save_round_trips(tmp_path):
    shell = _shell(tmp_path)
    shell._dispatch(["traffic", "add", "Gnd0", "Gnd1", "ping", "start=1"])
    shell._dispatch(["gnd", "add", "Berlin", "52.52", "13.405"])
    shell._dispatch(
        ["event", "add", "action=impairment", "at=2", "src=Sat0", "dst=Sat1", "loss=5"]
    )
    shell._dispatch(["save"])

    reloaded = Scenario.from_file(tmp_path / "demo.yaml")
    assert reloaded.traffic[0].start_s == 1.0
    assert reloaded.ground_stations[0].name == "Berlin"
    assert reloaded.events[0].loss_percent == 5.0
