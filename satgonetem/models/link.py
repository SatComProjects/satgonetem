import logging
from typing import Optional

from satgonetem.link_budget.config import LinkBudgetConfig
from satgonetem.link_budget.geometry import get_elevation_angle
from satgonetem.link_budget.service import LinkBudgetInputs, LinkBudgetService
from satgonetem.link_budget.strategies import (
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)
from satgonetem.link_budget.antenna import Antenna
from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.utils.utils import distance_3d_km

from sat_com_model.models import Link as SatComLink, GroundObjectLink

# Use a module logger to control verbosity more easily
logger = logging.getLogger(__name__)

# Default frequencies for satellite-ground links (GHz)
DEFAULT_DOWNLINK_FREQ_GHZ = 19.0
DEFAULT_UPLINK_FREQ_GHZ = 14.25


class Link:

    def __init__(
        self,
        source: Node,
        target: Node,
        distance: float,
        type: str,
        direction: str = "",
        is_active: bool = True,
        default_capacity_kbps: int = 1000,
        use_budget: bool = False,
        link_budget_config: Optional[LinkBudgetConfig] = None,
    ):
        self.source: Node = source
        self.target: Node = target
        self.distance: float = distance
        self.type: str = type
        self.is_active: bool = is_active
        self.direction: str = direction
        self.use_budget: bool = use_budget
        self.link_budget_config: Optional[LinkBudgetConfig] = link_budget_config

        self.default_capacity_kbps = default_capacity_kbps
        self.peer1_capacity: int = 0
        self.peer2_capacity: int = 0

        self.peer_interfaces: list["Interface"] = []

        self.delay = int((self.distance / 299_792_458) * 1000)  # in ms, speed of light

        # gRPC related flags
        self.to_update = False
        self.to_add = False
        self.to_remove = False
        self.to_delete = False

        # SatComTopology object
        self.satcom_object: SatComLink | None = None

        # Network usage
        self.rx = 0
        self.tx = 0

        self.update_link_capacities()

    # ------------------------------------------------------------------

    def update_link_capacities(self) -> None:
        """A method that updates the link capacities based on the link budget if enabled."""
        if self.use_budget and self.type == "GroundStationLink":
            self._compute_budget_capacity()
        else:
            self.peer1_capacity = self.default_capacity_kbps
            self.peer2_capacity = self.default_capacity_kbps

    # ------------------------------------------------------------------
    # Budget helpers (SOLID: thin wrappers around LinkBudgetService)
    # ------------------------------------------------------------------

    def _identify_sat_and_gs(self) -> tuple[Node, Node]:
        """Return *(satellite, ground_station)* regardless of link direction."""
        if getattr(self.source, "type", "") == "GroundStation":
            return self.target, self.source

        return self.source, self.target

    def _compute_elevation_angle(self, sat: Node, gs: Node) -> float:
        """Compute the elevation angle from the ground station to the satellite."""
        try:
            central_angle = get_elevation_angle(
                sat_coordinates=(
                    sat.position["latitude"],
                    sat.position["longitude"],
                    sat.position["altitude"],
                ),
                gnd_coordinates=(
                    gs.position["latitude"],
                    gs.position["longitude"],
                    gs.position["altitude"],
                ),
            )
            return 90.0 - central_angle
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute elevation angle for %s->%s: %s",
                sat.name,
                gs.name,
                exc,
            )
            return 30.0  # fallback typical elevation

    def _compute_budget_capacity(self) -> None:
        """Compute capacity for a *GroundStationLink* using the link budget.

        Downlink (SAT -> GS) is computed with a ModCodCapacityStrategy and
        stored as peer1_capacity.  Uplink (GS -> SAT) is computed with a
        ShannonCapacityStrategy and stored as peer2_capacity.

        If either node lacks an antenna the method falls back to the default
        static capacity from get_link_capacity, setting both peers equally.
        """
        sat, gs = self._identify_sat_and_gs()

        if sat.antenna is None or gs.antenna is None:
            logger.debug(
                "Missing antenna on %s or %s; falling back to default capacity.",
                sat.name,
                gs.name,
            )
            self.peer1_capacity = self.default_capacity_kbps
            self.peer2_capacity = self.default_capacity_kbps
            return

        elevation = self._compute_elevation_angle(sat, gs)
        gs_lat = float(gs.position.get("latitude", 0.0))
        gs_lon = float(gs.position.get("longitude", 0.0))
        gs_diam = gs.antenna.diameter if gs.antenna else 2.0

        if self.link_budget_config is not None:
            bw_dl = float(self.link_budget_config.bandwidth_hz_downlink)
            bw_ul = float(self.link_budget_config.bandwidth_hz_uplink)
        else:
            bw_dl = 500e6
            bw_ul = 500e6
        rx_tsys = float(getattr(self, "rx_tsys_k", 100.0))
        unav = float(getattr(self, "unavailability_percent", 0.1))

        dl_freq = (
            self.link_budget_config.downlink_freq_ghz
            if self.link_budget_config
            else DEFAULT_DOWNLINK_FREQ_GHZ
        )
        ul_freq = (
            self.link_budget_config.uplink_freq_ghz
            if self.link_budget_config
            else DEFAULT_UPLINK_FREQ_GHZ
        )

        # Downlink (SAT → GS) – MODCOD-based
        dl_inputs = LinkBudgetInputs(
            tx_antenna=sat.antenna,
            rx_antenna=gs.antenna,
            frequency_ghz=dl_freq,
            distance_km=self.distance / 1000.0,
            elevation_angle=elevation,
            gs_lat=gs_lat,
            gs_lon=gs_lon,
            gs_diameter=gs_diam,
            bandwidth_hz=bw_dl,
            rx_tsys_k=rx_tsys,
            unavailability_percent=unav,
        )
        dl_service = LinkBudgetService(capacity_strategy=ModCodCapacityStrategy())
        self.peer1_capacity = dl_service.compute_one_way(dl_inputs)

        # Uplink (GS → SAT) – Shannon upper bound
        ul_inputs = LinkBudgetInputs(
            tx_antenna=gs.antenna,
            rx_antenna=sat.antenna,
            frequency_ghz=ul_freq,
            distance_km=self.distance / 1000.0,
            elevation_angle=elevation,
            gs_lat=gs_lat,
            gs_lon=gs_lon,
            gs_diameter=gs_diam,
            bandwidth_hz=bw_ul,
            rx_tsys_k=rx_tsys,
            unavailability_percent=unav,
        )
        ul_service = LinkBudgetService(capacity_strategy=ShannonCapacityStrategy())
        self.peer2_capacity = ul_service.compute_one_way(ul_inputs)

        logger.info(
            "Link budget %s->%s | elev=%.2f deg, DL=%d kbps, UL=%d kbps",
            sat.name,
            gs.name,
            elevation,
            self.peer1_capacity,
            self.peer2_capacity,
        )

    # Sync helpers

    def sync_distance_from_satcom_and_delay(self) -> None:
        """
        A method that syncs the distance from the satcom object.
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set")

        new_distance = (
            distance_3d_km(
                lat1=self.source.position["latitude"],
                lon1=self.source.position["longitude"],
                alt1=self.source.position["altitude"],
                lat2=self.target.position["latitude"],
                lon2=self.target.position["longitude"],
                alt2=self.target.position["altitude"],
            )
            * 1000
        )  # Convert to meters

        new_delay = int((new_distance / 299_792_458) * 1000)  # in ms, speed of light

        if self.satcom_object.type == "GroundObjectLink" and isinstance(self.satcom_object, GroundObjectLink):
            if self.satcom_object.recorded_latency is not None:
                new_delay = self.satcom_object.recorded_latency
            else:
                new_delay = int((new_distance / 200_000_000) * 1000) # in ms speed of the light inside fiber optic

        if new_delay != self.delay:
            self.delay = new_delay
            self.distance = new_distance
            self.to_update = True
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Link):
            return NotImplemented
        return self.source == other.source and self.target == other.target

    def get_names(self) -> tuple[str, str]:
        return self.source.name + "." + str(
            self.target.id
        ), self.target.name + "." + str(self.source.id)

    def update_interfaces_state(self) -> None:
        """
        A method that updates the state of the interfaces.
        """
        for interface in self.peer_interfaces:
            interface.is_active = self.is_active

        return None

    def get_capacity(self) -> int:
        """Return peer1_capacity (downlink for SAT->GS links, uplink otherwise).

        Returns:
            peer1_capacity in kbps.
        """
        return self.peer1_capacity
