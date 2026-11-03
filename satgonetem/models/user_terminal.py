"""UserTerminal - terrestrial end-user node in the emulator.

User terminals share the ground-station networking container model but use the
V5 ``UserTerminal`` topology object and the ``UserTerminalLink`` link type.
They are stored in ``TopologyManager.ground_stations`` alongside gateways so
the launcher, addressing plan, graph, and routing code paths remain uniform;
``node.type`` distinguishes them.
"""

from __future__ import annotations

from satgonetem.models.ground_station import GroundStation


class UserTerminal(GroundStation):
    """A user terminal node with the ground-station container behaviour."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.type = "UserTerminal"
