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
import subprocess
import threading
import uuid

import docker.models
import docker.models.containers
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
        self.ipv6_routing_table: list[RoutingEntry] = []
        self.interfaces: list[Interface] = []
        self.ipv4_previous_routing_table = []
        self.ipv6_previous_routing_table = []
        self.entriesToEdit = []
        self.type: str
        self.command_output = ""

        self.position: dict[str, float] = {}

        # We create a loopback interface for the node
        self.loopback = Interface("lo", iface_type="lo")

        # New faster way to handle containers
        self.container: docker.models.containers.Container
        self.container_pid: int | None = None

        # Change FRRManager ISIS

    def execute_command(self, command: str, detach: bool = False) -> None:
        """
        A method that executes a command in the container.
        :param command: The command to execute.
        """
        if not self.container:
            logging.error(f"Container {self.name} is not initialized.")
            return None

        # Wrap command in a shell to ensure it runs correctly
        if command.startswith("sh -c") or command.startswith("bash -c"):
            command = command
        else:
            command = f'sh -c "{command}"'

        error, output = self.container.exec_run(
            cmd=command,
            detach=detach,
        )  # type: ignore
        if detach:
            return None
        if not error:
            self.command_output = output.decode("utf-8").strip()
        return error if error else output.decode("utf-8").strip()

    def set_ipv4s_to_containers(
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

        if not interface:
            ...
            # logging.info(f"Setting IPv4s to container {self.name} on all interfaces")
        else:
            # logging.info(f"Setting IPv4 {interface.ipv4} to container {self.name} on interface {interface.name}")
            if not interface.ipv4:
                logging.warning(
                    f"Interface {interface.name} has no IPv4 address set, skipping"
                )
                return None

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
        cmd = f'bash -lc "echo {b64} | base64 -d | ip -force -batch -"'

        self.container.exec_run(cmd=cmd, detach=False)  # type: ignore

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

    def set_ipv6s_to_container(self, set_lo: bool = True) -> None:
        """
        A method that sets the IPs of the interfaces to the container.
        """
        tmpfile = f"/tmp/batch_{uuid.uuid4().hex}.txt"
        logging.info(
            f"Setting IPv6s to container {self.name} using temporary file {tmpfile}"
        )
        with open(tmpfile, "w") as f:
            for interface in self.interfaces:
                IP = interface.ipv4
                interName = interface.get_iname()
                command = f"addr add {IP}/127 dev {interName}"
                f.write(f"{command}\n")

            if not set_lo:
                return None
            if hasattr(self, "loopback"):
                command = f"addr add {self.loopback.ipv4}/64 dev lo"
                f.write(f"{command}\n")

        executable = f"ip -6 -force -batch " + tmpfile + " && rm " + tmpfile

        self.execute_command(executable, detach=True)

    def enable_ipv6_forwarding(self) -> None:
        """
        A method that enables ipv6 forwarding in the container.
        """
        self.execute_command('sh -c "echo 1 > /proc/sys/net/ipv6/conf/all/forwarding"')
        self.execute_command(
            'sh -c "echo 1 > /proc/sys/net/ipv6/conf/default/forwarding"'
        )

        return None

    def disable_rp_filter(self) -> None:
        """
        A method that disables the reverse path filtering in the container.
        """
        self.execute_command('sh -c "echo 0 > /proc/sys/net/ipv4/conf/all/rp_filter"')
        self.execute_command(
            'sh -c "echo 0 > /proc/sys/net/ipv4/conf/default/rp_filter"'
        )
        for interface in self.interfaces:
            interName = interface.get_iname()
            self.execute_command(
                f'sh -c "echo 0 > /proc/sys/net/ipv4/conf/{interName}/rp_filter"'
            )
            logging.info(
                f"Disabling reverse path filtering for interface {interName} in container {self.name}"
            )

        return None

    def init_bmv2(self) -> None:
        """
        A method that initializes P4D in the container.
        """
        command = "/usr/bin/start-service.py --service bmv2"

        self.execute_command(command)

        return None

    # ==================== End MPLS Methods ====================

    @staticmethod
    def execute_function_in_all_containers(
        list_of_containers: list[
            Node
        ],  # 2.0071 seconds on average for 400 satellites, 40 gnds
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        A method that executes a function in all containers.
        :param function: The function to execute.
        :param args: The arguments of the function.
        :param kwargs: The keyword arguments of the function.
        """
        workers: list[threading.Thread] = []
        for container in list_of_containers:
            try:
                # Create a new thread for each container
                worker = threading.Thread(
                    target=function,
                    args=(container,) + args,
                    kwargs=kwargs,
                    name=container.name,
                )
                workers.append(worker)
                worker.start()
            except Exception as e:
                print(
                    f"Error executing function {function.__name__} in container {container.name}: {e}"
                )

        # Wait for all threads to finish
        for worker in workers:
            try:
                worker.join()
            except Exception as e:
                print(f"Error joining thread for container {worker.name}: {e}")

        return None

    def create_interface(
        self,
        name: str,
    ) -> Interface:
        """
        A method that creates an interface for the ground station.
        :param ip_address: The IP of the interface.
        :param name: The name of the interface.
        :param peer: The peer of the interface.
        :return: The interface.
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

    def open_console(self, start_command: str = "") -> None:
        """
        A method that opens the console of the container.
        """
        # Get the container ID
        print(f"Opening console for container {self.name} with command {start_command}")
        if hasattr(self, "container") and not isinstance(
            self.container, docker.models.containers.Container
        ):
            logging.error(f"Container {self.name} is not initialized correctly.")
            return None
        container_id: str | None = self.container.id
        if not container_id:
            logging.error(f"Container {self.name} is not initialized.")
            return None
        if not start_command:
            command: list[str] = [
                "gnome-terminal",
                "--",
                "docker",
                "exec",
                "-it",
                container_id,
                "/bin/bash",
            ]
        else:
            command: list[str] = [
                "gnome-terminal",
                "--",
                "docker",
                "exec",
                "-it",
                container_id,
                "/bin/bash",
                "-c",
                start_command,
            ]
        subprocess.Popen(command)

        return None

    def down_inactive_interfaces(self) -> None:
        """
        A method that down all inactive interfaces.
        """
        for interface in self.interfaces:
            if not interface.is_active and interface.previously_active:
                # If the interface is not active and was previously active, down it
                command = f'sh -c "ip link set {interface.get_iname()} down"'
                self.execute_command(command)
                logging.info(
                    f"Downing interface {interface.get_iname()} in container {self.name}"
                )

        return None

    def up_active_interfaces(self) -> None:
        """
        A method that up all active interfaces.
        """
        for interface in self.interfaces:
            if interface.is_active and not interface.previously_active:
                # If the interface is active and was previously inactive, up it
                command = f'sh -c "ip link set {interface.get_iname()} up"'
                self.execute_command(command)
                logging.info(
                    f"Upping interface {interface.get_iname()} in container {self.name}"
                )

        return None

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
        routing_table = self.execute_command("ip route show")
        if routing_table:
            return routing_table.split("\n")
        return []

    def sync_state_interfaces(self) -> None:
        """
        A method that syncs the state of the interfaces.
        """
        for interface in self.interfaces:
            interface.previously_active = interface.is_active
        return None

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

    def get_interface_by_peer(self, path: list[int], peer: Node) -> Interface | None:
        """
        Method to get the interface by peer node.
        :param path: The path to the peer node.
        :param peer: The peer node.
        :return: The interface if found, None otherwise.
        """
        first_hop = path[1]
        if first_hop is None:
            logging.error(f"No first hop found in path {path} for peer {peer.name}.")
            return None
        # Find the interface that matches the peer node
        print(f"{self.id}.{first_hop}")
        interface = get_interface_from_name(self.interfaces, f"{self.id}.{first_hop}")
        if interface is None:
            logging.error(
                f"No interface found for peer {peer.name} in node {self.name}."
            )
            return None
        logging.info(
            f"Found interface {interface.name} in node {self.name} for peer {peer.name}."
        )
        return interface

    def bridge_node_to_host(self, phy_interface: str) -> None:
        """
        A method that bridges the interface to the host.
        """
        ## We will need sudo for these commands so execute in a shell with sudo privileges

        ### Create the Veth pair ###
        veth_pair = "sudo ip link add veth-host type veth peer name veth-container"

        ### Attach one end of the Veth pair to the host bridge ###
        attach_to_bridge = f"sudo ip link set veth-container netns {self.container_pid}"

        ### Down interfaces
        down_interfaces = (
            f"sudo ip link set veth-host down && sudo ip link set {phy_interface} down"
        )

        ### Create bridge and add interfaces ###
        create_bridge = f"sudo brctl addbr br-{self.name} && sudo brctl addif br-{self.name} {phy_interface} && sudo brctl addif br-{self.name} veth-host"

        ### Up interfaces ###
        up_interfaces = f"sudo ip link set veth-host up && sudo ip link set {phy_interface} up && sudo ip link set br-{self.name} up"

        ### Append the commands together ###
        full_command = f"{veth_pair} && {attach_to_bridge} && {down_interfaces} && {create_bridge} && {up_interfaces}"

        subprocess.Popen(
            ["gnome-terminal", "--", "bash", "-c", full_command + "; exec bash"]
        )

        # ### Configure the interfaces ###

        # ## On the host side ##
        # configure_host_side = "sudo ip addr add 10.1.1.1/24 dev veth-host && sudo ip link set veth-host up"

        # ## On the container side ##
        # configure_container_side = f'sudo nsenter -t {self.container_pid} -n ip addr add 10.1.1.2/24 dev veth-container && sudo nsenter -t {self.container_pid} -n ip link set veth-container up'

        # ### Append the commands together ###
        # full_command = f"{veth_pair} && {attach_to_bridge} && {configure_host_side} && {configure_container_side}"

        # Execute on new shell with sudo privileges
        # subprocess.Popen(['gnome-terminal', '--', 'bash', '-c', full_command + '; exec bash'])

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
