"""Shared command builders for routing daemons.

These helpers keep exact command strings in one place so the daemons remain
behavior-compatible while avoiding duplicated shell construction.
"""

from __future__ import annotations

import base64
from typing import Any


def build_ip_batch_command(node: Any) -> str | None:
    """Build the base64 ``ip -batch`` command for a node's addresses.

    Returns None when there is nothing to assign.
    """
    lines = []
    for iface in node.interfaces:
        ip = getattr(iface, "ipv4", None)
        if ip:
            lines.append(
                f"addr replace {ip}/{iface.prefix_len} dev {iface.get_iname()}"
            )
    if getattr(node.loopback, "ipv4", None):
        lines.append(f"addr replace {node.loopback.ipv4}/32 dev lo")
    if not lines:
        return None
    payload = "\n".join(lines)
    encoded = base64.b64encode(payload.encode()).decode()
    return f"echo {encoded} | base64 -d | ip -force -batch -"


def assign_ips(node: Any) -> None:
    """Assign interface/loopback addresses through an idempotent batch."""
    command = build_ip_batch_command(node)
    if command is not None:
        node.execute_command(command)


def encode_ip_batch(lines: list[str]) -> str:
    """Encode iproute2 batch lines for ``ip -force -batch -``."""
    payload = "\n".join(lines)
    encoded = base64.b64encode(payload.encode()).decode()
    return f"echo {encoded} | base64 -d | ip -force -batch -"


def enable_mpls_commands(node: Any, *, load_modules: bool = True) -> list[str]:
    """Return the kernel MPLS-enable commands for a node."""
    commands: list[str] = []
    if load_modules:
        commands.extend(
            [
                "modprobe mpls_router 2>/dev/null || true",
                "modprobe mpls_iptunnel 2>/dev/null || true",
            ]
        )
    commands.extend(
        [
            "sysctl -w net.mpls.platform_labels=1048575",
            "sysctl -w net.mpls.conf.lo.input=1",
        ]
    )
    for iface in node.interfaces:
        commands.append(f"sysctl -w net.mpls.conf.{iface.get_iname()}.input=1")
    return commands


def enable_mpls(node: Any, *, load_modules: bool = True) -> None:
    """Enable kernel MPLS forwarding on a node.

    The sysctl commands are one fail-fast batch: a per-interface failure must
    abort setup instead of being hidden by a later command. Module loading
    stays best-effort because a built-in MPLS stack has no module to load.
    """
    commands = enable_mpls_commands(node, load_modules=load_modules)
    modules = [command for command in commands if command.startswith("modprobe ")]
    sysctls = [command for command in commands if not command.startswith("modprobe ")]
    batch = "; ".join([*modules, " && ".join(sysctls)])
    node.execute_command(f"set -e; {batch}")


def format_net_address(loopback_ip: str) -> str:
    """Format an IS-IS NET address from a loopback IPv4 address."""
    parts = [f"{int(part):03d}" for part in loopback_ip.split(".")]
    full = "".join(parts)
    system_id = f"{full[0:4]}.{full[4:8]}.{full[8:12]}"
    return f"49.0001.{system_id}.00"
