"""Behavioral tests for GoNetEm topology/project-file generation."""

from __future__ import annotations

import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

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


def test_topology_quotes_injected_image_scalars():
    sat = _satellite(0, "Sat0", image="evil\n  injected: true")
    gnd = _ground_station(1, "Gnd0", image="evil: image\n  injected: true")
    link = FakeLink(sat, gnd, capacity=2000)
    topology_manager = _topology({sat.id: sat}, {gnd.id: gnd}, {"sat-gnd": link})
    launcher = _launcher(topology_manager)

    document = yaml.safe_load(launcher.get_gonetem_topology())

    assert set(document) == {"nodes", "links"}
    assert set(document["nodes"]) == {"Sat0", "Gnd1"}
    assert document["nodes"]["Sat0"]["image"] == "evil\n  injected: true"
    assert document["nodes"]["Gnd1"]["image"] == "evil: image\n  injected: true"
    assert len(document["links"]) == 1


def test_topology_quotes_injected_link_peer_names():
    sat = _satellite(0, "Sat0\nevil: true")
    gnd = _ground_station(1, "Gnd0\n  injected: true")
    link = FakeLink(sat, gnd, capacity=2000)
    topology_manager = _topology({sat.id: sat}, {gnd.id: gnd}, {"sat-gnd": link})
    launcher = _launcher(topology_manager)

    document = yaml.safe_load(launcher.get_gonetem_topology())

    assert set(document) == {"nodes", "links"}
    assert [link["peer1"] for link in document["links"]] == ["Sat0\nevil: true.1"]
    assert [link["peer2"] for link in document["links"]] == ["Gnd0\n  injected: true.0"]
    assert document["links"][0]["peer1qos"]["rate"] == 2000
    assert document["links"][0]["peer2qos"]["delay"] == 10


def test_topology_quotes_injected_names_in_controller_links():
    sat = _satellite(7, "SatA\ninjected: true")
    topology_manager = _topology({sat.id: sat}, {}, {})
    launcher = _launcher(topology_manager)

    document = yaml.safe_load(launcher.get_gonetem_topology(controller=True))

    assert set(document) == {"nodes", "links"}
    assert set(document["nodes"]) == {"Sat7", "Controller"}
    assert document["nodes"]["Controller"] == {
        "type": "ovs",
        "volumes": ["/tmp:/tmp"],
    }
    assert len(document["links"]) == 1
    assert document["links"][0]["peer1"] == "Controller.7"
    assert document["links"][0]["peer2"] == "SatA\ninjected: true.50007"


@pytest.mark.parametrize(
    "peer_name",
    [
        "Gnd1, injected",
        "Gnd1[injected]",
        "Gnd1{injected}",
    ],
)
def test_topology_quotes_flow_mapping_characters_in_peer_names(peer_name):
    sat = _satellite(0, peer_name)
    gnd = _ground_station(0, "Gnd0")
    link = FakeLink(sat, gnd, capacity=2000)
    topology_manager = _topology({sat.id: sat}, {gnd.id: gnd}, {"sat-gnd": link})
    launcher = _launcher(topology_manager)

    document = yaml.safe_load(launcher.get_gonetem_topology())

    assert set(document) == {"nodes", "links"}
    assert set(document["nodes"]) == {"Sat0", "Gnd0"}
    assert len(document["links"]) == 1
    assert set(document["links"][0]) == {"peer1", "peer2", "peer1qos", "peer2qos"}
    assert document["links"][0]["peer1"] == f"{peer_name}.0"
    assert document["links"][0]["peer2"] == "Gnd0.0"


def test_topology_quotes_flow_mapping_characters_in_controller_links():
    sat = _satellite(7, "SatA, injected")
    topology_manager = _topology({sat.id: sat}, {}, {})
    launcher = _launcher(topology_manager)

    document = yaml.safe_load(launcher.get_gonetem_topology(controller=True))

    assert set(document) == {"nodes", "links"}
    assert len(document["links"]) == 1
    assert set(document["links"][0]) == {"peer1", "peer2", "peer1qos", "peer2qos"}
    assert document["links"][0]["peer1"] == "Controller.7"
    assert document["links"][0]["peer2"] == "SatA, injected.50007"


def test_topology_quotes_non_bmp_scalars_without_surrogate_escapes():
    image = "evil: 😀"
    peer_name = "Sat😀\nevil: true"
    gnd_image = "gnd😀: image"
    sat = _satellite(0, peer_name, image=image)
    gnd = _ground_station(1, "Gnd0", image=gnd_image)
    link = FakeLink(sat, gnd, capacity=2000)
    topology_manager = _topology({sat.id: sat}, {gnd.id: gnd}, {"sat-gnd": link})
    launcher = _launcher(topology_manager)

    generated = launcher.get_gonetem_topology()
    document = yaml.safe_load(generated)

    assert "\\ud83d" not in generated.lower()
    assert "\\ude00" not in generated.lower()
    assert document["nodes"]["Sat0"]["image"] == image
    assert document["nodes"]["Gnd1"]["image"] == gnd_image
    assert document["links"][0]["peer1"] == f"{peer_name}.1"


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
