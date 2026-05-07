"""Bird IS-IS SR-MPLS routing daemon.

All IS-IS + Segment Routing MPLS logic lives here. The daemon writes a
declarative Bird2 config directly to each container and manages the Bird
service lifecycle. MPLS kernel modules and sysctls are configured before
Bird is started.
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

ISIS_HELLO_INTERVAL = 1
ISIS_HELLO_MULTIPLIER = 3
SR_GLOBAL_BLOCK_LOW = 16000
SR_GLOBAL_BLOCK_HIGH = 23999


class ISISBirdSRDaemon(RoutingDaemon):
    """Bird2-based IS-IS SR-MPLS routing daemon.

    Writes a complete declarative Bird2 IS-IS + SR-MPLS configuration to each
    container, enables kernel MPLS forwarding, and starts the Bird service.
    When new links are added, rebuilds the config for directly affected nodes.
    """

    def init(self, max_workers: int = 4) -> bool:
        """Start Bird IS-IS SR-MPLS on every satellite and ground station.

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
                    logging.error(f"IS-IS SR init failed on {n.name}: {e}")
        return errors == 0

    def update(self, new_links: "List[Link]", max_workers: int = 4) -> None:
        """Rebuild Bird IS-IS SR config on nodes touched by new links.

        Bird uses a single declarative config file, so adding an interface
        requires a full config rebuild and reload on the affected node.

        Args:
            new_links: Links added in the latest topology change.
            max_workers: Accepted for interface consistency.
        """
        if not self.topology.status:
            logging.warning("IS-IS SR update skipped: topology not yet active")
            return

        affected = {link.source for link in new_links} | {
            link.target for link in new_links
        }
        for node in affected:
            try:
                self._init_node(node)
            except Exception as e:
                logging.error(f"IS-IS SR update failed on {node.name}: {e}")

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
        """Enable MPLS, assign IPs, build Bird IS-IS SR config, and start Bird.

        Args:
            node: The node to configure.
        """
        _enable_mpls(node)
        _assign_ips(node)
        config = _build_isis_sr_config(node)
        _apply_config(node, config)

    def _stop_node(self, node: "Node") -> None:
        """Stop the Bird service inside a node's container.

        Args:
            node: The node on which Bird should be stopped.
        """
        node.execute_command("service bird stop")


def _format_net_address(loopback_ip: str) -> str:
    """Format an IS-IS NET address from a loopback IPv4 address.

    Pads each octet to 3 digits, concatenates, then groups as XXXX.XXXX.XXXX.

    Args:
        loopback_ip: IPv4 address string (e.g. "128.33.0.133").

    Returns:
        Full NET string (e.g. "49.0001.1280.3300.0133.00").
    """
    parts = [f"{int(p):03d}" for p in loopback_ip.split(".")]
    full_str = "".join(parts)
    system_id = f"{full_str[0:4]}.{full_str[4:8]}.{full_str[8:12]}"
    return f"49.0001.{system_id}.00"


def _enable_mpls(node: "Node") -> None:
    """Load kernel MPLS modules and enable MPLS forwarding on a node.

    Loads mpls_router and mpls_iptunnel modules, sets the platform label
    space size, and enables MPLS input on loopback and every interface.

    Args:
        node: The node whose MPLS forwarding should be enabled.
    """
    commands = [
        "modprobe mpls_router 2>/dev/null || true",
        "modprobe mpls_iptunnel 2>/dev/null || true",
        "sysctl -w net.mpls.platform_labels=1048575",
        "sysctl -w net.mpls.conf.lo.input=1",
    ]
    for iface in node.interfaces:
        commands.append(f"sysctl -w net.mpls.conf.{iface.get_iname()}.input=1")
    node.execute_command("; ".join(commands))


def _build_isis_sr_config(node: "Node") -> str:
    """Build a complete Bird2 IS-IS + SR-MPLS configuration string for one node.

    The configuration includes:
    - Global settings (router id, hostname, log)
    - Base protocols (device, direct, kernel)
    - MPLS label manager protocol
    - IS-IS protocol with SR-MPLS, SRGB, node prefix-SID, and per-interface blocks

    Args:
        node: The node whose loopback IP, node ID, and interfaces are used.

    Returns:
        A Bird2 configuration file as a string.
    """
    net = _format_net_address(node.loopback.ipv4)
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
        "protocol mpls {",
        f"    label range {SR_GLOBAL_BLOCK_LOW} 1048575;",
        "}",
        "",
        "protocol isis isis1 {",
        f'    net "{net}";',
        "    is-type level-2-only;",
        "",
        "    ipv4 {",
        "        export where source = RTS_DEVICE;",
        "    };",
        "",
        "    spf-delay normal 50 100 5000;",
        "    lsp-gen-interval 50 100 5000;",
        "",
        "    segment routing ipv4 {",
        f"        srgb range {SR_GLOBAL_BLOCK_LOW} {SR_GLOBAL_BLOCK_HIGH};",
        "        node-sid {",
        f"            prefix {node.loopback.ipv4}/32;",
        f"            index {node.id};",
        "        };",
        "    };",
        "",
        '    interface "lo" {',
        "        passive yes;",
        "        mpls yes;",
        "    };",
        "",
    ]
    for iface in node.interfaces:
        iname = iface.get_iname()
        peer_name = iface.peer.name if iface.peer else "unknown"
        if "Gnd" in peer_name:
            link_type = "GSL"
        elif "Sat" in peer_name:
            link_type = "ISL"
        else:
            link_type = "link"
        lines += [
            f'    interface "{iname}" {{',
            f"        # {link_type} to {peer_name}",
            "        type p2p;",
            f"        hello-interval {ISIS_HELLO_INTERVAL};",
            f"        hello-multiplier {ISIS_HELLO_MULTIPLIER};",
            "        bfd yes;",
            "        mpls yes;",
            "    };",
            "",
        ]
    lines += [
        "}",
    ]
    return "\n".join(lines)


def _apply_config(node: "Node", config: str) -> None:
    """Write a Bird IS-IS SR config to a temp file and start Bird in the container.

    Args:
        node: The node that will run Bird.
        config: Complete Bird2 configuration as a string.
    """
    tmpfile = f"/tmp/bird_isis_sr_{uuid.uuid4().hex}.conf"
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
