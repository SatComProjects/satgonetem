import logging
import math
from datetime import datetime
from numbers import Real
from typing import Any, Dict, Optional

from satgonetem.link_budget.config import LinkBudgetConfig
from satgonetem.link_budget.geometry import get_elevation_angle
from satgonetem.link_budget.service import LinkBudgetInputs, LinkBudgetService
from satgonetem.link_budget.strategies import (
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)
from satgonetem.models.interface import Interface
from satgonetem.models.node import Node
from satgonetem.utils.utils import distance_3d_km

from sat_com_model.models import Link as SatComLink

# Use a module logger to control verbosity more easily
logger = logging.getLogger(__name__)

# Default frequencies for satellite-ground links (GHz)
DEFAULT_DOWNLINK_FREQ_GHZ = 19.0
DEFAULT_UPLINK_FREQ_GHZ = 14.25


def satcom_distance_km(satcom_object: SatComLink | None) -> Optional[float]:
    """Return the distance carried by a satComTopology link, in kilometres.

    V5 stores ``distance_km`` on every link; ``None`` or a negative value means
    the simulation did not provide one (for example a static ground-object
    link). The caller can then fall back to a geometric calculation.
    """
    if satcom_object is None:
        return None

    value = getattr(satcom_object, "distance_km", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    distance_km = float(value)
    if distance_km < 0 or not math.isfinite(distance_km):
        return None
    return distance_km


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

        # Link lifetime / optical state refreshed from the V5 link object.
        self.estimated_duration_s: Optional[float] = None
        self.estimated_disconnection_time: Optional[datetime] = None
        self.elevation_deg: Optional[float] = None
        self.temporary_unavailable: bool = False
        self.recovery_time: Optional[datetime] = None

        # Advanced optical model details, keyed by "source"/"destination".
        self.optical_link_model: Optional[str] = None
        self.optical_metrics: Dict[str, Any] = {}

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

    def refresh_link_capacities(self) -> bool:
        """Recompute capacities and mark the link for update if they changed.

        Returns:
            True if either peer capacity changed.
        """
        before = (self.peer1_capacity, self.peer2_capacity)
        self.update_link_capacities()
        changed = (self.peer1_capacity, self.peer2_capacity) != before
        if changed:
            self.to_update = True
        return changed

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
            elevation = get_elevation_angle(
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
            return elevation
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
        dl_capacity = dl_service.compute_one_way(dl_inputs)
        if dl_capacity > 0:
            self.peer1_capacity = dl_capacity
        else:
            logger.warning(
                "Downlink budget failed for %s->%s; using default %d kbps",
                sat.name,
                gs.name,
                self.default_capacity_kbps,
            )
            self.peer1_capacity = self.default_capacity_kbps

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
        ul_capacity = ul_service.compute_one_way(ul_inputs)
        if ul_capacity > 0:
            self.peer2_capacity = ul_capacity
        else:
            logger.warning(
                "Uplink budget failed for %s->%s; using default %d kbps",
                gs.name,
                sat.name,
                self.default_capacity_kbps,
            )
            self.peer2_capacity = self.default_capacity_kbps

        logger.debug(
            "Link budget %s->%s | elev=%.2f deg, DL=%d kbps, UL=%d kbps",
            sat.name,
            gs.name,
            elevation,
            self.peer1_capacity,
            self.peer2_capacity,
        )

    # Sync helpers

    @property
    def distance_km(self) -> float:
        """Link distance in kilometres."""
        return self.distance / 1000.0

    def update_distance_and_delay(self, new_distance: float) -> None:
        """Update distance and delay from an already-computed distance.

        Args:
            new_distance: New link distance in metres.
        """
        new_delay = int((new_distance / 299_792_458) * 1000)  # ms, speed of light

        # Keep the exact distance current even when the rounded delay does not
        # change; consumers such as routing and diagnostics need the precise
        # value, not a value quantised to the millisecond delay bucket.
        distance_changed = new_distance != self.distance
        self.distance = new_distance
        if new_delay != self.delay:
            self.delay = new_delay
            self.to_update = True
        if distance_changed and self.use_budget and self.type == "GroundStationLink":
            self.refresh_link_capacities()

    def sync_distance_from_satcom_and_delay(self) -> None:
        """
        A method that syncs the distance from the satcom object.
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set")

        satcom_distance = satcom_distance_km(self.satcom_object)
        if satcom_distance is not None:
            self.update_distance_and_delay(satcom_distance * 1000.0)
            return None

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

        self.update_distance_and_delay(new_distance)
        return None

    @staticmethod
    def _optional_float(
        value: object, minimum: Optional[float] = None
    ) -> Optional[float]:
        """Convert a V5 numeric field to a finite float, or return None."""
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        result = float(value)
        if not math.isfinite(result):
            return None
        if minimum is not None and result < minimum:
            return None
        return result

    def refresh_lifetime_from_satcom(self) -> None:
        """Refresh duration, disconnection time, elevation, and optical flags.

        This is the cheap subset used by the polling path for ground links.
        It leaves distance and active-state handling to the caller.
        """
        if self.satcom_object is None:
            return

        duration = getattr(self.satcom_object, "estimated_link_duration", None)
        if duration is None:
            duration = getattr(self.satcom_object, "estimated_duration", None)
        self.estimated_duration_s = self._optional_float(duration, minimum=0.0)

        disconnection = getattr(
            self.satcom_object, "estimated_disconnection_time", None
        )
        self.estimated_disconnection_time = (
            disconnection if isinstance(disconnection, datetime) else None
        )

        elevation = getattr(
            self.satcom_object, "elevation_above_horizon_angle", None
        )
        if elevation is None:
            elevation = getattr(self.satcom_object, "elevation_angle", None)
        self.elevation_deg = self._optional_float(elevation)

        self.temporary_unavailable = bool(
            getattr(self.satcom_object, "temporary_unavailable", False)
        )
        recovery = getattr(self.satcom_object, "recovery_time", None)
        self.recovery_time = recovery if isinstance(recovery, datetime) else None

    def remaining_duration_s(self, now: datetime) -> Optional[float]:
        """Return seconds until the predicted disconnection, or None.

        The absolute disconnection time is stored by V5, so this remains
        correct even when an event was missed or a kept link was not
        re-published.
        """
        if self.estimated_disconnection_time is None:
            return None
        return max(
            0.0,
            (self.estimated_disconnection_time - now).total_seconds(),
        )

    def is_expiring(self, now: datetime, horizon_s: float) -> bool:
        """Return True when the link is predicted to drop within the horizon."""
        if horizon_s <= 0:
            return False
        remaining = self.remaining_duration_s(now)
        return remaining is not None and remaining < horizon_s

    @classmethod
    def _constraint_metrics(cls, constraint: Any) -> Optional[Dict[str, Any]]:
        """Convert one V5 OpticalConstraint to a plain metrics dict."""
        if constraint is None:
            return None
        return {
            "azimuth_deg": cls._optional_float(
                getattr(constraint, "azimuth_angle_degree", None)
            ),
            "relative_azimuth_deg": cls._optional_float(
                getattr(constraint, "relative_azimuth_angle_degree", None)
            ),
            "elevation_deg": cls._optional_float(
                getattr(constraint, "elevation_angle_degree", None)
            ),
            "angular_velocity_deg_s": cls._optional_float(
                getattr(
                    constraint,
                    "angular_velocity_rate_degree_per_second",
                    None,
                )
            ),
            "range_km": cls._optional_float(
                getattr(constraint, "range_distance_km", None)
            ),
            "los_min_altitude_km": cls._optional_float(
                getattr(constraint, "los_min_altitude_km", None)
            ),
            "estimated_duration_s": cls._optional_float(
                getattr(constraint, "estimated_duration", None), minimum=0.0
            ),
            "range_rate_km_s": cls._optional_float(
                getattr(constraint, "range_rate", None)
            ),
        }

    def refresh_optical_from_satcom(self) -> None:
        """Refresh the advanced optical model and per-end constraint metrics."""
        if self.satcom_object is None:
            return

        model = getattr(self.satcom_object, "optical_link_model", None)
        self.optical_link_model = (
            str(getattr(model, "value", model)) if model is not None else None
        )
        self.optical_metrics = {
            "source": self._constraint_metrics(
                getattr(self.satcom_object, "source_constraint", None)
            ),
            "destination": self._constraint_metrics(
                getattr(self.satcom_object, "destination_constraint", None)
            ),
        }

    def refresh_from_satcom(self) -> bool:
        """Refresh distance, active state, duration, and optical flags from V5.

        Returns:
            True when the link's active state changed.

        Raises:
            ValueError: If no satcom object is attached.
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set")

        active_before = self.is_active
        self.refresh_lifetime_from_satcom()
        self.is_active = not getattr(
            self.satcom_object, "disabled", False
        ) and not self.temporary_unavailable

        distance_km = satcom_distance_km(self.satcom_object)
        if distance_km is not None:
            self.update_distance_and_delay(distance_km * 1000.0)

        self.refresh_optical_from_satcom()
        return self.is_active != active_before

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

    def get_mac_addresses(self) -> tuple[str, str]:
        """
        A method that retrieves the MAC addresses of the interfaces on both ends of the link.

        Returns:
            A tuple containing the MAC addresses of the source and target interfaces, respectively.
        """

        if not self.peer_interfaces:
            raise ValueError("No peer interfaces available on this link")

        src_iface_name = f"{self.source.name}.{self.target.id}"
        dst_iface_name = f"{self.target.name}.{self.source.id}"

        src_iface = None
        dst_iface = None
        for iface in self.peer_interfaces:
            if iface.name == src_iface_name:
                src_iface = iface
            elif iface.name == dst_iface_name:
                dst_iface = iface

        if src_iface is None or dst_iface is None:
            raise ValueError(
                "Could not find matching peer interfaces for MAC address lookup"
            )

        src_mac_address = src_iface.get_mac_address()
        if src_mac_address is None:
            src_mac_address = self.source.execute_command(
                f"cat /sys/class/net/{src_iface.get_iname()}/address"
            )
            src_iface.set_mac_address(src_mac_address)

        dst_mac_address = dst_iface.get_mac_address()
        if dst_mac_address is None:
            dst_mac_address = self.target.execute_command(
                f"cat /sys/class/net/{dst_iface.get_iname()}/address"
            )
            dst_iface.set_mac_address(dst_mac_address)

        return src_mac_address, dst_mac_address
