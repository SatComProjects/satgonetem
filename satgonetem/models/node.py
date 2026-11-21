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
import ipaddress
import logging
import shlex
from typing import Any

# Ensure the protobuf dependency for netem_pb2 is loaded first.
from google.protobuf import empty_pb2  # noqa: F401

from satgonetem.link_budget.antenna import Antenna
from satgonetem.models.interface import Interface
from satgonetem.models.routing_entry import RoutingEntry
from satgonetem.ports import (
    ContainerRuntime,
    DockerContainerRuntime,
    LinuxNetworkNamespace,
    NetworkNamespace,
)
from satgonetem.utils.utils import unique_pair_id


class Node:
    """
    Abstract class defining the interface for all nodes in the emulator.
    """

    def __init__(self, name: str):
        self.name = name
        self.id = int(name[3:])
        self.ipv4_routing_table: list[RoutingEntry] = []
        self.interfaces: list[Interface] = []
        self.ipv4_previous_routing_table: list[RoutingEntry] = []
        self.entries_to_edit: list[Any] = []
        self.type: str
        self.command_output = ""

        self.position: dict[str, float] = {}
        self.antenna: Antenna | None = None
        # Address block assigned by the topology-wide IPv4 plan.
        self.address_block: ipaddress.IPv4Network | None = None

        # We create a loopback interface for the node
        self.loopback = Interface("lo", iface_type="lo")

        # Container/runtime boundaries; injectable for tests.
        self.container: Any = None
        self.container_pid: int | None = None
        self.image = ""
        self.container_runtime: ContainerRuntime = DockerContainerRuntime()
        self.network_namespace: NetworkNamespace = LinuxNetworkNamespace()

        # gRPC client for GoNetem NodeRun (set by launcher after topology start)
        self.grpc_client: Any = None
        self.project_id: str = ""

    def _prepare_command(self, command: str | list[str]) -> list[str]:
        """Normalize a command to a list of strings."""
        if isinstance(command, str):
            if command.startswith("sh -c") or command.startswith("bash -c"):
                try:
                    return shlex.split(command)
                except ValueError as exc:
                    raise RuntimeError(
                        f"Malformed shell command for {self.name}: {exc}"
                    ) from exc
            else:
                return ["sh", "-c", command]
        return list(command)

    def execute_command_docker(
        self,
        command: str | list[str],
        detach: bool = False,
        ok_exit_codes: tuple[int, ...] = (0,),
    ) -> str:
        """
        Backup of the original docker-based command execution.

        Execute a command in the container using docker-py.

        Args:
            command: The shell command to execute.
            detach: If True, run the command in the background and return
                immediately.

        Returns:
            The command's stdout as a stripped string. Returns an empty string
            when *detach* is True.

        Raises:
            RuntimeError: If the container is not initialized, the command
                string is malformed, or the command exits with an unexpected
                status.
        """
        if not self.container:
            raise RuntimeError(f"Container {self.name} is not initialized.")

        cmd = self._prepare_command(command)

        exit_code, output = self.container_runtime.exec(
            self.container, cmd, detach=detach
        )
        if detach:
            return ""
        decoded = output.decode("utf-8").strip() if output else ""
        self.command_output = decoded
        if exit_code not in ok_exit_codes:
            raise RuntimeError(
                f"Command failed in {self.name} (exit {exit_code}): {decoded}"
            )
        return decoded
    
    def _require_container_pid(self, node: "Node") -> int:
        """Return *node*'s container PID, resolving and caching it if needed."""
        if node.container_pid is not None:
            return node.container_pid
        if node.container is not None:
            pid = self.container_runtime.pid(node.container)
            if pid is not None:
                node.container_pid = pid
                return pid
        raise RuntimeError(
            f"Cannot create interface on {node.name}: container PID is not available"
        )

    def create_dummy_interface(self, name: str) -> None:
        """Create a dummy interface inside the node's container namespace."""
        pid = self._require_container_pid(self)
        try:
            self.network_namespace.create_dummy_interface(pid, name)
        except Exception as exc:
            logging.error(
                "Failed to create dummy interface %s on %s: %s",
                name,
                self.name,
                exc,
            )
            raise

    def connect_to(
        self,
        peer_node: "Node",
        peer1_name: str,
        peer2_name: str,
    ) -> None:
        """Create a veth pair between this node and *peer_node*."""
        source_pid = self._require_container_pid(self)
        destination_pid = self._require_container_pid(peer_node)
        try:
            self.network_namespace.create_veth(
                source_pid, destination_pid, peer1_name, peer2_name
            )
        except Exception as exc:
            logging.error(
                "Failed to create veth %s<->%s: %s",
                peer1_name,
                peer2_name,
                exc,
            )
            raise

    def execute_command(
        self,
        command: str | list[str],
        detach: bool = False,
        use_docker_cli: bool = False,
        ok_exit_codes: tuple[int, ...] = (0,),
    ) -> str:
        """
        Execute a command on the node.

        Uses the GoNetem gRPC ``NodeRun`` when a gRPC client is available and
        *detach* is False; otherwise falls back to the docker-based
        :meth:`execute_command_docker`.

        Args:
            command: The shell command to execute.
            detach: If True, run the command in the background and return
                immediately.  Detached execution always uses docker.
            use_docker_cli: Skip gRPC and run the command through Docker.
            ok_exit_codes: Exit codes that are not errors. Tools such as ping
                and hping3 return 1 for a normal run with no replies.

        Returns:
            The command's stdout as a stripped string. Returns an empty string
            when *detach* is True.

        Raises:
            RuntimeError: If the node is not ready, the command string is
                malformed, or the command exits with an unexpected status.
        """
        if use_docker_cli: #Skip directly to docker
            return self.execute_command_docker(command, detach, ok_exit_codes)
        if self.grpc_client and not detach:
            import grpc

            from satgonetem.proto import netem_pb2

            cmd = self._prepare_command(command)
            request = netem_pb2.NodeRunRequest(  # type: ignore[attr-defined]
                prjId=self.project_id,
                node=self.name,
                cmd=cmd,
            )
            try:
                response = self.grpc_client.NodeRun(request)
            except grpc.RpcError as exc:
                if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                    return self.execute_command_docker(
                        command, detach, ok_exit_codes
                    )
                raise
            if response.status.code == netem_pb2.StatusCode.ERROR:  # type: ignore[attr-defined]
                raise RuntimeError(
                    f"NodeRun failed for {self.name}: {response.status.error}"
                )
            decoded = response.stdout.decode("utf-8").strip() if response.stdout else ""
            decoded_err = response.stderr.decode("utf-8").strip() if response.stderr else ""
            self.command_output = decoded
            if response.exitCode not in ok_exit_codes:
                detail = decoded
                if decoded_err:
                    detail = f"{decoded}\nstderr: {decoded_err}" if decoded else decoded_err
                raise RuntimeError(
                    f"Command failed in {self.name} (exit {response.exitCode}): {detail}"
                )
            return decoded

        return self.execute_command_docker(command, detach, ok_exit_codes)

    def set_ipv4_to_containers(
        self,
        interface: Interface | None,
        set_lo: bool = True,
    ) -> None:
        """Apply this node's planned IPv4 addresses in one batched command.

        The prefix comes from each :class:`Interface`; it is no longer assumed
        to be ``/31`` here.  ``addr replace`` keeps repeated setup calls
        idempotent and updates an address if a link is recreated.

        Args:
            interface: One interface to push, or ``None`` for all node
                interfaces.
            set_lo: Also apply the node loopback address.
        """
        lines: list[str] = []

        int_list = [interface] if interface is not None else self.interfaces

        for iface in int_list:
            ip = getattr(iface, "ipv4", None)
            if not ip:
                continue
            if iface.prefix_len not in (31, 32):
                raise ValueError(
                    f"{self.name} interface {iface.name} has unsupported IPv4 "
                    f"prefix /{iface.prefix_len}; expected /31 or /32"
                )
            lines.append(
                f"addr replace {ip}/{iface.prefix_len} dev {iface.get_iname()}"
            )

        if (
            set_lo
            and getattr(self, "loopback", None) is not None
            and getattr(self.loopback, "ipv4", None)
        ):
            lines.append(f"addr replace {self.loopback.ipv4}/32 dev lo")

        if not lines:
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
            logging.debug(
                "Removed interface %s from node %s connected to %s",
                interface_to_remove.name,
                self.name,
                peer.name,
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
