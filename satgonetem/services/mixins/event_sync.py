"""V5 EventBus integration for link lifecycle changes.

The mixin translates satComTopology link events into the same add/update/remove
lists used by the polling path. It deliberately leaves continuous quantities
(distance, duration, optical flags) to a per-tick refresh from the V5 link
objects: subscribing to every ``ISL_UPDATED`` event would add O(E) pydantic
events per tick without removing the need to touch the mutable link objects.

Event handlers only enqueue; ``update_simulation`` drains and applies one
coalesced delta on the simulation thread. This keeps the Docker/gRPC/routing
side of the emulator single-writer and prevents async handler delivery from
mutating the topology re-entrantly.
"""

from __future__ import annotations

import logging
import math
from numbers import Real
from typing import Any, Optional

from sat_com_application.event_bus.bus import EventBus
from sat_com_application.event_bus.events import EventType
from sat_com_model.models import InterSatelliteLinkDirection

from satgonetem.models.ground_station import GroundStation
from satgonetem.models.link import Link, satcom_distance_km
from satgonetem.models.satellite import Satellite
from satgonetem.services.event_collection import (
    EventCollector,
    LinkDelta,
    LinkKey,
)

_CREATED_EVENT_TYPES = frozenset({EventType.ISL_CREATED, EventType.GSL_CREATED})
_SUBSCRIBED_EVENT_TYPES = (
    EventType.ISL_CREATED,
    EventType.ISL_REMOVED,
    EventType.GSL_CREATED,
    EventType.GSL_REMOVED,
    # UTLs are collected and ignored until the emulator models terminals as nodes.
    EventType.UTL_CREATED,
    EventType.UTL_REMOVED,
)

_DIRECTION_LABELS = {
    InterSatelliteLinkDirection.ORBITAL: "Orbital",
    InterSatelliteLinkDirection.ADJACENT: "Adjacent",
    InterSatelliteLinkDirection.INTER_SHELL: "InterShell",
    InterSatelliteLinkDirection.UNDEFINED: "Undefined",
}


class EventSyncMixin:
    """Event-driven link lifecycle support for ``TopologyManager``."""

    def enable_event_sync(self) -> None:
        """Attach an EventBus collector to the current simulation.

        Call this after the initial ``init()`` sync. Existing initial events
        are not replayed: the emulator already built its initial state from the
        simulation manager. The method is idempotent.
        """
        if self.simulation_manager is None:
            raise ValueError("Simulation manager is not set")
        if getattr(self, "_event_collector", None) is not None:
            return

        event_bus = getattr(self.simulation_manager, "event_bus", None)
        if event_bus is None:
            event_bus = EventBus()
            self.simulation_manager.event_bus = event_bus

        collector = EventCollector()
        for event_type in _SUBSCRIBED_EVENT_TYPES:
            event_bus.subscribe(event_type, collector.handle)

        self.event_bus = event_bus
        self._event_collector = collector
        self._ticks_since_reconcile = 0
        logging.info(
            "Event-driven link sync enabled (reconcile every %s tick(s))",
            getattr(self, "event_reconcile_interval", 0),
        )

    def _drain_link_events(self) -> LinkDelta:
        """Drain queued V5 events into a coalesced link delta."""
        delta = LinkDelta()
        collector = getattr(self, "_event_collector", None)
        if collector is None:
            return delta

        for event in collector.drain():
            key = self._link_event_key(event)
            if key is None:
                continue
            action = "created" if event.type in _CREATED_EVENT_TYPES else "removed"
            delta.record(key, action, event)
        return delta

    def _link_event_key(self, event: Any) -> Optional[LinkKey]:
        """Map an ISL/GSL event to the emulator's undirected link key."""
        event_type = getattr(event, "type", None)
        if event_type in {EventType.ISL_CREATED, EventType.ISL_REMOVED}:
            satellite_a_id = getattr(event, "satellite_a_id", None)
            satellite_b_id = getattr(event, "satellite_b_id", None)
            if satellite_a_id not in self.satellites:
                return None
            if satellite_b_id not in self.satellites:
                return None
            return frozenset((f"Sat{satellite_a_id}", f"Sat{satellite_b_id}"))

        if event_type in {EventType.GSL_CREATED, EventType.GSL_REMOVED}:
            satellite_id = getattr(event, "satellite_id", None)
            ground_station_id = getattr(event, "ground_station_id", None)
            if satellite_id not in self.satellites:
                return None
            if ground_station_id not in self.ground_stations:
                return None
            return frozenset(
                (f"Sat{satellite_id}", f"Gnd{ground_station_id}")
            )

        # UTLs and any future event types are collected but not modelled yet.
        return None

    def _apply_link_delta(self, delta: LinkDelta) -> dict[str, list[Link]]:
        """Apply created/updated/removed links and refresh existing links."""
        result = delta.resolve(set(self.links))

        for key, _event in result.removed.items():
            link = self.links.get(key)
            if link is not None:
                link.to_add = False
                link.to_update = False
                link.to_remove = True

        for key, event in result.created.items():
            if key in self.links:
                continue
            link = self._create_link_from_event(event)
            if link is None:
                continue
            self.links[key] = link
            if link.is_active:
                link.to_add = True

        for key, event in result.updated.items():
            link = self.links.get(key)
            if link is None:
                continue
            # A remove+recreate in one tick is an update, not a deletion.
            link.to_remove = False
            satcom_link = self._find_satcom_link_from_event(event)
            if satcom_link is not None:
                link.satcom_object = satcom_link
            self._refresh_link_from_satcom(link)

        # Continuous state: distance, active flag, duration and optical flags.
        # These are refreshed for every existing link, including links whose
        # V5 strategy kept them without publishing an update event.
        for link in list(self.links.values()):
            if link.to_remove:
                continue
            self._refresh_link_from_satcom(link)

        return self._collect_pending_link_changes()

    def _refresh_link_from_satcom(self, link: Link) -> None:
        """Refresh one link from its V5 object and update add/remove flags."""
        satcom_link = getattr(link, "satcom_object", None)
        if satcom_link is None:
            return

        active_before = link.is_active
        try:
            link.refresh_from_satcom()
        except Exception:
            logging.exception(
                "Failed to refresh link %s--%s from V5",
                link.source.name,
                link.target.name,
            )
            return

        if link.is_active != active_before:
            if link.is_active:
                link.to_add = True
                link.to_remove = False
            else:
                link.to_add = False
                link.to_remove = True

    def _collect_pending_link_changes(self) -> dict[str, list[Link]]:
        """Classify links by their pending flags after event/reconciliation."""
        links_to_add: list[Link] = []
        links_to_update: list[Link] = []
        links_to_remove: list[Link] = []
        for link in self.links.values():
            if link.to_remove:
                links_to_remove.append(link)
            elif link.to_add:
                links_to_add.append(link)
            elif link.to_update:
                links_to_update.append(link)
        return {
            "links_to_add": links_to_add,
            "links_to_update": links_to_update,
            "links_to_remove": links_to_remove,
        }

    def _should_reconcile(self) -> bool:
        """Return True when the periodic full reconciliation is due."""
        interval = int(getattr(self, "event_reconcile_interval", 0) or 0)
        if interval <= 0:
            return False
        self._ticks_since_reconcile = getattr(self, "_ticks_since_reconcile", 0) + 1
        if self._ticks_since_reconcile < interval:
            return False
        self._ticks_since_reconcile = 0
        return True

    def _create_link_from_event(self, event: Any) -> Optional[Link]:
        """Build an emulator Link from a V5 created event."""
        endpoints = self._event_endpoints(event)
        if endpoints is None:
            return None
        source, target, satcom_link = endpoints

        is_isl = event.type in {EventType.ISL_CREATED, EventType.ISL_REMOVED}
        link_type = "InterSatelliteLink" if is_isl else "GroundStationLink"
        direction = ""
        if is_isl:
            direction = self._event_direction(event, satcom_link)

        distance_km = self._event_distance_km(event, satcom_link)
        is_active = (
            not getattr(satcom_link, "disabled", False)
            if satcom_link is not None
            else True
        )
        capacity = (
            self.isl_link_capacity if is_isl else self.gnd_link_capacity
        )
        link = Link(
            source=source,
            target=target,
            distance=distance_km * 1000.0,
            type=link_type,
            direction=direction,
            is_active=is_active,
            default_capacity_kbps=capacity,
            use_budget=self.use_budget,
            link_budget_config=self.link_budget_config,
        )
        link.satcom_object = satcom_link
        if satcom_link is not None:
            link.refresh_from_satcom()
        else:
            self._apply_event_metadata(link, event)
        return link

    def _event_endpoints(
        self, event: Any
    ) -> Optional[tuple[Satellite | GroundStation, Satellite | GroundStation, Any]]:
        """Resolve the emulator nodes and V5 link for an ISL/GSL event."""
        if event.type in {EventType.ISL_CREATED, EventType.ISL_REMOVED}:
            source_id = getattr(event, "satellite_a_id", None)
            target_id = getattr(event, "satellite_b_id", None)
            source = self.satellites.get(source_id)
            target = self.satellites.get(target_id)
        elif event.type in {EventType.GSL_CREATED, EventType.GSL_REMOVED}:
            source_id = getattr(event, "satellite_id", None)
            target_id = getattr(event, "ground_station_id", None)
            source = self.satellites.get(source_id)
            target = self.ground_stations.get(target_id)
        else:
            return None

        if source is None or target is None:
            logging.debug("Ignoring link event with unknown endpoint: %r", event)
            return None

        satcom_link = self._find_satcom_link(source_id, target_id)
        return source, target, satcom_link

    def _find_satcom_link(self, source_id: int, target_id: int) -> Any:
        """Return the V5 link connecting two topology objects, if available."""
        if self.simulation_manager is None:
            return None
        try:
            source = self.simulation_manager.get_topology_object_from_uniq_id(
                source_id
            )
            target = self.simulation_manager.get_topology_object_from_uniq_id(
                target_id
            )
            link = self.simulation_manager.get_link_connecting_topology_objects(
                source, target
            )
            if link is None:
                link = self.simulation_manager.get_link_connecting_topology_objects(
                    target, source
                )
            return link
        except Exception:
            logging.debug(
                "Could not resolve V5 link between %s and %s",
                source_id,
                target_id,
                exc_info=True,
            )
            return None

    def _find_satcom_link_from_event(self, event: Any) -> Any:
        """Resolve the V5 link referenced by an event, if possible."""
        if event.type in {EventType.ISL_CREATED, EventType.ISL_REMOVED}:
            return self._find_satcom_link(
                getattr(event, "satellite_a_id", None),
                getattr(event, "satellite_b_id", None),
            )
        if event.type in {EventType.GSL_CREATED, EventType.GSL_REMOVED}:
            return self._find_satcom_link(
                getattr(event, "satellite_id", None),
                getattr(event, "ground_station_id", None),
            )
        return None

    @staticmethod
    def _event_distance_km(event: Any, satcom_link: Any) -> float:
        """Read the event distance, falling back to the V5 link and then zero."""
        value = getattr(event, "distance_km", None)
        if isinstance(value, Real) and not isinstance(value, bool):
            distance = float(value)
            if math.isfinite(distance) and distance >= 0:
                return distance
        distance = satcom_distance_km(satcom_link)
        return distance if distance is not None else 0.0

    @staticmethod
    def _event_direction(event: Any, satcom_link: Any) -> str:
        """Map the event/V5 direction enum or string to the emulator label."""
        raw = getattr(event, "direction", None)
        if raw is None and satcom_link is not None:
            raw = getattr(satcom_link, "inter_satellite_direction", None)
        if isinstance(raw, InterSatelliteLinkDirection):
            raw = raw.name
        if isinstance(raw, str):
            try:
                raw = InterSatelliteLinkDirection[raw.upper()]
            except KeyError:
                return "Undefined"
        return _DIRECTION_LABELS.get(raw, "Undefined")

    @staticmethod
    def _apply_event_metadata(link: Link, event: Any) -> None:
        """Apply event-only metadata when the V5 link object is unavailable."""
        link.estimated_duration_s = Link._optional_float(
            getattr(event, "estimated_duration", None), minimum=0.0
        )
        disconnection = getattr(event, "estimated_disconnection_time", None)
        if hasattr(disconnection, "year"):
            link.estimated_disconnection_time = disconnection
        elevation = getattr(event, "elevation_angle", None)
        if elevation is None:
            elevation = getattr(event, "elevation_above_horizon_angle", None)
        link.elevation_deg = Link._optional_float(elevation)
