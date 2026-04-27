"""
Bird OSPF routing daemon.

All OSPF logic lives here. The daemon writes a declarative Bird2 config
directly to each container and manages the Bird service lifecycle.
"""
from __future__ import annotations

import base64
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, List, Optional

from satgonetem.routing.base_daemon import RoutingDaemon

if TYPE_CHECKING:
    from satgonetem.models.link import Link
    from satgonetem.models.node import Node

OSPF_AREA = 0
OSPF_HELLO_INTERVAL = 1
OSPF_DEAD_COUNT = 3


class OSPFDaemon(RoutingDaemon):
    """Bird2-based OSPF routing daemon.

    Writes a complete declarative Bird2 OSPF configuration to each container
    and starts the Bird service. When new links are added, rebuilds the config
    only for the directly affected nodes (Bird requires a full reload).
    """

    def init(self, max_workers: int = 4) -> bool:
        """Start Bird OSPF on every satellite and ground station.

        Args:
            max_workers: Maximum number of parallel worker threads.

        Returns:
            True if all nodes initialized successfully, False if any failed.
        """
        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        errors = 0
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._init_node, n): n for n in nodes}
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    errors += 1
                    logging.error(f"OSPF init failed on {n.name}: {e}")
        return errors == 0

    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
        """Rebuild Bird OSPF config on nodes touched by new links.

        Bird uses a single declarative config file, so adding an interface
        requires a full config rebuild and reload on the affected node.

        Args:
            new_links: Links added in the latest topology change.
            max_workers: Accepted for interface consistency; affected nodes
                are processed sequentially because they are typically few.
        """
        if not self.topology.status:
            logging.warning("OSPF update skipped: topology not yet active")
            return

        affected = {link.source for link in new_links} | {
            link.target for link in new_links
        }
        for node in affected:
            try:
                self._init_node(node)
            except Exception as e:
                logging.error(f"OSPF update failed on {node.name}: {e}")

    def remove(self, node: "Optional[Node]" = None, max_workers: int = 4) -> None:
        """Stop Bird on all nodes or on a single node.

        Args:
            node: If provided, only this node is affected. If None, all
                satellites and ground stations are processed in parallel.
            max_workers: Maximum number of parallel worker threads.
        """
        if node is not None:
            self._stop_node(node)
            return

        nodes = list(self.topology.get_satellites()) + list(
            self.topology.get_ground_stations()
        )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._stop_node, n): n for n in nodes}
            for fut in as_completed(futures):
                n = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    logging.error(f"Failed to stop Bird on {n.name}: {e}")

    def _init_node(self, node: "Node") -> None:
        """Assign IPs, build Bird OSPF config, and start Bird on one node.

        Args:
            node: The node to configure.
        """
        _assign_ips(node)
        config = _build_ospf_config(node)
        _apply_config(node, config)

    def _stop_node(self, node: "Node") -> None:
        """Stop the Bird service inside a node's container.

        Args:
            node: The node on which Bird should be stopped.
        """
        node.execute_command("service bird stop")


def _build_ospf_config(node: "Node") -> str:
    """Build a complete Bird2 OSPF configuration string for one node.

    Args:
        node: The node whose loopback IP and interfaces are used.

    Returns:
        A Bird2 configuration file as a string.
    """
    lines = [
        "log syslog all;",
        f"router id {node.loopback.ipv4};",
        f'hostname "{node.name}";',
        "",
        "protocol device {}",
        "",
        "protocol direct {",
        "    ipv4;",
        "}",
        "",
        "protocol kernel {",
        "    ipv4 {",
        "        export all;",
        "    };",
        "}",
        "",
        "protocol ospf v2 ospf1 {",
        "    ipv4 {",
        "        export where source = RTS_DEVICE;",
        "    };",
        f"    area {OSPF_AREA} {{",
        '        interface "lo" {',
        "            stub yes;",
        "        };",
    ]
    for iface in node.interfaces:
        lines += [
            f'        interface "{iface.get_iname()}" {{',
            f"            # link to {iface.peer.name}",
            "            type pointopoint;",
            f"            hello {OSPF_HELLO_INTERVAL};",
            f"            dead count {OSPF_DEAD_COUNT};",
            "        };",
        ]
    lines += [
        "    };",
        "}",
    ]
    return "\n".join(lines)


def _apply_config(node: "Node", config: str) -> None:
    """Write a Bird config to a temp file and start Bird in the container.

    Args:
        node: The node that will run Bird.
        config: Complete Bird2 configuration as a string.
    """
    tmpfile = f"/tmp/bird_ospf_{uuid.uuid4().hex}.conf"
    with open(tmpfile, "w") as f:
        f.write(config)
    node.execute_command(
        f"/usr/bin/start-service.py --service bird --bird-config {tmpfile}"
    )


def _assign_ips(node: "Node") -> None:
    """Synchronously assign interface IPv4 addresses before Bird starts.

    Uses ``ip addr replace`` (idempotent) so Bird's device and direct
    protocols can see the addresses on startup.

    Args:
        node: The node whose interfaces should have IPs assigned.
    """
    lines = []
    for iface in node.interfaces:
        ip = getattr(iface, "ipv4", None)
        if ip:
            lines.append(f"addr replace {ip}/31 dev {iface.get_iname()}")
    if getattr(node.loopback, "ipv4", None):
        lines.append(f"addr replace {node.loopback.ipv4}/32 dev lo")
    if not lines:
        return
    payload = "\n".join(lines)
    b64 = base64.b64encode(payload.encode()).decode()
    node.execute_command(f"echo {b64} | base64 -d | ip -force -batch -")
