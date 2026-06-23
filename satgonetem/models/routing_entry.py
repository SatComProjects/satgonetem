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

        if protocol != "ipv4":
            raise ValueError("Unsupported protocol. Only 'ipv4' is supported.")

        if not (0 <= prefix <= 32):
            raise ValueError(
                f"Invalid prefix length for IPv4: {prefix}. Must be between 0 and 32."
            )

        """
        Constructor for the RoutingEntry class.
        :param destination: The destination IPv4 address.
        :param interface: The interface through which the destination is reachable.
        :param gateway: The gateway IPv4 address for the route.
        :param prefix: The prefix length for the route (default is 16).
        :param protocol: The protocol type. Only 'ipv4' is supported.
        """

        self.prefix = prefix
        try:
            self.destination = IPUtils.summarize_ipv4_address(destination, prefix)
        except ValueError as exc:
            raise ValueError(
                f"Invalid IPv4 address: {destination} to destination {target_node}"
            ) from exc
        self.interface = interface
        self.gateway = gateway
        self.target_node = target_node
        self.update = True
        self.source_node = source_node
        if source:
            self.source = IPUtils.summarize_ipv4_address(source, prefix)
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

    @classmethod
    def from_validated(
        cls,
        destination: str,
        interface: Interface,
        gateway: str,
        prefix: int,
        target_node: str,
        source_node: str,
        source: str,
    ) -> "RoutingEntry":
        """Create an entry without re-validating/summarizing IPs.

        Assumes ``destination`` and ``source`` are already summarized to
        ``prefix``.  This skips the expensive IPv4 parsing done by ``__init__``
        and is intended for hot paths where inputs are known to be valid.
        """
        entry = cls.__new__(cls)
        entry.prefix = prefix
        entry.destination = destination
        entry.interface = interface
        entry.gateway = gateway
        entry.target_node = target_node
        entry.update = True
        entry.source_node = source_node
        entry.source = source
        return entry

    def __str__(self) -> str:
        return f"Destination: {self.destination}, Interface: {self.interface.name}, Gateway: {self.gateway}, Node name: {self.target_node}"

    def get_prefix(self) -> str:
        """
        Returns the prefix length of the routing entry.
        """
        return "/" + str(self.prefix)
