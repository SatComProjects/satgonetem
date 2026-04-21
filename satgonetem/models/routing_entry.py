from __future__ import annotations

from satgonetem.models.interface import Interface
from satgonetem.utils.ip_utils import IPUtils


class RoutingEntry:

    def __init__(
        self,
        destination: str,
        interface: Interface,
        gateway: str,
        prefix: int = 16,
        protocol: str = "ipv4",
        target_node: str = "",
        source_node: str = "",
        source: str = "",
    ) -> None:

        if protocol not in ["ipv4", "ipv6"]:
            raise ValueError(
                "Unsupported protocol. Only 'ipv4' and 'ipv6' are supported."
            )

        if protocol == "ipv4" and not IPUtils.is_valid_ipv4(destination):
            raise ValueError(
                f"Invalid IPv4 address: {destination} to destination {target_node}"
            )
        elif protocol == "ipv6" and not IPUtils.is_valid_ipv6(destination):
            raise ValueError(f"Invalid IPv6 address: {destination}")

        if protocol == "ipv4" and not (0 <= prefix <= 32):
            raise ValueError(
                f"Invalid prefix length for IPv4: {prefix}. Must be between 0 and 32."
            )
        elif protocol == "ipv6" and not (0 <= prefix <= 128):
            raise ValueError(
                f"Invalid prefix length for IPv6: {prefix}. Must be between 0 and 128."
            )

        """
        Constructor for the RoutingEntry class.
        :param destination: The destination IP address (IPv4 or IPv6).
        :param interface: The interface through which the destination is reachable.
        :param gateway: The gateway IP address (IPv4 or IPv6) for the route.
        :param prefix: The prefix length for the route (default is 16 for IPv4).
        :param protocol: The protocol type, either 'ipv4' or 'ipv6'
        """

        # Destination is received as IP address in string format
        self.prefix = prefix
        if protocol == "ipv4":
            self.destination = IPUtils.summarize_ipv4_address(destination, prefix)
        else:  # ipv6
            self.destination = IPUtils.summarize_ipv6_address(destination, prefix)
        self.interface = interface
        self.gateway = gateway
        self.target_node = target_node
        self.update = True
        self.source_node = source_node
        if source:
            if protocol == "ipv4":
                self.source = IPUtils.summarize_ipv4_address(source, prefix)
            else:  # ipv6
                self.source = IPUtils.summarize_ipv6_address(source, prefix)
        else:
            self.source = source

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RoutingEntry):
            return NotImplemented
        return (
            self.destination == other.destination
            and self.interface == other.interface
            and self.gateway == other.gateway
            and self.prefix == other.prefix
        )

    def __str__(self) -> str:
        return f"Destination: {self.destination}, Interface: {self.interface.name}, Gateway: {self.gateway}, Node name: {self.target_node}"

    def get_prefix(self) -> str:
        """
        Returns the prefix length of the routing entry.
        """
        return "/" + str(self.prefix)
