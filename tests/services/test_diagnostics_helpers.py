"""Characterization tests for DiagnosticsMixin lookup and reporting helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

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
    path.write_text(json.dumps(historical) + "\n" + json.dumps(current) + "\n")

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


def _node_host():
    host = _DiagnosticsHost()
    sat = host.add_satellite("Sat0")
    sat.container = object()
    sat.execute_command = MagicMock(return_value="")
    add_interface(sat, "Sat0.0", ipv4="10.0.0.1")
    host.gonetem_status = True
    host.get_gonetem_status = lambda: host.gonetem_status
    return host, sat


def test_launch_tcpdump_starts_one_process_per_interface(tmp_path):

    host, sat = _node_host()

    host.launch_tcpdump_on_satellites(dump_dir=str(tmp_path), satellites=[sat])

    command = sat.execute_command.call_args.args[0]
    assert "tcpdump -i eth0" in command
    assert str(tmp_path) in command
    assert sat.execute_command.call_args.kwargs["detach"] is True


def test_launch_tcpdump_skips_nodes_without_containers(caplog):
    host = _DiagnosticsHost()
    sat = host.add_satellite("Sat0")
    sat.container = None

    host.launch_tcpdump_on_satellites(satellites=[sat])

    assert "skipping tcpdump" in caplog.text


def test_is_running_tcpdump_handles_success_and_failure():

    host, sat = _node_host()
    sat.execute_command.return_value = "123"
    assert host.is_running_tcpdump() is True

    sat.execute_command.side_effect = RuntimeError("not running")
    assert host.is_running_tcpdump() is False


def test_enable_and_disable_tcpdump_respect_status_and_kill():
    from unittest.mock import patch

    host, sat = _node_host()
    host.gonetem_status = False
    host.enable_tcpdump_on_satellites(satellites=[sat])
    host.disable_tcpdump_on_satellites(satellites=[sat])
    sat.execute_command.assert_not_called()

    host.gonetem_status = True
    with patch.object(host, "is_running_tcpdump", return_value=False):
        with patch.object(host, "launch_tcpdump_on_satellites") as launch:
            host.enable_tcpdump_on_satellites(satellites=[sat])
            launch.assert_called_once_with(dump_dir="/tmp/dump", satellites=[sat])

    host.disable_tcpdump_on_satellites(satellites=[sat])
    assert sat.execute_command.call_args.args[0] == "pkill tcpdump"


def test_execute_command_on_resolves_nodes_and_reports_errors():

    host, sat = _node_host()
    sat.execute_command.return_value = "ok"

    assert host.execute_command_on("Sat0", "uptime") == {
        "output": "ok",
        "error": "",
    }

    sat.execute_command.side_effect = RuntimeError("boom")
    assert host.execute_command_on("Sat0", "uptime")["error"] == "boom"
    assert "not found" in host.execute_command_on("Sat99", "uptime")["error"]
    assert "Invalid node name" in host.execute_command_on("bad", "uptime")["error"]

    sat.container = None
    assert "no container" in host.execute_command_on("Sat0", "uptime")["error"]


def test_monitor_links_maps_interface_usage_to_links(monkeypatch):
    from types import SimpleNamespace

    from tests.support import make_link

    host = _DiagnosticsHost(project_name="proj")
    sat0 = host.add_satellite("Sat0")
    sat1 = host.add_satellite("Sat1")
    sat0.satcom_object = SimpleNamespace(topology_uniq_id=0)
    sat1.satcom_object = SimpleNamespace(topology_uniq_id=1)
    link = make_link(sat0, sat1)
    host.links = {frozenset(("Sat0", "Sat1")): link}
    host._build_link_key = lambda source, target: frozenset(("Sat0", "Sat1"))

    monkeypatch.setattr(
        host,
        "_process_interface_file",
        lambda path: {"proj.Sat1": 1234} if path.endswith("Sat0") else {},
    )

    result = host.monitor_links(debug=True)

    assert result == [
        {
            "src": "Sat0",
            "dst": "Sat1",
            "value": 1234,
            "type": link.type,
        }
    ]
    assert link.tx == 1234
    assert link.rx == 0


def test_monitor_links_returns_empty_for_empty_topology(diagnostics_host):
    assert diagnostics_host.links == {}

    assert diagnostics_host.monitor_links() == []


def test_monitor_links_uses_a_single_satellite_snapshot(monkeypatch):
    """Paths and worker names must come from the same topology snapshot."""
    from types import SimpleNamespace

    from tests.support import make_link

    host = _DiagnosticsHost(project_name="proj")
    sat0 = host.add_satellite("Sat0")
    sat1 = host.add_satellite("Sat1")
    sat0.satcom_object = SimpleNamespace(topology_uniq_id=0)
    sat1.satcom_object = SimpleNamespace(topology_uniq_id=1)
    host.links = {frozenset(("Sat0", "Sat1")): make_link(sat0, sat1)}
    host._build_link_key = lambda source, target: frozenset(("Sat0", "Sat1"))

    snapshots = [[sat0, sat1], []]
    host.get_satellites = lambda: snapshots.pop(0)

    monkeypatch.setattr(
        host,
        "_process_interface_file",
        lambda path: {"proj.Sat1": 1234} if path.endswith("Sat0") else {},
    )

    result = host.monitor_links()

    assert result == [
        {
            "src": "Sat0",
            "dst": "Sat1",
            "value": 1234,
            "type": "InterSatelliteLink",
        }
    ]


def test_node_terminal_sends_resizes_reads_and_closes():
    from types import SimpleNamespace

    from satgonetem.services.node_terminal import NodeTerminal

    class FakeStream:
        def __iter__(self):
            yield SimpleNamespace(code=0, data=b"stdout")  # STDOUT
            yield None  # sentinel? recv loop only iterates responses

    client = MagicMock()
    client.NodeExec.return_value = iter([SimpleNamespace(code=0, data=b"hello")])
    terminal = NodeTerminal(client, "project", "Sat0")

    with terminal:
        terminal.send("ls\n")
        terminal.resize(80, 24)
        code, data = terminal.read(timeout=0)

    assert code == "stdout"
    assert data == b"hello"
    terminal.close()
