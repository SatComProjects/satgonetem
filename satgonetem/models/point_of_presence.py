"""PointOfPresence - terrestrial peering/interconnect node.

Points of presence use the same container model as ground stations and user
terminals. ``node.type`` distinguishes them; links between PoPs and other
ground objects are modelled as ``GroundObjectLink``.
"""

from __future__ import annotations

from satgonetem.models.ground_station import GroundStation


class PointOfPresence(GroundStation):
    """A point-of-presence node with the ground-station container behaviour."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.type = "PointOfPresence"
