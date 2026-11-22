"""Peer interface index used by the static routing daemon.

Builds and caches ``{peer_id: Interface}`` lookups per node so route planning
does not rescan interface name strings on every update.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.satellite import Satellite
from satgonetem.utils.utils import get_interface_from_name

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface


_PEER_IFACE_PATTERN = re.compile(r"^(Gnd|Sat)\d+\.\d+$")


class PeerInterfaceIndex:
    """Cache of peer-id to interface maps for the topology's nodes."""

    def __init__(self, topology) -> None:
        """Initialize an empty interface index for *topology*."""
        self._topology = topology
        self._iface_cache_version: int | None = None
        self._iface_cache = None

    @property
    def iface_cache(self):
        """The cached ``(gs_ifaces, sat_ifaces)`` tuple, if any."""
        return self._iface_cache

    @property
    def iface_cache_version(self) -> int | None:
        """The interface version the cached maps were built from."""
        return self._iface_cache_version

    def prebuild(self):
        """Build fast peer_id->Interface lookups for every node.

        The maps are cached by the topology's interface version and rebuilt
        only when link interfaces are added or removed.

        Returns:
            Tuple of (gs_ifaces, sat_ifaces) where each value is a dict
            mapping node_id to a {peer_id: Interface} dict.
        """
        version = getattr(self._topology, "interface_version", None)
        if (
            isinstance(version, int)
            and self._iface_cache is not None
            and self._iface_cache_version == version
        ):
            return self._iface_cache

        gs_ifaces = {
            gs.id: self.peer_iface_map(gs)
            for gs in self._topology.ground_stations.values()
        }
        sat_ifaces = {
            sat.id: self.peer_iface_map(sat)
            for sat in self._topology.satellites.values()
        }
        result = (gs_ifaces, sat_ifaces)
        if isinstance(version, int):
            self._iface_cache = result
            self._iface_cache_version = version
        return result

    def peer_iface_map(self, node) -> dict:
        """Return {peer_id: Interface} by parsing interface names.

        Args:
            node: A satellite or ground station node.

        Returns:
            Dict mapping integer peer_id to Interface objects.
        """
        pattern = _PEER_IFACE_PATTERN
        mapping = {}
        for iface in node.interfaces:
            name = str(iface.name)
            if not pattern.match(name):
                logging.warning(
                    "Interface '%s' on node '%s' does not match expected pattern, skipping.",
                    name,
                    getattr(node, "_id", repr(node)),
                )
                continue
            try:
                _, peer = name.split(".", 1)
                mapping[int(peer)] = iface
            except (ValueError, TypeError):
                continue
        return mapping

    def resolve_interface(self, src_node, src_id, next_hop, iface_map) -> Interface:
        """Resolve the outgoing Interface toward next_hop.

        Tries the prebuilt iface_map first, then falls back to name-based lookup.

        Args:
            src_node: The source node object.
            src_id: Integer ID of the source node.
            next_hop: Integer ID of the next-hop node.
            iface_map: {peer_id: Interface} for fast lookup, or None.

        Returns:
            The resolved Interface.

        Raises:
            ValueError: If no interface toward next_hop can be found.
        """
        interface = iface_map.get(int(next_hop)) if iface_map is not None else None
        if interface is None:
            match self._topology.satellites.get(int(next_hop)):
                case Satellite() as peer:
                    interface = get_interface_from_name(
                        src_node.interfaces, f"{src_id}.{peer.id}"
                    )
                case None:
                    match self._topology.ground_stations.get(int(next_hop)):
                        case GroundStation() as peer:  # type: ignore[misc]
                            interface = get_interface_from_name(
                                src_node.interfaces, f"{src_id}.{peer.id}"
                            )
                        case None:
                            pass
                        case _ as unexpected:
                            raise TypeError(
                                f"Expected GroundStation, got {type(unexpected)}"
                            )
                case _ as unexpected:
                    raise TypeError(f"Expected Satellite, got {type(unexpected)}")
        if interface is None:
            raise ValueError(
                f"Could not resolve interface from {src_node.name} (id={src_id}) "
                f"to next hop id={next_hop}"
            )
        return interface
