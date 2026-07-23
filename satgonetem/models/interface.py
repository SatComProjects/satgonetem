from __future__ import annotations


from satgonetem.utils.ip_utils import IPUtils


class Interface:

    def __init__(self, name: str = "Default", iface_type: str = ""):

        self.ipv4: str = ""
        self.name: str = name
        self.peer: Interface
        self.is_monitored: bool = False
        self.is_active: bool = False
        self.previously_active: bool = True

        self.type: str = iface_type  # Type of interface, e.g., GndLink or ISL
        self.delay: int = 0
        self.mac_address: str | None = None

    def set_ip(self, ip_address: str) -> None:
        """
        A method that sets the IP of the Interface.
        """
        self.ipv4 = ip_address

    def get_iname(self) -> str:
        """
        A method that returns the name of the interface.
        """
        return "lo" if self.type == "lo" else "eth" + self.name.split(".")[1]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Interface):
            return NotImplemented
        return self.name == other.name

    def set_ipv4_address(self) -> None:
        """
        A method that sets the ipv4 address of the interface.
        """

        if self.ipv4:
            return  # IP already set

        is_loopback = self.type == "lo"
        local_name = self.name.split("_")[1] if is_loopback else self.name.split(".")[0]
        local_id = int(local_name[3:])

        if "Gnd" in local_name:
            node_type = "GroundStation"
        elif "Sat" in local_name:
            node_type = "Satellite"
        else:
            raise ValueError(
                f"Interface type {self.type} does not match node type {local_name}."
            )

        if is_loopback:
            loopback_ip, _ = IPUtils.get_ipv4_address(
                node=local_id, peer=local_id, type=node_type, loopback=True
            )
            self.set_ip(loopback_ip)
        else:
            peer_id = int(self.name.split(".")[1])
            local_ip, peer_ip = IPUtils.get_ipv4_address(
                node=local_id, peer=peer_id, type=node_type, loopback=False
            )
            self.set_ip(local_ip)
            self.peer.set_ip(peer_ip)

    def get_mac_address(self) -> str | None:
        """
        A method that returns the mac address of the interface.
        Mac Addresses are not configured by default. Call set_mac_address() to set a mac address for the interface.
        Returns:

        """
        return self.mac_address

    def set_mac_address(self, src_mac_address) -> None:
        self.mac_address = src_mac_address
