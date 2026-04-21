"""
Satellite - LEO/MEO/GEO satellite node in the emulator.

Extends Node with satellite-specific state and behaviour:
  - Position synchronisation from sat_com_model.
  - IPv4/IPv6 address assignment for ISL interfaces and loopbacks.
  - Network statistics logging (ifstat, net_logger).
  - Attack control (hping3 stop).
  - Ingress Filter Bridge (IFB) QoS: create, delete, apply program-specific rules.

Cross-cutting concerns are inherited via mixins:
  - QoSCapableMixin: TBF global QoS initialisation.
"""

from __future__ import annotations

import logging
import re
from threading import Thread
import time

from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.models.mixins.qos_mixin import QoSCapableMixin
from satgonetem.utils.ip_utils import IPUtils

from sat_com_model.models import Satellite as SatComSatellite

import pathlib


class Satellite(Node, QoSCapableMixin):
    """
    Represents a satellite node in the emulated constellation.

    Attributes:
        antenna (Antenna): Satellite antenna model with downlink EIRP.
        handover (bool): True if a handover occurred in the last timestep.
        default_qos_is_on (bool): Whether the default TBF queuing is active.
        program_specific_qos_is_on (bool): Whether program-specific HTB is active.
        qos_configuration_count (int): Number of times QoS has been reconfigured.
        satcom_object (SatComSatellite | None): Corresponding sat_com_model object.
        shell: Legacy shell reference (unused).
    """

    def __init__(self, name: str = "") -> None:
        """
        Initialise a Satellite node.

        Args:
            name: Node identifier (e.g. 'Sat0'). The numeric suffix is used as
                the node ID.
        """
        super().__init__(name)

        self.shell = None

        self.default_qos_is_on: bool = True
        self.program_specific_qos_is_on: bool = False
        self.qos_configuration_count: int = 0

        self.satcom_object: SatComSatellite | None = None

    def sync_position_from_satcom(self) -> None:
        """
        Update the satellite's geographic position from the sat_com_model object.

        Raises:
            ValueError: If satcom_object has not been assigned.

        Returns:
            None
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set for satellite " + self.name)

        lat, lon, alt = (
            self.satcom_object.spatial_position.to_latitude_longitude_altitude()
        )
        if lat is None or lon is None or alt is None:
            logging.warning(
                "Satellite %s: could not retrieve position from sat_com_model",
                self.name,
            )
            return
        self.position["latitude"] = lat
        self.position["longitude"] = lon
        self.position["altitude"] = alt / 1000
        if self.position["altitude"] < 100:
            self.position["altitude"] *= 1000

    def set_ipv4_addresses(self, interface: Interface | None = None) -> None:
        """
        Assign IPv4 addresses to satellite interfaces.

        Args:
            interface: If provided, assign only to this interface. If None,
                assign to all interfaces and the loopback.

        Returns:
            None
        """
        if interface is None:
            for iface in self.interfaces:
                iface.set_ipv4_address()
            self.loopback.set_ipv4_address()
        else:
            interface.set_ipv4_address()

    @staticmethod
    def _set_ip_addresses_to_satellites(
        sat_list: list[Satellite], version: int
    ) -> None:
        """
        Assign IPv4 or IPv6 addresses to all satellites in a list.

        Skips interfaces with 'Gnd' in their name; those are handled by
        GroundStation._set_ip_addresses_to_ground_stations.

        Args:
            sat_list: List of Satellite objects to configure.
            version: IP version. 4 for IPv4, 6 for IPv6.

        Returns:
            None
        """
        if version == 4:
            get_addr = IPUtils.get_ipv4_address
            get_entropy = IPUtils.get_ipv4_address_entropy
            set_iface = lambda iface, ip: iface.set_ip(ip)
            peer_id_fn = lambda name: int(name.split(".")[1])
        else:
            get_addr = IPUtils.get_ipv6_address
            get_entropy = IPUtils.get_ipv6_address_entropy
            set_iface = lambda iface, ip: iface.set_ipv6(ip)
            peer_id_fn = lambda name: int(name.split(".")[0][3:])

        for sat in sat_list:
            local_id = sat.id

            for iface in sat.interfaces:
                name = iface.name
                if "Gnd" in name:
                    continue
                peer_id = peer_id_fn(name)

                local_ip, peer_ip = get_addr(
                    node=local_id, peer=peer_id, type="Satellite", loopback=False
                )
                set_iface(iface, local_ip)
                if iface.peer is None:
                    logging.error(
                        "Satellite %s interface %s has no peer set.",
                        sat.name,
                        iface.name,
                    )
                    continue
                set_iface(iface.peer, peer_ip)

                entropy = get_entropy(local_ip)
                logging.info(
                    "Satellite %s interface %s entropy %.4f IP %s peer %s",
                    sat.name,
                    iface.name,
                    entropy,
                    local_ip,
                    peer_ip,
                )

            loopback_ip = get_addr(
                node=local_id, peer=local_id, type="Satellite", loopback=True
            )
            set_iface(sat.loopback, loopback_ip)

    @staticmethod
    def set_ipv4_addresses_to_satellites(sat_list: list[Satellite]) -> None:
        """
        Assign IPv4 addresses to all satellites in a list.

        Args:
            sat_list: List of Satellite objects.

        Returns:
            None
        """
        Satellite._set_ip_addresses_to_satellites(sat_list, version=4)

    @staticmethod
    def set_ipv6_addresses_to_satellites(sat_list: list[Satellite]) -> None:
        """
        Assign IPv6 addresses to all satellites in a list.

        Args:
            sat_list: List of Satellite objects.

        Returns:
            None
        """
        Satellite._set_ip_addresses_to_satellites(sat_list, version=6)

    def start_ifstat_alpine(self, path: str) -> None:
        """
        Start continuous ifstat JSON logging on an Alpine-based container.

        Args:
            path: Directory path inside the container to write logs.

        Returns:
            None
        """
        command = f'bash -c "while true; do ifstat -j > {path}/{self.name}.log; sleep 1; done"'
        Thread(target=self.execute_command, args=(command,)).start()

    def start_ifstat_debian(self, path: str) -> None:
        """
        Start ifstat logging on a Debian-based container.

        Args:
            path: Directory path inside the container to write logs.

        Returns:
            None
        """
        command = f'bash -c "ifstat > {path}/{self.name}.log"'
        Thread(target=self.execute_command, args=(command,)).start()

    def start_net_logger(self, path: str) -> None:
        """
        Start net_logger on every interface of the satellite.

        Args:
            path: Host directory path for log files.

        Returns:
            None
        """
        for interface in self.interfaces:
            iname = interface.get_iname()
            destination = path + f"/{self.name}_{iname}.log"
            command = (
                f'bash -c "net_logger --interface {iname} '
                f'--log {destination} --interval 1"'
            )
            self.execute_command(command, detach=True)

    def stopAttack(self, delay: int = 0) -> None:
        """
        Stop any running hping3 attack inside the container.

        Args:
            delay: Seconds to wait before issuing the kill command.

        Returns:
            None
        """
        time.sleep(delay)
        logging.info("Stopping attack on %s", self.name)
        self.execute_command('sh -c "killall hping3"')

    def create_ibf_interaces(self) -> None:
        """
        Create IFB (Ingress Filter Bridge) interfaces for ingress traffic shaping.

        For each egress interface, an IFB counterpart is created and traffic is
        mirrored to it via a tc ingress qdisc and filter. The existing netem and
        TBF qdisc parameters are copied to the IFB interface.

        Returns:
            None
        """
        for interface in self.interfaces:
            egress_name = "eth" + interface.name.split(".")[1]
            ingress_name = "ifb" + interface.name.split(".")[1]

            self.execute_command(f"ip link add name {ingress_name} type ifb")
            self.execute_command(f"ip link set dev {ingress_name} up")
            self.execute_command(f"tc qdisc add dev {egress_name} handle ffff: ingress")
            self.execute_command(
                f"tc filter add dev {egress_name} parent ffff: protocol ip u32 "
                f"match u32 0 0 action mirred egress redirect dev {ingress_name}"
            )

            self.execute_command(f"tc qdisc show dev {egress_name}")
            lines = self.command_output.strip().split("\n")

            netem_pattern = r"limit (\d+) delay (\d+(?:ms|us))"
            match_netem = re.search(netem_pattern, lines[0])
            if not match_netem:
                logging.error(
                    "Satellite %s: could not parse netem on %s", self.name, egress_name
                )
                continue
            netem_limit = match_netem.group(1)
            netem_delay = match_netem.group(2)

            self.execute_command(
                f"tc qdisc add root handle 1: dev {ingress_name} "
                f"netem limit {netem_limit} delay {netem_delay}"
            )

            tbf_pattern_latency = r"rate (\d+[GMK]?bit) burst (\d+b) lat (\d+(?:ms|us))"
            tbf_pattern_limit = r"rate (\d+[GMK]?bit) burst (\d+b) limit (\d+b)"

            match_latency = re.search(tbf_pattern_latency, lines[1])
            match_limit = re.search(tbf_pattern_limit, lines[1])

            if match_latency:
                tbf_rate = match_latency.group(1)
                tbf_burst = match_latency.group(2)
                tbf_latency = match_latency.group(3)
                self.execute_command(
                    f"tc qdisc add dev {ingress_name} parent 1:1 handle 10: "
                    f"tbf rate {tbf_rate} burst {tbf_burst} latency {tbf_latency}"
                )
            elif match_limit:
                tbf_rate = match_limit.group(1)
                tbf_burst = match_limit.group(2)
                tbf_limit_val = match_limit.group(3)
                self.execute_command(
                    f"tc qdisc add dev {ingress_name} parent 1:1 handle 10: "
                    f"tbf rate {tbf_rate} burst {tbf_burst} limit {tbf_limit_val}"
                )
            else:
                logging.error(
                    "Satellite %s: no TBF match on %s", self.name, egress_name
                )

        logging.info("Created IFB interfaces on satellite %s", self.name)

    def delete_configured_qos(self) -> None:
        """
        Remove program-specific HTB qdiscs from all interfaces.

        Resets program_specific_qos_is_on to False and restores the default
        queuing flag.

        Returns:
            None
        """
        for interface in self.interfaces:
            egress_name = "eth" + interface.name.split(".")[1]
            ingress_name = "ifb" + interface.name.split(".")[1]
            self.execute_command(
                f"sh -c 'tc qdisc del dev {egress_name} parent 10: handle 2: htb'"
            )
            self.execute_command(
                f"sh -c 'tc qdisc del dev {ingress_name} parent 10: handle 2: htb'"
            )
        self.program_specific_qos_is_on = False
        self.default_qos_is_on = True

    def apply_program_specific_qos(
        self,
        program_name: str,
        demand_bit_rate: int = 100,
        isl_capacity: int = 1000,
        partial_demand_percentages: list | None = None,
    ) -> None:
        """
        Configure HTB-based per-program QoS on all satellite interfaces.

        Implements class-based queuing that maps IP TOS/DSCP values from iperf
        clients to HTB traffic classes. Idempotent: does nothing if
        program_specific_qos_is_on is already True.

        Args:
            program_name: Identifier of the optimisation program. Controls which
                HTB class structure is applied. Supported values: 'A', 'B', 'E', 'F'.
            demand_bit_rate: Total demanded bit rate per link in Mbps (default 100).
            isl_capacity: ISL link capacity in Mbps (default 1000).
            partial_demand_percentages: For program 'E', a list of per-flow demand
                percentages used to build proportional HTB classes.

        Returns:
            None
        """
        if self.program_specific_qos_is_on or not self.default_qos_is_on:
            logging.info(
                "QoS already configured on all interfaces of satellite %s", self.name
            )
            return

        for interface in self.interfaces:
            egress_name = "eth" + interface.name.split(".")[1]
            ingress_name = "ifb" + interface.name.split(".")[1]

            self.execute_command(
                f"tc qdisc add dev {egress_name} parent 10: handle 2: htb default 30"
            )
            self.execute_command(
                f"tc qdisc add dev {ingress_name} parent 10: handle 2: htb default 30"
            )
            self.execute_command(
                f"tc class add dev {egress_name} parent 2: classid 2:10 "
                f"htb rate {demand_bit_rate}Mbit ceil {demand_bit_rate}Mbit"
            )
            self.execute_command(
                f"tc class add dev {ingress_name} parent 2: classid 2:10 "
                f"htb rate {demand_bit_rate}Mbit ceil {demand_bit_rate}Mbit"
            )

            if program_name in {"A", "B", "F"}:
                self._apply_abf_qos(egress_name, ingress_name, demand_bit_rate)

            if program_name == "E":
                self._apply_e_qos(
                    egress_name,
                    ingress_name,
                    demand_bit_rate,
                    partial_demand_percentages or [],
                )

        self.program_specific_qos_is_on = True
        self.default_qos_is_on = False
        logging.info(
            "Program-specific QoS configured on all interfaces of satellite %s",
            self.name,
        )

    def _apply_abf_qos(
        self,
        egress_name: str,
        ingress_name: str,
        demand_bit_rate: int,
    ) -> None:
        """
        Apply HTB class structure for programs A, B, and F.

        Creates two priority classes (high/low) plus fq_codel leaf qdiscs and
        installs a DSCP filter to steer tagged flows into the high-priority class.

        Args:
            egress_name: Egress interface name (e.g. 'eth0').
            ingress_name: IFB ingress interface name (e.g. 'ifb0').
            demand_bit_rate: Link demand in Mbps used for the high-priority class.

        Returns:
            None
        """
        for dev in (egress_name, ingress_name):
            self.execute_command(
                f"tc class add dev {dev} parent 2:10 classid 2:20 "
                f"htb rate {demand_bit_rate}Mbit ceil {demand_bit_rate}Mbit prio 1"
            )
            self.execute_command(
                f"tc class add dev {dev} parent 2:10 classid 2:30 "
                "htb rate 0.1Mbit ceil 0.1Mbit prio 2"
            )
            self.execute_command(
                f"tc qdisc add dev {dev} parent 2:20 handle 30: fq_codel"
            )
            self.execute_command(
                f"tc filter add dev {dev} protocol ip parent 2: "
                "u32 match ip dsfield 10 0xff flowid 2:20"
            )

    def _apply_e_qos(
        self,
        egress_name: str,
        ingress_name: str,
        demand_bit_rate: int,
        partial_demand_percentages: list,
    ) -> None:
        """
        Apply proportional HTB class structure for program E.

        Builds one HTB class per demand percentage, assigns descending priorities,
        and installs DSCP filters to steer flows into the appropriate class.

        Args:
            egress_name: Egress interface name.
            ingress_name: IFB ingress interface name.
            demand_bit_rate: Total link demand in Mbps.
            partial_demand_percentages: List of per-flow demand percentages.

        Returns:
            None
        """
        all_traffic_tags = []
        tag = 10
        for _ in partial_demand_percentages:
            all_traffic_tags.append(tag)
            tag += 2

        partial_demand_percentages = sorted(partial_demand_percentages, reverse=True)

        class_params: dict[int, dict] = {}
        for idx, pct in enumerate(partial_demand_percentages, start=1):
            rate = float(pct) * int(demand_bit_rate) / 100
            class_params[idx] = {
                "rate": f"{rate}Mbit",
                "ceil": f"{int(demand_bit_rate)}Mbit",
                "prio": str(idx),
            }

        for dev in (egress_name, ingress_name):
            for class_id, params in class_params.items():
                self.execute_command(
                    f"tc class add dev {dev} parent 2:10 classid 2:{class_id + 1}0 "
                    f"htb rate {params['rate']} ceil {params['ceil']} prio {params['prio']}"
                )
                self.execute_command(
                    f"tc qdisc add dev {dev} parent 2:{class_id + 1}0 "
                    f"handle {class_id + 1}00: fq_codel"
                )

        all_traffic_tags_sorted = sorted(all_traffic_tags, reverse=True)
        for i, tag_val in enumerate(all_traffic_tags_sorted):
            flowid = f"2:{i + 2}0"
            for dev in (egress_name, ingress_name):
                self.execute_command(
                    f"tc filter add dev {dev} protocol ip parent 2: "
                    f"u32 match ip dsfield {tag_val} 0xff flowid {flowid}"
                )
