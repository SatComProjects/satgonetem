"""Deterministic IPv4 and optional IPv6 address-planning primitives."""

from satgonetem.addressing.ipv4 import (
    AddressPlanConfigurationError,
    AddressPlanError,
    AddressPlanExhausted,
    Ipv4AddressPlan,
    LinkAddress,
    NodeAddress,
    NodeKind,
)
from satgonetem.addressing.ipv6 import (
    Ipv6AddressPlan,
    Ipv6LinkAddress,
    Ipv6NodeAddress,
)

__all__ = [
    "AddressPlanConfigurationError",
    "AddressPlanError",
    "AddressPlanExhausted",
    "Ipv4AddressPlan",
    "Ipv6AddressPlan",
    "Ipv6LinkAddress",
    "Ipv6NodeAddress",
    "LinkAddress",
    "NodeAddress",
    "NodeKind",
]
