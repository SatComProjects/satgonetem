"""
Bird OSPF routing daemon.

All OSPF logic lives here. The daemon writes a declarative Bird2 config
directly to each container and manages the Bird service lifecycle.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from satgonetem.routing.bird_daemon import BirdRoutingDaemon
from satgonetem.routing.bird_service import apply_bird_config
from satgonetem.routing.commands import assign_ips

if TYPE_CHECKING:
    from satgonetem.models.node import Node

OSPF_AREA = 0
OSPF_HELLO_INTERVAL = 1
OSPF_DEAD_COUNT = 3


class OSPFDaemon(BirdRoutingDaemon):
    """Bird2-based OSPF routing daemon.

    Writes a complete declarative Bird2 OSPF configuration to each container
    and starts the Bird service. When new links are added, rebuilds the config
    only for the directly affected nodes (Bird requires a full reload).
    """

    def _init_node(self, node: "Node") -> None:
        """Assign IPs, build Bird OSPF config, and start Bird on one node.

        Args:
            node: The node to configure.
        """
        _assign_ips(node)
        config = _build_ospf_config(node)
        _apply_config(node, config)


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
    """Install a Bird config and start or reload Bird in the container.

    Args:
        node: The node that will run Bird.
        config: Complete Bird2 configuration as a string.
    """
    apply_bird_config(node, config)


_assign_ips = assign_ips
