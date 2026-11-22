"""GoNetEm project/YAML generation extracted from the launcher."""

from __future__ import annotations

import json
import os
import tarfile
import tempfile

import yaml

# YAML treats these as line breaks even inside double-quoted scalars, but
# json.dumps leaves them raw. Go's yaml.v2/v3 then normalizes them to spaces
# while parsing, silently changing the value.
_YAML_LINE_BREAK_ESCAPES = {
    "\u0085": "\\u0085",  # NEL
    "\u2028": "\\u2028",  # LINE SEPARATOR
    "\u2029": "\\u2029",  # PARAGRAPH SEPARATOR
}


def _yaml_scalar(value: object) -> str:
    """Return *value* as a single-line, YAML-safe scalar.

    Plain values stay unquoted so the generated document keeps the shape the
    GoNetEm server expects. The value is validated in a flow mapping because
    peers are emitted inside ``{peer1: ..., peer2: ...}``. Anything YAML would
    reinterpret there (newlines, comments, mapping separators, flow
    indicators, anchor/alias indicators, type coercion) is emitted as a
    JSON-quoted scalar, which is also valid YAML.

    ``ensure_ascii=False`` keeps non-BMP characters literal: Go's yaml.v2/v3
    rejects the UTF-16 surrogate-pair escapes that ``json.dumps`` produces by
    default. NEL, LINE SEPARATOR, and PARAGRAPH SEPARATOR are escaped as
    ``\\uXXXX`` because YAML parses them as line breaks even in quoted scalars.
    """
    text = str(value)
    if not any(char in text for char in _YAML_LINE_BREAK_ESCAPES):
        try:
            parsed = yaml.safe_load(f"{{value: {text}}}")
        except yaml.YAMLError:
            parsed = None
        if parsed == {"value": text}:
            return text
    quoted = json.dumps(text, ensure_ascii=False)
    for char, escape in _YAML_LINE_BREAK_ESCAPES.items():
        quoted = quoted.replace(char, escape)
    return quoted


def _tbf_qos(rate_kbps: int, delay_ms: int) -> tuple[int, float]:
    """Return TBF burst bytes and BDP multiplier for a link.

    gonetem computes ``limit = buffer * rate * delay / 8``. Choosing the
    buffer factor as ``(burst + BDP) / BDP`` makes the resulting limit equal
    to ``burst + BDP`` without changing the server.
    """
    rate_kbps = int(rate_kbps)
    delay_ms = max(int(delay_ms), 1)
    if rate_kbps <= 0:
        return 0, 0.0

    rate_bytes_per_sec = rate_kbps * 125
    burst = rate_bytes_per_sec // 250 + 1514
    bdp_bytes = rate_kbps * delay_ms / 8.0
    buffer_factor = (burst + bdp_bytes) / bdp_bytes
    return burst, buffer_factor


class GoNetEmProjectBuilder:
    """Build GoNetEm network.yml content and .gnet archives."""

    def __init__(self, launcher) -> None:
        self._launcher = launcher
        self._topology_manager = launcher._topology_manager
        self.isl_capacity_kbps = launcher.isl_capacity_kbps
        self.gnd_capacity_kbps = launcher.gnd_capacity_kbps
        self.ground_object_capacity_kbps = launcher.ground_object_capacity_kbps
        self.netem_limit = launcher.netem_limit
        self.txqueuelen = launcher.txqueuelen

    # Project file generation

    def get_gonetem_topology(
        self,
        fallback: bool = False,
        controller: bool = False,
        nodes: list | None = None,
        links: list | None = None,
    ) -> str:
        """Build the GoNetem network.yml content from the current topology.

        Args:
            fallback: Use host networking instead of the Docker images.
            controller: Add the controller switch and its links.
            nodes: Optional filtered node set. When provided, only these
                satellites/ground stations (and links between them) are
                serialized. HIL-managed ground stations are excluded by the
                caller.
            links: Optional filtered link set. When omitted, all topology
                links between the selected nodes are used.
        """
        tm = self._topology_manager
        sat_type = "docker.host" if fallback else "docker.satellite"
        gs_type = "docker.host" if fallback else "docker.gndstation"

        if nodes is None:
            satellite_nodes = list(tm.satellites.values())
            ground_station_nodes = list(tm.ground_stations.values())
            allowed_names = None
        else:
            allowed_names = {node.name for node in nodes}
            satellite_nodes = [
                sat for sat in tm.satellites.values() if sat.name in allowed_names
            ]
            ground_station_nodes = [
                gs for gs in tm.ground_stations.values() if gs.name in allowed_names
            ]

        link_nodes = tm.links.values() if links is None else links
        if allowed_names is not None:
            link_nodes = [
                link
                for link in link_nodes
                if link.source.name in allowed_names
                and link.target.name in allowed_names
            ]

        satellites = self._write_satellites(satellite_nodes, sat_type)
        groundStations = self._write_ground_stations(
            ground_station_nodes, gs_type, fallback=fallback
        )
        controller_section = self._write_controller_switch() if controller else ""
        link_section = self._write_links(link_nodes)
        controller_links = (
            self._write_controller_switch_links(satellite_nodes, ground_station_nodes)
            if controller
            else ""
        )

        return (
            "nodes: \n"
            + satellites
            + groundStations
            + controller_section
            + "links: \n"
            + link_section
            + controller_links
        )

    def _controller_iface_suffix(self, node) -> str:
        """Return the GoNetEm peer suffix for a node controller interface."""
        pattern = getattr(
            self._topology_manager, "controller_node_iface_pattern", "eth{node_id}"
        )
        iface = pattern.format(node_id=50000 + node.id)
        if not iface.startswith("eth") or not iface[3:].isdigit():
            raise ValueError(
                "controller_node_iface_pattern must format to 'eth<digits>', "
                f"got {iface!r}"
            )
        return iface[3:]

    def _write_controller_switch_links(self, satellites, ground_stations) -> str:
        # Link from every node to the controller switch with high capacity and low latency
        result = ""
        for sat in satellites:
            eth_id = self._controller_iface_suffix(sat)
            burst, buffer = _tbf_qos(1_000_000, 1)
            peer1 = _yaml_scalar(f"Controller.{sat.id}")
            peer2 = _yaml_scalar(f"{sat.name}.{eth_id}")
            result += (
                f" - {{peer1: {peer1}, peer2: {peer2},"
                f" peer1qos: {{ rate: 1000000, buffer: {buffer}, jitter: 0, delay: 1, burst: {burst}}},"
                f" peer2qos: {{ rate: 1000000, buffer: {buffer}, jitter: 0, delay: 1, burst: {burst}}}}} \n"
            )
        for gs in ground_stations:
            eth_id = self._controller_iface_suffix(gs)
            burst, buffer = _tbf_qos(1_000_000, 1)
            peer1 = _yaml_scalar(f"Controller.{gs.id}")
            peer2 = _yaml_scalar(f"{gs.name}.{eth_id}")
            result += (
                f" - {{peer1: {peer1}, peer2: {peer2},"
                f" peer1qos: {{ rate: 1000000, buffer: {buffer}, jitter: 0, delay: 1, burst: {burst}}},"
                f" peer2qos: {{ rate: 1000000, buffer: {buffer}, jitter: 0, delay: 1, burst: {burst}}}}} \n"
            )
        return result

    @staticmethod
    def _write_controller_switch() -> str:
        return " Controller:\n  type: ovs\n  volumes:\n  - /tmp:/tmp\n"

    @staticmethod
    def _write_satellites(satellites, sat_type: str) -> str:
        result = ""
        for sat in satellites:
            image = "jariassuarez/sgnt:satellite" if not sat.image else sat.image
            key = _yaml_scalar(f"Sat{sat.id}")
            result += (
                f" {key}:\n  type: {_yaml_scalar(sat_type)}\n"
                f"  image: {_yaml_scalar(image)}\n  volumes:\n  - /tmp:/tmp\n"
            )
        return result

    @staticmethod
    def _write_ground_stations(
        ground_stations, gs_type: str, fallback: bool = False
    ) -> str:
        result = ""
        for gs in ground_stations:
            prefix = "Gnd" if gs.type == "GroundStation" else "Usr"
            image = gs.image
            if not image and not fallback:
                image = "jariassuarez/sgnt:ground-station"
            key = _yaml_scalar(f"{prefix}{gs.id}")
            result += (
                f" {key}:\n  type: {_yaml_scalar(gs_type)}\n"
                f"  image: {_yaml_scalar(image)}\n  volumes:\n  - /tmp:/tmp\n"
            )
        return result

    def _write_links(self, links) -> str:
        result = ""
        for link in links:
            if not getattr(link, "is_active", True):
                continue
            delay = max(int(link.delay), 1)
            rate = link.get_capacity()
            burst, buffer = _tbf_qos(rate, delay)
            queue = f"limit: {self.netem_limit}, txqueuelen: {self.txqueuelen}"
            peer1 = _yaml_scalar(f"{link.source.name}.{link.target.id}")
            peer2 = _yaml_scalar(f"{link.target.name}.{link.source.id}")
            result += (
                f" - {{peer1: {peer1},"
                f" peer2: {peer2},"
                f" peer1qos: {{ rate: {rate}, buffer: {buffer}, jitter: 0, delay: {delay}, burst: {burst}, {queue}}},"
                f" peer2qos: {{ rate: {rate}, buffer: {buffer}, jitter: 0, delay: {delay}, burst: {burst}, {queue}}}}}"
                f" #Latency of {delay}ms \n"
            )
        return result

    def generate_project(
        self,
        dest_dir: str,
        controller: bool = False,
        nodes: list | None = None,
        links: list | None = None,
    ) -> str:
        """Write a project.gnet archive to *dest_dir* and return its path.

        Args:
            dest_dir: Directory for the generated archive.
            controller: Add the controller switch and its links.
            nodes: Optional filtered node set passed to
                :meth:`get_gonetem_topology`.
            links: Optional filtered link set passed to
                :meth:`get_gonetem_topology`.
        """

        data = self._launcher.get_gonetem_topology(
            controller=controller, nodes=nodes, links=links
        )
        with tempfile.TemporaryDirectory() as tmp:
            network_yml = os.path.join(tmp, "network.yml")
            with open(network_yml, "w") as network_file:
                network_file.write(data)
            fd, gnet_path = tempfile.mkstemp(suffix=".gnet", dir=dest_dir)
            os.close(fd)
            with tarfile.open(gnet_path, "w:gz") as tar:
                tar.add(network_yml, arcname="network.yml")
        return gnet_path
