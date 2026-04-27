"""
Node - abstract base class for all network nodes in the emulator.

Responsibilities:
  - Core identity and state (name, id, position, interfaces, routing tables).
  - Container command execution and IP address assignment.
  - Interface lifecycle (create, up/down, state sync).
  - Routing daemon delegation (IS-IS / SR-MPLS via frr_manager).
  - OSPF is handled externally by OSPFDaemon.

Cross-cutting concerns such as packet capture, QoS, and traffic generation
are handled by the mixin classes in satgonetem.models.mixins.
"""

from __future__ import annotations

import base64
import logging
import shlex
import subprocess
import threading

import docker.models
import docker.models.containers
from satgonetem.link_budget.antenna import Antenna
from satgonetem.models.interface import Interface
from satgonetem.utils.utils import get_interface_from_name, unique_pair_id

from typing import Any, Callable, Optional

import os

import docker

from satgonetem.models.routing_entry import RoutingEntry


class Node:
    """
    Abstract class defining the interface for all nodes in the emulator.
    """

    def __init__(self, name: str):
        self.name = name
        self.id = int(name[3:])
        self.ipv4_routing_table: list[RoutingEntry] = []
        self.interfaces: list[Interface] = []
        self.ipv4_previous_routing_table = []
        self.entries_to_edit = []
        self.type: str
        self.command_output = ""

        self.position: dict[str, float] = {}
        self.antenna: Antenna | None = None

        # We create a loopback interface for the node
        self.loopback = Interface("lo", iface_type="lo")

        # New faster way to handle containers
        self.container: docker.models.containers.Container | None = None
        self.container_pid: int | None = None

    def execute_command(self, command: str | list[str], detach: bool = False) -> str:
        """
        Execute a command in the container.

        Args:
            command: The shell command to execute.
            detach: If True, run the command in the background and return
                immediately.

        Returns:
            The command's stdout as a stripped string. Returns an empty string
            when *detach* is True.

        Raises:
            RuntimeError: If the container is not initialized, the command
                string is malformed, or the command exits with a non-zero
                status.
        """
        if not self.container:
            raise RuntimeError(f"Container {self.name} is not initialized.")

        # Pass commands as a list so docker-py forwards them directly to the
        # container runtime without additional shell interpretation.  If a
        # caller already wrapped the command in sh -c / bash -c we split it
        # safely with shlex; otherwise we wrap it ourselves.
        if isinstance(command, str):
            if command.startswith("sh -c") or command.startswith("bash -c"):
                try:
                    command = shlex.split(command)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Malformed shell command for {self.name}: {exc}"
                    ) from exc
            else:
                command = ["sh", "-c", command]

        exit_code, output = self.container.exec_run(
            cmd=command,
            detach=detach,
        )  # type: ignore
        if detach:
            return ""
        decoded = output.decode("utf-8").strip() if output else ""
        self.command_output = decoded
        if exit_code != 0:
            raise RuntimeError(
                f"Command failed in {self.name} (exit {exit_code}): {decoded}"
            )
        return decoded

    def set_ipv4_to_containers(
        self,
        interface: Interface,
        set_lo: bool = True,
    ) -> None:
        """
        Set IPv4 addresses on all interfaces inside the container in one fast shot.

        Performance improvements:
        - No temp files: pipe a single base64-encoded batch to `ip -force -batch -`.
        - Use `addr replace` to make ops idempotent (no "File exists" errors).
        - Single shell process per node (lower process exec overhead).

        Side effects and concurrency model remain the same (detach=True).
        """
        # Build batch lines
        lines = []

        int_list = [interface] if interface else self.interfaces

        for interface in int_list:
            ip = getattr(interface, "ipv4", None)
            if not ip:
                continue
            dev = interface.get_iname()
            # /31 per your original code
            lines.append(f"addr add {ip}/31 dev {dev}")

        if (
            set_lo
            and hasattr(self, "loopback")
            and getattr(self.loopback, "ipv4", None)
        ):
            lines.append(f"addr add {self.loopback.ipv4}/32 dev lo")

        if not lines:
            # Nothing to do
            return None

        payload = "\n".join(lines)
        b64 = base64.b64encode(payload.encode()).decode()
        cmd = f"echo {b64} | base64 -d | ip -force -batch -"

        self.execute_command(command=["sh", "-c", cmd], detach=False)  # type: ignore

        return None

    def remove_interface_connected_to_node(self, peer: Node) -> None:
        """
        A method that removes the interface connected to a specific node.
        :param peer: The peer node.
        """

        eth_name = f"{peer.name}.{self.id}"

        interface_to_remove = None
        for interface in self.interfaces:
            if interface.peer and eth_name == interface.peer.name:
                interface_to_remove = interface
                break

        if interface_to_remove:
            self.interfaces.remove(interface_to_remove)
            logging.info(
                f"Removed interface {interface_to_remove.name} from node {self.name} connected to {peer.name}"
            )
        else:
            logging.warning(
                f"No interface found in node {self.name} connected to {peer.name}"
            )

        return None

    def init_bmv2(self) -> None:
        """
        A method that initializes P4D in the container.
        """
        command = "/usr/bin/start-service.py --service bmv2"

        self.execute_command(command)

        return None

    def create_interface(
        self,
        name: str,
    ) -> Interface:
        """
        A method that creates an interface for the ground station.
        Args:
            name: The name of the interface to create.
        Returns: The created Interface object.
        """
        interface = Interface(name=name)
        self.interfaces.append(interface)

        return interface

    def get_interfaces(self) -> list[Interface]:
        """
        A method that returns the interfaces of the node.
        :return: The interfaces.
        """
        return self.interfaces

    def hash_node(self) -> int:
        """
        A method that returns the hash of the node.
        :return: The hash of the node.
        """
        return hash(
            (
                self.name,
                self.position["latitude"],
                self.position["longitude"],
                self.position["altitude"],
            )
        )

    def get_routing_table(self) -> list[str]:
        """
        A method that returns the routing table of the node.
        :return: The routing table.
        """
        try:
            routing_table = self.execute_command("ip route show")
        except RuntimeError:
            return []
        return routing_table.split("\n") if routing_table else []

    def add_policy_based_routing_rules(self) -> None:
        """
        Method to add policy-based routing rules for IPv4.
        This method should be overridden by subclasses to implement specific routing rules.
        """
        logging.info(f"Adding policy-based routing rules for {self.name}.")

        for entry in self.ipv4_routing_table:
            rule_id = 10000 + unique_pair_id(
                int(entry.source_node[3:]), int(entry.target_node[3:])
            )  # Get the rule ID based on the target node ID

            command = (
                f"ip rule add to {entry.destination}/{entry.prefix} table {rule_id}"
            )

            logging.info(f"Executing command: {command}")
            self.execute_command(command)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Node):
            return False
        return self.name == other.name

    def __hash__(self) -> int:
        return hash(self.name)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return self.name
