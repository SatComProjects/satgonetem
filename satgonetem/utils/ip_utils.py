import numpy as np


class IPUtils:

    IPV4_ADDRESS_LENGTH = 32
    IPV6_ADDRESS_LENGTH = 128

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
    def get_ipv6_address_entropy(ipv6_address: str) -> float:
        """Calculate the entropy of an IPv6 address."""
        if not IPUtils.is_valid_ipv6(ipv6_address):
            raise ValueError("Invalid IPv6 address.")

        parts = ipv6_address.split(":")

        count_0 = 0
        count_1 = 0

        for part in parts:
            binary_part = f"{int(part, 16):016b}"
            count_0 += binary_part.count("0")
            count_1 += binary_part.count("1")

        p_1 = count_1 / (count_0 + count_1)
        p_0 = count_0 / (count_0 + count_1)

        if p_0 == 0 or p_1 == 0:
            return 0
        entropy = -(p_0 * np.log2(p_0) + p_1 * np.log2(p_1))
        return float(128 * entropy)

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
    def binary_to_ipv6(binary: str) -> str:
        """
        Returns an ipv6 string representation of a 128-bit binary number.
        :param binary: A 128-bit binary number (string of '0'/'1')
        :return: A full ipv6 address in hexadecimal (8 groups of 4 hex digits)
        """
        if len(binary) != 128 or any(c not in "01" for c in binary):
            raise ValueError("Input must be a 128-bit binary string of '0' and '1'")
        # split into eight 16-bit segments
        segments = [binary[i : i + 16] for i in range(0, 128, 16)]
        # convert each to hex, zero-pad to 4 digits
        hex_segs = [format(int(seg, 2), "x").zfill(4) for seg in segments]
        return ":".join(hex_segs)

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
    def is_valid_ipv6(ip: str) -> bool:
        """Check if the given string is a valid IPv6 address.
        Supports compressed forms (e.g., "2001:db8::1").
        """
        if not isinstance(ip, str) or not ip:
            return False

        # Quick character filter (only hex digits and colons allowed)
        allowed = set("0123456789abcdefABCDEF:")
        if any(c not in allowed for c in ip):
            return False

        # There can be at most one "::" (zero-compression)
        if ip.count("::") > 1:
            return False

        def valid_side(side: str) -> list[str] | None:
            """Validate a side of '::'. Empty side -> zero groups."""
            if side == "":
                return []
            parts = side.split(":")
            for p in parts:
                if (
                    not p
                    or len(p) > 4
                    or not all(ch in "0123456789abcdefABCDEF" for ch in p)
                ):
                    return None
            return parts

        if "::" in ip:
            left, right = ip.split("::", 1)
            left_parts = valid_side(left)
            right_parts = valid_side(right)
            if left_parts is None or right_parts is None:
                return False
            total_groups = len(left_parts) + len(right_parts)
            # '::' must compress at least one group, so total < 8
            return total_groups < 8
        else:
            parts = ip.split(":")
            if len(parts) != 8:
                return False
            for part in parts:
                if (
                    not part
                    or len(part) > 4
                    or not all(c in "0123456789abcdefABCDEF" for c in part)
                ):
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
    def get_ipv6_address(
        node: int,
        peer: int,
        type: str,
        loopback: bool = False,
    ) -> tuple[str, str]:
        """Generate an IPv4 address based on node, peer, type, and loopback."""

        if type not in ["Satellite", "GroundStation", "UserTerminal"]:
            raise ValueError(
                "Type must be either 'Satellite', 'GroundStation' or 'UserTerminal'."
            )
        if not (0 <= node < 2**26):
            raise ValueError("Node must be between 0 and 16383.")
        if not (0 <= peer < 2**26):
            raise ValueError("Peer must be between 0 and 16383.")

        if type == "Satellite":
            owner_code = "0010"
        elif type == "GroundStation":
            owner_code = "0001"
        elif type == "UserTerminal":
            owner_code = "0011"

        if loopback:
            loopback_code = "01"
        else:
            loopback_code = "00"

        base_ip = (
            owner_code + f"{node:0>26b}" + loopback_code + f"{peer:0>26b}"
            f"{0:0>68b}"  # Reserved bits for future use (68 bits, 17 hex digits, 4 bits per hex digit = 17*4 = 68 bits
        )
        if node < peer:
            first_ip = IPUtils.binary_to_ipv6(base_ip + "01")
            second_ip = IPUtils.binary_to_ipv6(base_ip + "10")
        else:
            first_ip = IPUtils.binary_to_ipv6(base_ip + "10")
            second_ip = IPUtils.binary_to_ipv6(base_ip + "01")

        if not IPUtils.is_valid_ipv6(first_ip) or not IPUtils.is_valid_ipv6(second_ip):
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
    def ipv6_to_binary(ip_address: str) -> str:
        """Convert an IPv6 address to a 128-bit binary string."""
        if not IPUtils.is_valid_ipv6(ip_address):
            raise ValueError("Invalid IPv6 address.")

        parts = ip_address.split(":")
        binary_parts = [f"{int(part, 16):016b}" for part in parts]
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

    @staticmethod
    def summarize_ipv6_address(ip_address: str, prefix: int) -> str:
        """Summarize an IPv6 address with a given prefix length."""
        if not (0 <= prefix <= 128):
            raise ValueError("Prefix must be between 0 and 128.")
        if not IPUtils.is_valid_ipv6(ip_address):
            raise ValueError("Invalid IPv6 address.")

        binary_ip = IPUtils.ipv6_to_binary(ip_address)

        summarized_ip_binary = binary_ip[:prefix] + "0" * (
            IPUtils.IPV6_ADDRESS_LENGTH - prefix
        )

        summarized_ip = IPUtils.binary_to_ipv6(summarized_ip_binary)
        return summarized_ip
