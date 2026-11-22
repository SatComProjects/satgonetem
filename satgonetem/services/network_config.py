"""Network configuration for :class:`TopologyManager`.

This module owns the :class:`NetworkConfig` dataclass and the two helpers that
copy configuration values to and from a manager instance.  It deliberately
depends only on the addressing package so it stays leaf-ward of the services
facade.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from satgonetem.addressing import Ipv6AddressPlan

# Upper bound for the netem queue limit and the interface txqueuelen, in
# packets. It mirrors the GoNetEm server's maxQueuePackets so a configuration
# accepted here is never rejected by the server.
MAX_LINK_QUEUE_PACKETS = 1_000_000


@dataclass
class NetworkConfig:
    """Network configuration parameters for TopologyManager.

    All fields are optional; defaults match the original built-in values.
    Pass an instance to TopologyManager.__init__ to override any subset.

    Attributes:
        project_name: Human-readable name for the constellation project.
            Defaults to None, which causes TopologyManager to fall back to
            the simulation_manager's own project_name (or "Constellation").
        update_time: Tick interval in seconds between topology updates.
        gnd_link_capacity: Capacity in kbps for ground-station links.
        isl_link_capacity: Capacity in kbps for inter-satellite links.
        ground_object_link_capacity: Capacity in kbps for ground-object-to-ground-object links.
        link_netem_limit: netem queue limit in packets for emulated links.
            0 keeps the GoNetEm server default (1000 packets). This bounds the
            packets held by the netem qdisc, i.e. the modelled link buffer.
        link_txqueuelen: Transmit queue length in packets of the interfaces
            terminating an emulated link. 0 keeps the GoNetEm server default
            (1000 packets).
        link_loss_percent: Default packet loss percentage for new links.
        link_jitter_ms: Default netem jitter for new links, in milliseconds.
        protocol: Network-layer protocol string (e.g. "ipv4").
        routing: Default routing method (e.g. "static", "dynamic-ospf").
        satellite_image: Docker image tag used for satellite containers.
        network_launcher: Launcher backend identifier (e.g. "GONETEM").
        gonetem_server: Address of the GoNetem gRPC server.
        controller_veth_host: Host-side veth interface name for the controller link.
        controller_veth_peer: Container-side veth interface name for the controller link.
        controller_host_ip: IPv4 address assigned to the host side of the controller veth.
        controller_subnet_prefix: Subnet prefix length for controller-plane addresses.
        controller_bridge_name: Name of the OVS bridge inside the controller container.
        controller_node_iface_pattern: Python format string for node controller interfaces.
            Must format to ``eth<digits>``; ``{node_id}`` receives the controller
            interface index (50000 + node id).
        ground_station_pool: Data-plane pool subdivided into ground-station blocks.
        satellite_pool: Data-plane pool subdivided into satellite blocks.
        user_terminal_pool: Data-plane pool subdivided into user-terminal blocks.
        ground_station_prefix_len: Prefix length of each ground-station block.
        satellite_prefix_len: Prefix length of each satellite block.
        user_terminal_prefix_len: Prefix length of each user-terminal block.
        ipv6_enabled: Enable the optional dual-stack IPv6 address plan.
        ipv6_ground_station_pool: ULA pool subdivided into ground-station /64s.
        ipv6_satellite_pool: ULA pool subdivided into satellite /64s.
        ipv6_user_terminal_pool: ULA pool subdivided into user-terminal /64s.
        ipv6_ground_station_prefix_len: Prefix length of a ground-station block.
        ipv6_satellite_prefix_len: Prefix length of a satellite block.
        ipv6_user_terminal_prefix_len: Prefix length of a user-terminal block.
    """

    project_name: str | None = None
    update_time: int = 5
    gnd_link_capacity: int = 100000
    isl_link_capacity: int = 100000
    ground_object_link_capacity: int = 100000
    link_netem_limit: int = 1000
    link_txqueuelen: int = 1000
    link_loss_percent: float = 0.0
    link_jitter_ms: int = 0
    protocol: str = "ipv4"
    routing: str = "static"
    satellite_image: str = "jariassuarez/sgnt:satellite"
    network_launcher: str = "GONETEM"
    gonetem_server: str = "localhost:10110"
    use_budget: bool = False

    # Data-plane IPv4 addressing.  The defaults tile 10.0.0.0/8:
    # ground stations /10, user terminals /10, satellites /9.
    ground_station_pool: str = "10.0.0.0/10"
    satellite_pool: str = "10.128.0.0/9"
    user_terminal_pool: str = "10.64.0.0/10"
    ground_station_prefix_len: int = 20
    satellite_prefix_len: int = 27
    user_terminal_prefix_len: int = 20

    # Optional dual-stack IPv6 addressing.  Disabled by default; every node
    # still receives IPv4 addresses.  The defaults are private ULA /40 pools
    # subdivided into /64 node blocks.
    ipv6_enabled: bool = False
    ipv6_ground_station_pool: str = "fd00:1::/40"
    ipv6_satellite_pool: str = "fd00:2::/40"
    ipv6_user_terminal_pool: str = "fd00:3::/40"
    ipv6_ground_station_prefix_len: int = 64
    ipv6_satellite_prefix_len: int = 64
    ipv6_user_terminal_prefix_len: int = 64

    # Controller connection settings
    controller_veth_host: str = "veth-host"
    controller_veth_peer: str = "veth-controller"
    controller_host_ip: str = "248.0.0.2"
    controller_subnet_prefix: int = 16
    controller_bridge_name: str = "Controller"
    controller_node_iface_pattern: str = "eth{node_id}"

    def __post_init__(self) -> None:
        """Validate the queue-length fields and the tick interval.

        Raises:
            TypeError: If a queue-length field is not an integer.
            ValueError: If a queue-length field is outside
                ``[0, MAX_LINK_QUEUE_PACKETS]``, or if ``update_time`` is not
                positive.
        """
        if self.update_time <= 0:
            raise ValueError(
                f"NetworkConfig.update_time must be positive, got {self.update_time}"
            )

        for name in ("link_netem_limit", "link_txqueuelen"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"NetworkConfig.{name} must be an int, got {value!r}")
            if value < 0 or value > MAX_LINK_QUEUE_PACKETS:
                raise ValueError(
                    f"NetworkConfig.{name} must be between 0 and "
                    f"{MAX_LINK_QUEUE_PACKETS} packets, got {value}"
                )

        loss = self.link_loss_percent
        if isinstance(loss, bool) or not isinstance(loss, (int, float)):
            raise TypeError(
                f"NetworkConfig.link_loss_percent must be a number, got {loss!r}"
            )
        if not math.isfinite(float(loss)) or not 0.0 <= float(loss) <= 100.0:
            raise ValueError(
                "NetworkConfig.link_loss_percent must be between 0 and 100, "
                f"got {loss!r}"
            )
        jitter = self.link_jitter_ms
        if isinstance(jitter, bool) or not isinstance(jitter, int):
            raise TypeError(
                f"NetworkConfig.link_jitter_ms must be an int, got {jitter!r}"
            )
        if jitter < 0:
            raise ValueError(
                f"NetworkConfig.link_jitter_ms must be non-negative, got {jitter}"
            )

        pattern = self.controller_node_iface_pattern
        if "{node_id}" not in pattern:
            raise ValueError(
                "controller_node_iface_pattern must contain {node_id}, "
                f"got {pattern!r}"
            )
        try:
            formatted = pattern.format(node_id=0)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"Invalid controller_node_iface_pattern {pattern!r}: {exc}"
            ) from exc
        if not formatted.startswith("eth") or not formatted[3:].isdigit():
            raise ValueError(
                "controller_node_iface_pattern must format to 'eth<digits>', "
                f"got {formatted!r}"
            )

        if not isinstance(self.ipv6_enabled, bool):
            raise TypeError(
                f"NetworkConfig.ipv6_enabled must be a bool, got {self.ipv6_enabled!r}"
            )

        for name in (
            "ipv6_ground_station_prefix_len",
            "ipv6_satellite_prefix_len",
            "ipv6_user_terminal_prefix_len",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"NetworkConfig.{name} must be an int, got {value!r}")
            if value < 0 or value > 128:
                raise ValueError(
                    f"NetworkConfig.{name} must be between 0 and 128, got {value}"
                )

        if self.ipv6_enabled:
            # Constructing the plan validates address families, ULA ranges,
            # pool/prefix fit, and overlap before any topology is built.
            Ipv6AddressPlan(
                ground_station_pool=self.ipv6_ground_station_pool,
                satellite_pool=self.ipv6_satellite_pool,
                user_terminal_pool=self.ipv6_user_terminal_pool,
                ground_station_prefix_len=self.ipv6_ground_station_prefix_len,
                satellite_prefix_len=self.ipv6_satellite_prefix_len,
                user_terminal_prefix_len=self.ipv6_user_terminal_prefix_len,
            )

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        base: NetworkConfig | None = None,
    ) -> NetworkConfig:
        """Build a config from a mapping, optionally merged over *base*.

        Args:
            values: Field overrides. Unknown field names are rejected.
            base: Config providing defaults for fields absent from *values*.
                When ``None``, the dataclass defaults are used. *base* is not
                mutated.

        Raises:
            TypeError: If *values* is not a mapping.
            ValueError: If *values* contains an unknown field name.
        """
        if not isinstance(values, Mapping):
            raise TypeError(
                "NetworkConfig.from_mapping expects a mapping, "
                f"got {type(values).__name__}"
            )

        known = {field.name for field in fields(cls)}
        unknown = sorted(str(name) for name in set(values) - known)
        if unknown:
            raise ValueError("Unknown NetworkConfig field(s): " + ", ".join(unknown))

        if base is None:
            return cls(**values)

        merged = {field.name: getattr(base, field.name) for field in fields(cls)}
        merged.update(values)
        return cls(**merged)


def config_from_instance(instance: Any) -> NetworkConfig:
    """Build a NetworkConfig from the matching fields on *instance*."""
    values = {
        field.name: getattr(instance, field.name)
        for field in fields(NetworkConfig)
        if hasattr(instance, field.name)
    }
    return NetworkConfig(**values)


def apply_config(instance: Any, config: NetworkConfig) -> None:
    """Copy *config* values onto *instance*'s network attributes.

    Args:
        instance: Object receiving the attributes.
        config: NetworkConfig instance whose values are written to *instance*.
            If config.project_name is None, falls back to the instance's
            simulation manager project_name attribute, or "Constellation".
    """
    base_name = config.project_name or getattr(
        instance.simulation_manager, "project_name", "Constellation"
    )
    instance.project_name = f"{base_name}_{int(time.time())}"
    for field in fields(NetworkConfig):
        if field.name != "project_name":
            setattr(instance, field.name, getattr(config, field.name))
