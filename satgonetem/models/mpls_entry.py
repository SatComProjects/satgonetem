"""
MPLS (Multi-Protocol Label Switching) data models for satellite network emulation.

This module provides classes for SR-MPLS (Segment Routing MPLS) entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface


# MPLS Reserved Labels (RFC 3032)
MPLS_LABEL_IMPLICIT_NULL = 3  # Used for PHP (Penultimate Hop Popping)

# Minimum and maximum usable labels
MPLS_LABEL_MIN = 16
MPLS_LABEL_MAX = 1048575


@dataclass
class MPLSConfig:
    """
    MPLS configuration parameters.

    Attributes:
        enabled: Whether MPLS routing is enabled
        label_range_start: First usable label (minimum 16)
        label_range_end: Last usable label (maximum 1048575)
        use_ldp: Use Label Distribution Protocol for automatic label distribution
        use_php: Use Penultimate Hop Popping
        ttl: Default MPLS TTL value
        use_sr: Use Segment Routing (source-routed MPLS)
        sr_node_sid_base: Base label for Node SIDs in SR mode
    """

    enabled: bool = False
    label_range_start: int = MPLS_LABEL_MIN
    label_range_end: int = MPLS_LABEL_MAX
    use_ldp: bool = False
    use_php: bool = False
    ttl: int = 64
    use_sr: bool = True
    sr_node_sid_base: int = 16000

    def __post_init__(self):
        if self.label_range_start < MPLS_LABEL_MIN:
            self.label_range_start = MPLS_LABEL_MIN
        if self.label_range_end > MPLS_LABEL_MAX:
            self.label_range_end = MPLS_LABEL_MAX
        if self.label_range_start >= self.label_range_end:
            raise ValueError("label_range_start must be less than label_range_end")


@dataclass
class SRNodeSIDEntry:
    """
    Segment Routing Node SID entry for LOCAL delivery.

    This entry is installed for the node's OWN Node SID:
    "If I receive my SID, pop it and deliver locally (or process next label)."

    Attributes:
        node_sid: The Node SID label for this node
        node_name: Name of the node (for logging)
    """

    node_sid: int
    node_name: str = ""

    def to_iproute2_command(self) -> str:
        """
        Generate the iproute2 command to install the local Node SID rule.

        The rule pops the label and delivers to lo. Linux kernel will then
        process the next label in the stack (if any) or deliver the IP packet.

        Returns:
            iproute2 command string
        """
        # Pop our SID and deliver to lo - kernel processes next label if present
        return f"ip -f mpls route replace {self.node_sid} dev lo"

    def to_iproute2_batch_line(self) -> str:
        """
        Generate a line for ip -batch to install the local Node SID rule.

        Returns:
            Command line without the leading 'ip' word, suitable for ip -batch.
        """
        return f"-f mpls route replace {self.node_sid} dev lo"

    def __str__(self) -> str:
        return f"SRNodeSID: {self.node_name} -> label {self.node_sid} (pop to local)"


@dataclass
class SRForwardEntry:
    """
    Segment Routing forwarding entry for TRANSIT traffic.

    Each node needs forwarding entries for all OTHER Node SIDs in the network.
    When a packet arrives with another node's SID, we:
    1. Pop the label (remove the top SID from the stack)
    2. Forward to the next hop toward that destination

    The Linux kernel will then process the next label in the stack.

    Attributes:
        target_sid: The Node SID this entry handles
        next_hop: IP address of the next hop towards target
        interface: Outgoing interface
        target_name: Name of the target node (for logging)
    """

    target_sid: int
    next_hop: str
    interface: "Interface"
    target_name: str = ""

    def to_iproute2_command(self) -> str:
        """
        Generate the iproute2 command to install the SR forwarding rule.

        When we receive a packet with target_sid, pop it and forward to next_hop.
        The 'via inet' format tells Linux to forward as IP after popping.

        Returns:
            iproute2 command string
        """
        iface_name = self.interface.get_iname()
        # Pop the label and forward to next hop - kernel handles remaining labels
        return f"ip -f mpls route replace {self.target_sid} via inet {self.next_hop} dev {iface_name}"

    def to_iproute2_batch_line(self) -> str:
        """
        Generate a line for ip -batch to install the SR forwarding rule.

        Returns:
            Command line without the leading 'ip' word, suitable for ip -batch.
        """
        iface_name = self.interface.get_iname()
        return f"-f mpls route replace {self.target_sid} via inet {self.next_hop} dev {iface_name}"

    def __str__(self) -> str:
        return f"SRForward: label {self.target_sid} ({self.target_name}) -> pop via {self.next_hop}"


@dataclass
class SRLabelStackEntry:
    """
    Segment Routing label stack entry for source routing.

    At the ingress (source) node, we push a stack of labels that
    encodes the entire path. Each label is a Node SID of a transit node.

    Attributes:
        destination: Destination IP address/prefix
        label_stack: List of labels to push (bottom to top order)
        next_hop: IP of the first hop
        interface: Outgoing interface
        fec_prefix: Prefix length for the FEC
    """

    destination: str
    label_stack: List[int]
    next_hop: str
    interface: "Interface"
    fec_prefix: int = 32

    def to_iproute2_command(self) -> str:
        """
        Generate the iproute2 command to install the SR route.

        The label stack is specified in the order they should appear
        in the packet (bottom to top), separated by '/'.

        Example: For path GS1 -> Sat3 -> Sat7 -> GS2
                 label_stack = [SID(Sat3), SID(Sat7)] = [16003, 16007]
                 Command: ip route replace <dest> encap mpls 16003/16007 via <next_hop>

        Returns:
            iproute2 command string
        """
        iface_name = self.interface.get_iname()

        if not self.label_stack:
            # No labels to push - direct IP forwarding
            return f"ip route replace {self.destination}/{self.fec_prefix} via {self.next_hop} dev {iface_name}"

        # Join labels with '/' for the MPLS encap command
        # Labels are pushed in order: first label becomes innermost (processed last)
        labels_str = "/".join(str(l) for l in self.label_stack)

        return f"ip route replace {self.destination}/{self.fec_prefix} encap mpls {labels_str} via {self.next_hop} dev {iface_name}"

    def to_iproute2_batch_line(self) -> str:
        """
        Generate a line for ip -batch to install the SR route.

        Returns:
            Command line without the leading 'ip' word, suitable for ip -batch.
        """
        iface_name = self.interface.get_iname()

        if not self.label_stack:
            return f"route replace {self.destination}/{self.fec_prefix} via {self.next_hop} dev {iface_name}"

        labels_str = "/".join(str(l) for l in self.label_stack)
        return f"route replace {self.destination}/{self.fec_prefix} encap mpls {labels_str} via {self.next_hop} dev {iface_name}"

    def __str__(self) -> str:
        stack_str = (
            "/".join(str(l) for l in self.label_stack)
            if self.label_stack
            else "(empty)"
        )
        return f"SRLabelStack: {self.destination}/{self.fec_prefix} -> [{stack_str}] via {self.next_hop}"
