from __future__ import annotations

import ipaddress


class Interface:
    """A network interface with an optional IPv4 address and prefix.

    Point-to-point emulated links use ``/31`` and node loopbacks use ``/32`` by
    default.  The prefix is stored with the address instead of being repeated
    in every routing/daemon module.
    """

    def __init__(self, name: str = "Default", iface_type: str = ""):
        self.ipv4: str = ""
        self.prefix_len: int = 32 if iface_type == "lo" else 31
        self.name: str = name
        self.peer: Interface
        self.is_monitored: bool = False
        self.is_active: bool = False
        self.previously_active: bool = True

        self.type: str = iface_type  # Type of interface, e.g., GndLink or ISL
        self.delay: int = 0
        self.mac_address: str | None = None

    def set_ipv4(self, ip_address: str, prefix_len: int | None = None) -> None:
        """Set the interface IPv4 address and prefix length.

        Args:
            ip_address: Canonical IPv4 address string.
            prefix_len: Prefix length. Defaults to the interface's current
                prefix (31 for links, 32 for loopbacks).

        Raises:
            ValueError: If the address or prefix is invalid.
        """
        try:
            address = ipaddress.IPv4Address(ip_address)
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"Invalid IPv4 address: {ip_address!r}") from exc

        effective_prefix = self.prefix_len if prefix_len is None else prefix_len
        if not isinstance(effective_prefix, int) or not 0 <= effective_prefix <= 32:
            raise ValueError(
                f"Invalid IPv4 prefix length: {effective_prefix!r}; expected 0..32"
            )

        self.ipv4 = str(address)
        self.prefix_len = effective_prefix

    def set_ip(self, ip_address: str) -> None:
        """Backward-compatible alias for :meth:`set_ipv4`."""
        self.set_ipv4(ip_address)

    @property
    def network(self) -> ipaddress.IPv4Network | None:
        """Return the interface network, or ``None`` when no address is set."""
        if not self.ipv4:
            return None
        return ipaddress.IPv4Interface(f"{self.ipv4}/{self.prefix_len}").network

    def get_iname(self) -> str:
        """Return the name of the interface inside the container."""
        return "lo" if self.type == "lo" else "eth" + self.name.split(".")[1]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Interface):
            return NotImplemented
        return self.name == other.name

    def get_mac_address(self) -> str | None:
        """Return the configured MAC address, if any."""
        return self.mac_address

    def set_mac_address(self, mac_address) -> None:
        self.mac_address = mac_address
