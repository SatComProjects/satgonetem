"""Pure link-identity helpers for topology sync.

Keeps the local/satcom node-type switch out of the mixin so it can be tested
without constructing a TopologyManager.
"""

from __future__ import annotations

from typing import Any, Iterable

from sat_com_model.models import GroundStation as SatComGroundStation
from sat_com_model.models import Satellite as SatComSatellite

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.satellite import Satellite


def _node_token(node: Any) -> str:
    if isinstance(node, (Satellite, SatComSatellite)):
        prefix = "Sat"
    elif isinstance(node, (GroundStation, SatComGroundStation)):
        prefix = "Gnd"
    else:
        raise ValueError("Source and target must be either Satellite or GroundStation")

    satcom = getattr(node, "satcom_object", node)
    return f"{prefix}{getattr(satcom, 'topology_uniq_id', None)}"


def build_link_key(source: Any, target: Any) -> frozenset[str]:
    """Return a direction-independent key for a link between two nodes."""
    return frozenset((_node_token(source), _node_token(target)))


def removed_link_keys(
    current_keys: Iterable[frozenset[str]],
    new_keys: Iterable[frozenset[str]],
) -> set[frozenset[str]]:
    """Return keys present in *current_keys* but absent from *new_keys*."""
    return set(current_keys) - set(new_keys)
