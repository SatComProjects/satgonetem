import numpy as np


class IPUtils:

    IPV4_ADDRESS_LENGTH = 32

    @staticmethod
    def get_ipv4_address_entropy(ipv4_address: str) -> float:
        """Calculate the entropy of an IPv4 address."""
        if not IPUtils.is_valid_ipv4(ipv4_address):
            raise ValueError("Invalid IPv4 address.")

        parts = ipv4_address.split(".")

        count_0 = 0
        count_1 = 0

        for part in parts:
            binary_part = f"{int(part):08b}"
            count_0 += binary_part.count("0")
            count_1 += binary_part.count("1")

        p_1 = count_1 / (count_0 + count_1)
        p_0 = count_0 / (count_0 + count_1)

        if p_0 == 0 or p_1 == 0:
            return 0
        entropy = -(p_0 * np.log2(p_0) + p_1 * np.log2(p_1))
        return float(32 * entropy)

    @staticmethod
    def quaddot(binary: str) -> str:
        "Returns a quad-dotted string representation of a 32-bit binary number" ""
        return "{}.{}.{}.{}".format(
            int(binary[0:8], 2),
            int(binary[8:16], 2),
            int(binary[16:24], 2),
            int(binary[24:], 2),
        )

    @staticmethod
    def is_valid_ipv4(ip: str) -> bool:
        """Check if the given string is a valid IPv4 address."""
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        for part in parts:
            if not part.isdigit() or not (0 <= int(part) <= 255):
                return False
        return True

    @staticmethod
    def get_ipv4_address(
        node: int,
        peer: int,
        type: str,
        loopback: bool = False,
    ) -> tuple[str, str] | str:
        """Generate an IPv4 address based on node, peer, type, and loopback."""

        if type not in ["Satellite", "GroundStation", "UserTerminal"]:
            raise ValueError(
                f"Type must be either 'Satellite', 'GroundStation' or 'UserTerminal'. Current type is: {type}"
            )
        if not (0 <= node < 2**13):
            raise ValueError("Node must be between 0 and 16383.")
        if not (0 <= peer < 2**13):
            raise ValueError("Peer must be between 0 and 16383.")
        owner_code = ""
        if type == "Satellite":
            owner_code = "10"
        elif type == "GroundStation":
            owner_code = "01"
        elif type == "UserTerminal":
            owner_code = "11"

        if loopback:
            loopback_code = "1"
        else:
            loopback_code = "0"

        base_ip = (
            owner_code + f"{node:0>13b}" + loopback_code + f"{peer:0>13b}" + "1" + "0"
        )
        if node < peer:
            first_ip = IPUtils.quaddot(base_ip + "0")
            second_ip = IPUtils.quaddot(base_ip + "1")
        else:
            first_ip = IPUtils.quaddot(base_ip + "1")
            second_ip = IPUtils.quaddot(base_ip + "0")

        if not IPUtils.is_valid_ipv4(first_ip) or not IPUtils.is_valid_ipv4(second_ip):
            raise ValueError("Generated IP addresses are not valid IPv4 addresses.")

        return first_ip, second_ip

    @staticmethod
    def ipv4_to_binary(ip_address: str) -> str:
        """Convert an IPv4 address to a 32-bit binary string."""
        if not IPUtils.is_valid_ipv4(ip_address):
            raise ValueError("Invalid IPv4 address.")

        parts = ip_address.split(".")
        binary_parts = [f"{int(part):08b}" for part in parts]
        return "".join(binary_parts)

    @staticmethod
    def summarize_ipv4_address(ip_address: str, prefix: int) -> str:
        """Summarize an IP address with a given prefix length."""
        if not (0 <= prefix <= 32):
            raise ValueError("Prefix must be between 0 and 32.")
        if not IPUtils.is_valid_ipv4(ip_address):
            raise ValueError("Invalid IPv4 address.")

        binary_ip = IPUtils.ipv4_to_binary(ip_address)

        summarized_ip_binary = binary_ip[:prefix] + "0" * (
            IPUtils.IPV4_ADDRESS_LENGTH - prefix
        )

        summarized_ip = IPUtils.quaddot(summarized_ip_binary)
        return summarized_ip

