"""Characterization tests for DiagnosticsMixin lookup and reporting helpers."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from satgonetem.services.mixins.diagnostics import DiagnosticsMixin
from tests.support import DummyTopology, add_interface

pytestmark = pytest.mark.unit


class _DiagnosticsHost(DiagnosticsMixin, DummyTopology):
    """Lightweight topology host exposing DiagnosticsMixin."""


@pytest.fixture
def diagnostics_host() -> _DiagnosticsHost:
    return _DiagnosticsHost()


@pytest.fixture
def populated_topology(diagnostics_host: _DiagnosticsHost) -> _DiagnosticsHost:
    diagnostics_host.add_satellite("Sat0")
    diagnostics_host.add_satellite("Sat1")
    diagnostics_host.add_ground_station("Gnd0")
    diagnostics_host.add_ground_station("Gnd2")
    return diagnostics_host


# ----------------------------------------------------------------------
# Interface usage files
# ----------------------------------------------------------------------


def test_process_interface_file_returns_empty_dict_for_empty_file(
    diagnostics_host: _DiagnosticsHost, tmp_path
) -> None:
    path = tmp_path / "interfaces.log"
    path.write_text("")

    assert diagnostics_host._process_interface_file(str(path)) == {}


def test_process_interface_file_returns_empty_dict_for_blank_last_line(
    diagnostics_host: _DiagnosticsHost, tmp_path
) -> None:
    path = tmp_path / "interfaces.log"
    path.write_text('{"eth0": 656}\n\n')

    assert diagnostics_host._process_interface_file(str(path)) == {}


def test_process_interface_file_returns_empty_dict_for_malformed_json(
    diagnostics_host: _DiagnosticsHost, tmp_path
) -> None:
    path = tmp_path / "interfaces.log"
    path.write_text('{"eth0": 656}\nnot-json\n')

    assert diagnostics_host._process_interface_file(str(path)) == {}


def test_process_interface_file_returns_only_the_last_state(
    diagnostics_host: _DiagnosticsHost, tmp_path
) -> None:
    path = tmp_path / "interfaces.log"
    historical = {"eth0": 1, "eth55": 2}
    current = {"eth0": 656, "eth55": 272974304}
    path.write_text(
        json.dumps(historical) + "\n" + json.dumps(current) + "\n"
    )

    assert diagnostics_host._process_interface_file(str(path)) == current


def test_process_interface_file_raises_for_missing_file(
    diagnostics_host: _DiagnosticsHost, tmp_path
) -> None:
    with pytest.raises(FileNotFoundError):
        diagnostics_host._process_interface_file(str(tmp_path / "missing.log"))


# ----------------------------------------------------------------------
# Node lookup
# ----------------------------------------------------------------------


def test_get_node_by_name_returns_satellites_and_ground_stations(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology

    assert topology.get_node_by_name("Sat1") is topology.satellites[1]
    assert topology.get_node_by_name("Gnd2") is topology.ground_stations[2]


@pytest.mark.parametrize("name", ["Sat9", "Gnd9", "Foo0", ""])
def test_get_node_by_name_returns_none_for_unknown_names(
    populated_topology: _DiagnosticsHost, name: str
) -> None:
    assert populated_topology.get_node_by_name(name) is None


def test_get_node_by_id_prefers_satellites_on_id_collision(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology

    # Satellites and ground stations are separate dicts; both may contain ID 0.
    assert topology.get_node_by_id(0) is topology.satellites[0]
    assert topology.get_node_by_id(2) is topology.ground_stations[2]
    assert topology.get_node_by_id(99) is None


# ----------------------------------------------------------------------
# Node address/interface reporting
# ----------------------------------------------------------------------


def test_get_node_ip_addresses_returns_only_addressed_link_interfaces(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology
    satellite = topology.satellites[0]
    station = topology.ground_stations[0]

    add_interface(satellite, "Sat0.7", ipv4="10.128.0.4")
    add_interface(satellite, "Sat0.8")
    add_interface(satellite, "Sat0.9", ipv4="10.128.0.6")
    add_interface(station, "Gnd0.5", ipv4="10.0.0.5")

    assert topology.get_node_ip_addresses("Sat0") == [
        "10.128.0.4",
        "10.128.0.6",
    ]
    assert topology.get_node_ip_addresses("Gnd0") == ["10.0.0.5"]
    assert topology.get_node_ip_addresses("Sat9") == []


def test_get_node_ip_addresses_excludes_the_loopback(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology
    satellite = topology.satellites[0]
    satellite.loopback.set_ipv4("10.128.0.1", 32)
    add_interface(satellite, "Sat0.7", ipv4="10.128.0.4")

    # Only node.interfaces is inspected; the node loopback is tracked
    # separately on TopologyManager.interfaces.
    assert topology.get_node_ip_addresses("Sat0") == ["10.128.0.4"]


def test_get_node_ifaces_returns_only_named_and_addressed_interfaces(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology
    satellite = topology.satellites[0]

    add_interface(satellite, "Sat0.7", ipv4="10.128.0.4")
    add_interface(satellite, "Sat0.8")
    add_interface(satellite, "Sat0.9", ipv4="10.128.0.6")

    assert topology.get_node_ifaces("Sat0") == ["Sat0.7", "Sat0.9"]
    assert topology.get_node_ifaces("Gnd0") == []
    assert topology.get_node_ifaces("Sat9") == []


# ----------------------------------------------------------------------
# Topology summary
# ----------------------------------------------------------------------


def test_get_topology_summary_for_empty_topology(
    diagnostics_host: _DiagnosticsHost,
) -> None:
    assert diagnostics_host.get_topology_summary() == {
        "satellites": 0,
        "ground_stations": 0,
        "links": 0,
    }


def test_get_topology_summary_counts_entries(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology
    topology.add_link(topology.satellites[0], topology.satellites[1])
    topology.add_link(topology.satellites[0], topology.ground_stations[0])

    assert topology.get_topology_summary() == {
        "satellites": 2,
        "ground_stations": 2,
        "links": 2,
    }


# ----------------------------------------------------------------------
# Coverage percentage
# ----------------------------------------------------------------------


def test_get_coverage_percentage_returns_zero_without_satellites(
    diagnostics_host: _DiagnosticsHost,
) -> None:
    assert diagnostics_host.get_coverage_percentage() == 0.0


def test_get_coverage_percentage_delegates_to_fast_metric(
    populated_topology: _DiagnosticsHost,
) -> None:
    topology = populated_topology
    expected_sats = list(topology.satellites.values())

    with patch(
        "satgonetem.services.mixins.diagnostics.coverage_percentage_fast",
        return_value=37.5,
    ) as metric:
        result = topology.get_coverage_percentage(
            elev_min_deg=5.0,
            grid_res_deg=2.0,
            max_latitude_deg=45.0,
            R_earth_km=6000.0,
        )

    assert result == 37.5
    metric.assert_called_once_with(
        sats=expected_sats,
        elev_min_deg=5.0,
        grid_res_deg=2.0,
        max_latitude_deg=45.0,
        R_earth_km=6000.0,
    )


def test_get_coverage_percentage_forwards_documented_defaults(
    populated_topology: _DiagnosticsHost,
) -> None:
    with patch(
        "satgonetem.services.mixins.diagnostics.coverage_percentage_fast",
        return_value=12.0,
    ) as metric:
        populated_topology.get_coverage_percentage()

    assert metric.call_args.kwargs["elev_min_deg"] == 10.0
    assert metric.call_args.kwargs["grid_res_deg"] == 1.0
    assert metric.call_args.kwargs["max_latitude_deg"] == 90.0
    assert metric.call_args.kwargs["R_earth_km"] == 6_371.0


def test_get_coverage_percentage_reports_a_percentage_for_overhead_satellite(
    diagnostics_host: _DiagnosticsHost,
) -> None:
    diagnostics_host.add_satellite(
        "Sat0", latitude=0.0, longitude=0.0, altitude_km=550.0
    )

    result = diagnostics_host.get_coverage_percentage(
        elev_min_deg=10.0,
        grid_res_deg=10.0,
        max_latitude_deg=10.0,
    )

    assert isinstance(result, float)
    assert 0.0 < result <= 100.0


def test_open_bash_terminal_uses_injected_process_runner(
    diagnostics_host: _DiagnosticsHost, monkeypatch
) -> None:
    from unittest.mock import MagicMock

    diagnostics_host.add_satellite("Sat0")
    diagnostics_host.project_id = "project-1"
    diagnostics_host.gonetem_server = "localhost:10110"
    diagnostics_host.process_runner = MagicMock()

    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/xterm" if name == "xterm" else None
    )

    diagnostics_host.open_bash_terminal("Sat0")

    diagnostics_host.process_runner.popen.assert_called_once()
    command = diagnostics_host.process_runner.popen.call_args.args[0]
    assert command[0] == "xterm"
    assert "satgonetem.utils.node_terminal_bridge" in command
