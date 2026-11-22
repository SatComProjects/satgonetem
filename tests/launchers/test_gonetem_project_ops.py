"""Behavioral tests for GoNetEm topology/project-file generation."""

from __future__ import annotations

import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from satgonetem.launchers.gonetem_launcher import GoNetEmLauncher


class FakeLink:
    """Minimal link object accepting the fields used by the project builder."""

    def __init__(self, source, target, *, delay=10, capacity=1000, is_active=True):
        self.source = source
        self.target = target
        self.delay = delay
        self.capacity = capacity
        self.is_active = is_active

    def get_capacity(self):
        return self.capacity


def _satellite(node_id: int, name: str, image: str = ""):
    return SimpleNamespace(id=node_id, name=name, image=image)


def _ground_station(node_id: int, name: str, image: str = "", type_="GroundStation"):
    return SimpleNamespace(id=node_id, name=name, image=image, type=type_)


def _topology(satellites, ground_stations, links):
    return SimpleNamespace(
        satellites=satellites,
        ground_stations=ground_stations,
        links=links,
    )


def _launcher(topology_manager) -> GoNetEmLauncher:
    return GoNetEmLauncher(topology_manager, "localhost:10110", "test-project")


def test_topology_without_filters_serializes_all_nodes_and_active_links():
    sat0 = _satellite(0, "Sat0")
    sat1 = _satellite(1, "Sat1")
    gnd = _ground_station(2, "Gnd0", image="gnd-image")
    active = FakeLink(sat0, gnd, capacity=2000)
    inactive = FakeLink(sat1, gnd, is_active=False)
    topology_manager = _topology(
        {sat0.id: sat0, sat1.id: sat1},
        {gnd.id: gnd},
        {"active": active, "inactive": inactive},
    )
    launcher = _launcher(topology_manager)

    yaml = launcher.get_gonetem_topology()

    assert "Sat0:" in yaml
    assert "Sat1:" in yaml
    assert "Gnd2:" in yaml
    assert "type: docker.satellite" in yaml
    assert "type: docker.gndstation" in yaml
    assert "image: gnd-image" in yaml
    assert "peer1: Sat0.2, peer2: Gnd0.0" in yaml
    assert "peer1: Sat1.2" not in yaml
    assert yaml.count("#Latency of") == 1
    assert "Controller" not in yaml


def test_topology_controller_adds_switch_and_links_for_every_node():
    sat = _satellite(7, "SatA")
    gnd = _ground_station(3, "GndB")
    topology_manager = _topology(
        {sat.id: sat},
        {gnd.id: gnd},
        {},
    )
    launcher = _launcher(topology_manager)

    yaml = launcher.get_gonetem_topology(controller=True)

    assert " Controller:\n  type: ovs\n" in yaml
    assert yaml.count("peer1: Controller.") == 2
    assert "Controller.7" in yaml
    assert "SatA.50007" in yaml
    assert "Controller.3" in yaml
    assert "GndB.50003" in yaml
    # controller links use a 1 Gbit/s TBF with a 1 ms delay
    assert yaml.count("burst: 501514") == 4
    assert yaml.count("delay: 1") == 4


def test_topology_controller_honours_custom_interface_pattern():
    sat = _satellite(7, "SatA")
    topology_manager = _topology({sat.id: sat}, {}, {})
    topology_manager.controller_node_iface_pattern = "eth9{node_id}"
    launcher = _launcher(topology_manager)

    yaml = launcher.get_gonetem_topology(controller=True)

    assert "SatA.950007" in yaml
    assert "SatA.50007" not in yaml


@pytest.mark.parametrize(
    ("pattern", "iface"),
    [
        ("veth{node_id}", "veth50007"),
        ("ethX{node_id}", "ethX50007"),
    ],
)
def test_topology_controller_rejects_non_eth_digit_interface(pattern, iface):
    sat = _satellite(7, "SatA")
    topology_manager = _topology({sat.id: sat}, {}, {})
    topology_manager.controller_node_iface_pattern = pattern
    launcher = _launcher(topology_manager)

    with pytest.raises(ValueError) as excinfo:
        launcher.get_gonetem_topology(controller=True)

    message = str(excinfo.value)
    assert "must format to 'eth<digits>'" in message
    assert f"got {iface!r}" in message


def test_generate_project_writes_network_yml_archive(tmp_path):
    sat = _satellite(0, "Sat0")
    gnd = _ground_station(2, "Gnd0", image="gnd-image")
    link = FakeLink(sat, gnd, capacity=2000)
    topology_manager = _topology(
        {sat.id: sat},
        {gnd.id: gnd},
        {"sat-gnd": link},
    )
    launcher = _launcher(topology_manager)

    project_file = launcher.generate_project(
        str(tmp_path), controller=True, nodes=[sat, gnd]
    )

    path = Path(project_file)
    assert path.parent == tmp_path
    assert path.suffix == ".gnet"
    with tarfile.open(path, "r:gz") as archive:
        assert archive.getnames() == ["network.yml"]
        network_file = archive.extractfile("network.yml")
        assert network_file is not None
        content = network_file.read().decode()

    assert " Controller:\n  type: ovs\n" in content
    assert "Sat0:" in content
    assert "Gnd2:" in content
    assert "peer1: Sat0.2, peer2: Gnd0.0" in content
    assert "Controller.0" in content
