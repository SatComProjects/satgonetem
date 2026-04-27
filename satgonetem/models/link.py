import logging
from functools import lru_cache
from typing import Optional

from satgonetem.utils.utils import distance_3d_km

from satgonetem.models.node import Node

from sat_com_model.models import Link as SatComLink

# Use a module logger to control verbosity more easily
logger = logging.getLogger(__name__)


class Link:

    def __init__(
        self,
        source: Node,
        target: Node,
        distance: float,
        type: str,
        direction: str = "",
        is_active: bool = True,
        capacities: list = list(),
    ):
        self.source: Node = source
        self.target: Node = target
        self.distance: float = distance
        self.type: str = type
        self.is_active: bool = is_active
        self.direction: str = direction
        # Compute capacities (directional + aggregate)
        self.peer1_capacity = 0
        self.peer2_capacity = 0

        self.capacities = capacities

        self.capacity: int = self.get_link_capacity()

        self.peer_interfaces = []

        self.delay = int((self.distance / 299_792_458) * 1000)  # in ms, speed of light

        # self.calculate_link_budget()

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

        if new_delay != self.delay:
            self.delay = new_delay
            self.distance = new_distance
            self.to_update = True
        return None

    def sync_status_from_satcom(self) -> None:
        """
        A method that syncs the status from the satcom object.
        """
        if self.satcom_object is None:
            raise ValueError("Satcom object is not set")

        self.is_active = not getattr(self.satcom_object, "is_active", True)
        return None

    def init_telecom_parameters(self) -> None:
        """
        A method that initializes the telecom parameters for the link.
        """
        if not self.type == "GroundStationLink":
            return
        # Let's start implementing telecom
        self.frequency_ghz = {
            "uplink": 14.25,  # Placeholder for uplink frequency
            "downlink": 19.0,  # Placeholder for downlink frequency
        }

        self.free_space_loss_db = {
            "uplink": calculate_free_space_loss_db(
                self.frequency_ghz["uplink"], self.distance / 1000
            ),  # Convert distance to km
            "downlink": calculate_free_space_loss_db(
                self.frequency_ghz["downlink"], self.distance / 1000
            ),  # Convert distance to km
        }

    def _compute_capacity_one_way(
        self,
        tx: Node,
        rx: Node,
        frequency_ghz: float,
        elevation_angle: float,
        gs_lat: float,
        gs_lon: float,
        gs_diameter: float,
        bandwidth_hz: float = 100e6,
        rx_tsys_k: float = 290.0,
        unavailability_percent: float = 0.1,
    ) -> int:
        """Compute one-way capacity (kbps) from tx->rx using a simplified link budget.

        Steps:
        1) Compute TX EIRP from antenna parameters at the given frequency.
        2) Compute RX G/T from receive antenna gain and an approximate Tsys.
        3) Compute path loss (free space) and other losses (atmosphere).
        4) Compute C/N0 (dB-Hz), then C/N over noise bandwidth.
        5) Convert to SNR and use Shannon capacity C = B * log2(1+SNR) as an upper bound.
        """
        # TX EIRP
        ant = getattr(tx, "antenna", None)
        if ant is None:
            return 0
        try:
            ant.calculate_gain_db(frequency_ghz)
        except (AttributeError, TypeError, ValueError):
            logger.debug("TX antenna gain calculation failed, using cached value")
        gdb = getattr(ant, "gain_db", 0.0)
        sspa = getattr(ant, "sspa_output_power_db", 0.0)
        losses = getattr(ant, "losses_db", 0.0)
        eirp_db = calculate_transmitter_eirp(sspa, gdb, losses)

        # RX G/T (approximate using antenna gain and 290K system temperature)
        if hasattr(rx, "antenna"):
            try:
                rx.antenna.calculate_gain_db(frequency_ghz)
            except (AttributeError, TypeError, ValueError):
                logger.debug("RX antenna gain calculation failed, using cached value")
        rx_gain_db = getattr(getattr(rx, "antenna", None), "gain_db", 0.0)
        g_over_t_db = rx_gain_db - linear_to_db(rx_tsys_k)

        # Free-space loss
        fsl_db = calculate_free_space_loss_db(frequency_ghz, self.distance / 1000.0)

        # Other propagation losses (atmospheric)
        att = calculate_atmospheric_attenuation_dB(
            lat_GS=gs_lat,
            lon_GS=gs_lon,
            frequency_ghz=frequency_ghz,
            elevation_angle=elevation_angle,
            unavailability=unavailability_percent,
            antenna_diameter=gs_diameter,
        )
        other_losses_db = att[0] if isinstance(att, (list, tuple)) else float(att)

        # C/N0 (dB-Hz)
        cn0_dbhz = calculate_carrier_to_noise_power_spectral_density_ratio(
            eirp_db=eirp_db,
            g_over_t_db=g_over_t_db,
            free_space_loss_db=fsl_db,
            other_losses_db=other_losses_db,
        )

        # Noise bandwidth (Hz). Use occupied signal bandwidth (configurable)
        # Convert C/N0 to C/N over bandwidth: C/N [dB] = C/N0 [dB-Hz] - 10*log10(B)
        cn_db = cn0_dbhz - linear_to_db(bandwidth_hz)
        # Linear SNR
        snr_lin = 10.0 ** (cn_db / 10.0)
        # Shannon capacity upper bound
        import math

        capacity_bps = bandwidth_hz * math.log2(1.0 + max(0.0, snr_lin))
        return int(capacity_bps / 1000.0)

    def _compute_ground_link_capacities(self) -> tuple[int, int]:
        """Compute (downlink_kbps, uplink_kbps) for Satellite<->GroundStation."""
        if self.type != "GroundStationLink":
            return (0, 0)
        self.init_telecom_parameters()
        # Identify GS and SAT nodes
        if getattr(self.source, "type", "") == "GroundStation":
            gs = self.source
            sat = self.target
        else:
            gs = self.target
            sat = self.source

        # Geometry (degrees)
        try:
            elevation_angle = 90 - get_elevation_angle(
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
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute elevation angle for %s->%s: %s",
                sat.name,
                gs.name,
                exc,
            )
            elevation_angle = 30.0  # fallback typical elevation

        gs_lat = float(gs.position.get("latitude", 0.0))
        gs_lon = float(gs.position.get("longitude", 0.0))
        gs_diam = getattr(getattr(gs, "antenna", None), "diameter", 2.0)

        # Parameters from instance (set by service from config) with fallbacks
        bw_dl = float(getattr(self, "bandwidth_hz_downlink", 100_000_000))
        bw_ul = float(getattr(self, "bandwidth_hz_uplink", 100_000_000))
        rx_tsys = float(getattr(self, "rx_tsys_k", 290.0))
        unav = float(getattr(self, "unavailability_percent", 0.1))

        # Downlink (SAT -> GS)
        dl_kbps = self._compute_capacity_one_way(
            tx=sat,
            rx=gs,
            frequency_ghz=self.frequency_ghz["downlink"],
            elevation_angle=elevation_angle,
            gs_lat=gs_lat,
            gs_lon=gs_lon,
            gs_diameter=gs_diam,
            bandwidth_hz=bw_dl,
            rx_tsys_k=rx_tsys,
            unavailability_percent=unav,
        )
        # Uplink (GS -> SAT)
        ul_kbps = self._compute_capacity_one_way(
            tx=gs,
            rx=sat,
            frequency_ghz=self.frequency_ghz["uplink"],
            elevation_angle=elevation_angle,
            gs_lat=gs_lat,
            gs_lon=gs_lon,
            gs_diameter=gs_diam,
            bandwidth_hz=bw_ul,
            rx_tsys_k=rx_tsys,
            unavailability_percent=unav,
        )
        return (dl_kbps, ul_kbps)

    def calculate_link_budget(self) -> None:
        """
        Calculate the link budget for GroundStation links and update capacity.

        Notes on fixes and optimizations:
        - Uses the transmitter EIRP from the link source (satellite) for downlink.
        - Computes/derives receiver G/T if missing on the target (ground station).
        - Corrects unit handling and symbol rate formula (Rs = BW / (1 + rolloff)).
        - Reduces logging overhead and improves readability.
        """

        if self.type != "GroundStationLink":
            return

        # Initialize link-layer telecom parameters (FSL, frequencies)
        self.init_telecom_parameters()

        # Validate required attributes
        if not hasattr(self.source, "antenna") or not hasattr(self.target, "antenna"):
            logger.debug(
                "Missing antenna attributes on source/target; skipping link budget."
            )
            return

        # Geometry: elevation angle (degrees). Keep historical behavior using 90 - elevation.
        try:
            elevation_angle = 90 - get_elevation_angle(
                sat_coordinates=(
                    self.source.position["latitude"],
                    self.source.position["longitude"],
                    self.source.position["altitude"],
                ),
                gnd_coordinates=(
                    self.target.position["latitude"],
                    self.target.position["longitude"],
                    self.target.position["altitude"],
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute elevation angle for %s->%s: %s",
                self.source.name,
                self.target.name,
                exc,
            )
            return

        # Atmospheric attenuations (dB). Use total attenuation (index 0) if a tuple/list is returned.
        try:
            attenuations = calculate_atmospheric_attenuation_dB(
                lat_GS=self.target.position["latitude"],
                lon_GS=self.target.position["longitude"],
                frequency_ghz=self.frequency_ghz["downlink"],
                elevation_angle=elevation_angle,
                unavailability=0.1,  # percentage
                antenna_diameter=self.target.antenna.diameter,
            )
            other_losses_db = (
                attenuations[0]
                if isinstance(attenuations, (list, tuple))
                else float(attenuations)
            )
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute atmospheric attenuation for %s->%s: %s",
                self.source.name,
                self.target.name,
                exc,
            )
            return

        # Transmit EIRP: use source (satellite) EIRP for downlink
        eirp_db = getattr(self.source.antenna, "eirp_db", None)
        if eirp_db is None:
            logger.debug(
                "Source EIRP not available on %s; skipping link budget.", self.source
            )
            return

        # Receiver G/T (dB/K): if not present on target, approximate using antenna gain and 290K system noise
        g_over_t_db: Optional[float] = getattr(self.target, "g_over_t_db", None)
        if g_over_t_db is None:
            try:
                rx_gain_db = self.target.antenna.calculate_gain_db(
                    self.frequency_ghz["downlink"]
                )
                # Approximate Tsys at 290K if not provided
                g_over_t_db = rx_gain_db - linear_to_db(290.0)
            except (AttributeError, TypeError, ValueError) as exc:
                logger.warning(
                    "Failed to derive G/T for %s: %s", self.target.name, exc
                )
                return

        # Free-space loss (dB) computed during init for downlink
        fsl_db = self.free_space_loss_db["downlink"]

        # C/N0 in dB-Hz
        try:
            cn0_dbhz = calculate_carrier_to_noise_power_spectral_density_ratio(
                eirp_db=eirp_db,
                g_over_t_db=g_over_t_db,
                free_space_loss_db=fsl_db,
                other_losses_db=other_losses_db,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute C/N0 for %s->%s: %s",
                self.source.name,
                self.target.name,
                exc,
            )
            return

        # Link bandwidth and roll-off
        rolloff = 0.25
        bandwidth_hz = 500e6  # 500 MHz placeholder; adjust as needed

        # C/N (dB) over the occupied bandwidth
        try:
            _ = calculate_carrier_to_noise_ratio(
                c_over_n0_dbhz=cn0_dbhz,
                bandwidth_hz=bandwidth_hz,
            )
        except (TypeError, ValueError):
            # Some callers may not need C/N explicitly; compute continues with Rs for ModCod selection
            pass

        # Symbol rate (Hz) for RRC shaping: Rs = BW / (1 + rolloff)
        symbol_rate_hz = bandwidth_hz / (1.0 + rolloff)

        # Convert to the metric expected by ModCod helper (historical behavior): C/N0 - 10log10(Rs)
        cn_or_esn0_db = cn0_dbhz - linear_to_db(symbol_rate_hz)

        best_modcod = ModCod.best_for_csat_n0_rs(cn_or_esn0_db)
        if best_modcod is None:
            logger.debug(
                "No suitable ModCod for metric %.2f dB on %s", cn_or_esn0_db, self
            )
            return

        # Capacity (bps)
        try:
            capacity_bps = calculate_link_capacity(
                bandwidth_hz=bandwidth_hz,
                rolloff_factor=rolloff,
                bits_per_symbol=best_modcod.spectral_efficiency,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Failed to compute capacity for %s->%s: %s",
                self.source.name,
                self.target.name,
                exc,
            )
            return

        # Save as kbps for consistency with other link types
        self.capacity = int(capacity_bps / 1000.0)
        logger.info(
            "Link budget %s->%s | elev=%.2f°, C/N0=%.2f dBHz, Rs=%.2f Msps, ModCod=%s, cap=%.2f Mbps",
            self.source.name,
            self.target.name,
            elevation_angle,
            cn0_dbhz,
            symbol_rate_hz / 1e6,
            best_modcod,
            capacity_bps / 1e6,
        )

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

    def get_link_capacity(self) -> int:
        """
        A method that returns the link capacity based on the type of link.
        """
        if self.type == "InterSatelliteLink":
            return self.capacities[0]
        elif self.type == "GroundStationLink":
            return self.capacities[0]
        elif self.type == "UserTerminalLink":
            return self.capacities[0]

        return self.capacities[0]

    def get_capacity(self) -> int:
        """Return the current link capacity in kbps.

        Returns:
            The capacity in kbps as set by the topology manager, or the
            initial value from get_link_capacity if never overridden.
        """
        return self.capacity
