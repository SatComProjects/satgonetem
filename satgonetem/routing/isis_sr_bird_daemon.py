"""Bird IS-IS SR-MPLS routing daemon.

All IS-IS + Segment Routing MPLS logic lives here. The daemon writes a
declarative Bird2 config directly to each container and manages the Bird
service lifecycle. MPLS kernel modules and sysctls are configured before
Bird is started.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

from satgonetem.routing.bird_daemon import BirdRoutingDaemon
from satgonetem.routing.bird_service import apply_bird_config
from satgonetem.routing.commands import (
    assign_ips,
    enable_mpls,
    format_net_address,
)

if TYPE_CHECKING:
    from satgonetem.models.node import Node

ISIS_HELLO_INTERVAL = 1
ISIS_HELLO_MULTIPLIER = 3
SR_GLOBAL_BLOCK_LOW = 16000
SR_GLOBAL_BLOCK_HIGH = 23999

_ISIS_PROBE_CONFIG = "protocol device {}\nprotocol mpls { label range 16000 23999; }\n"


def _bird_supports_isis(node: Node) -> bool:
    """Return whether *node*'s Bird build accepts IS-IS and MPLS config.

    Stock Bird 2.x ships neither protocol, so only a patched build accepts the
    generated template.

    Args:
        node: Node whose container should be probed.

    Returns:
        True when ``bird -p`` accepts the probe configuration.
    """
    payload = base64.b64encode(_ISIS_PROBE_CONFIG.encode("utf-8")).decode("ascii")
    probe = "/tmp/bird_isis_probe.conf"
    try:
        node.execute_command(
            f"printf %s '{payload}' | base64 -d > {probe} && "
            f"{{ bird -p -c {probe}; rc=$?; rm -f {probe}; exit $rc; }}"
        )
    except Exception as exc:
        logging.debug("Bird IS-IS capability probe failed: %s", exc)
        return False
    return True


class ISISBirdSRDaemon(BirdRoutingDaemon):
    """Bird2-based IS-IS SR-MPLS routing daemon.

    Writes a complete declarative Bird2 IS-IS + SR-MPLS configuration to each
    container, enables kernel MPLS forwarding, and starts the Bird service.
    When new links are added, rebuilds the config for directly affected nodes.
    """

    def _validate_nodes(self, nodes: list[Node]) -> bool:
        if nodes and not _bird_supports_isis(nodes[0]):
            logging.error(
                "IS-IS SR requires a Bird build with IS-IS and MPLS support; "
                "the selected image does not provide one. Use a patched Bird "
                "image or the 'dynamic-ospf' routing method."
            )
            return False
        return True

    def _init_node(self, node: Node) -> None:
        """Enable MPLS, assign IPs, build Bird IS-IS SR config, and start Bird."""
        _enable_mpls(node)
        _assign_ips(node)
        config = _build_isis_sr_config(node)
        _apply_config(node, config)


def _build_isis_sr_config(node: Node) -> str:
    """Build a complete Bird2 IS-IS + SR-MPLS configuration string for one node.

    The configuration includes:
    - Global settings (router id, log)
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


def _apply_config(node: Node, config: str) -> None:
    """Install a Bird IS-IS SR config and start or reload Bird in the container.

    Args:
        node: The node that will run Bird.
        config: Complete Bird2 configuration as a string.
    """
    apply_bird_config(node, config)


_format_net_address = format_net_address
_enable_mpls = enable_mpls
_assign_ips = assign_ips
