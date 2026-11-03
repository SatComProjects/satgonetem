"""Thin emulator metadata on top of satComTopology V5 objects.

The emulator does not replace V5's models. It adds an optional, pydantic
metadata mixin carrying the stable emulator identity (`emulator_id`), the
container name used by the launcher, and the address-plan block. Factories
build these subclasses; the topology manager fills the metadata after the
initial sync and the address plan.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from sat_com_application.event_bus.events import (
    GSLCreatedEvent,
    GSLRemovedEvent,
    GSLUpdatedEvent,
    ISLCreatedEvent,
    ISLRemovedEvent,
    ISLUpdatedEvent,
    UTLCreatedEvent,
    UTLRemovedEvent,
    UTLUpdatedEvent,
)
from sat_com_application.event_bus.factories import (
    DefaultGSLEventFactory,
    DefaultISLEventFactory,
    DefaultUTLEventFactory,
    EventFactories,
)
from sat_com_builder.factories import (
    DefaultGroundStationFactory,
    DefaultPointOfPresenceFactory,
    DefaultSatelliteFactory,
    DefaultUserTerminalFactory,
    TopologyObjectFactories,
)
from sat_com_model.models import (
    GroundStation,
    PointOfPresence,
    Satellite,
    UserTerminal,
)


class EmulatorMetadataMixin(BaseModel):
    """Optional identity/address metadata carried by V5 topology objects."""

    emulator_id: int | None = None
    container_name: str | None = None
    address_block: str | None = None


class EmulatorSatellite(EmulatorMetadataMixin, Satellite):
    """V5 satellite plus emulator identity metadata."""


class EmulatorGroundStation(EmulatorMetadataMixin, GroundStation):
    """V5 ground station plus emulator identity metadata."""


class EmulatorUserTerminal(EmulatorMetadataMixin, UserTerminal):
    """V5 user terminal plus emulator identity metadata."""


class EmulatorPointOfPresence(EmulatorMetadataMixin, PointOfPresence):
    """V5 point of presence plus emulator identity metadata."""


class EmulatorSatelliteFactory(DefaultSatelliteFactory):
    """Build :class:`EmulatorSatellite` instances."""

    def create_satellite(
        self,
        satellite_name: str,
        object_id: int,
        shell: Any,
        movement_model: Any,
        domain: str = "public",
    ) -> EmulatorSatellite:
        return EmulatorSatellite(
            satellite_name=satellite_name,
            object_id=object_id,
            domain=domain,
            shell=shell,
            movement_model=movement_model,
        )


class EmulatorGroundStationFactory(DefaultGroundStationFactory):
    """Build :class:`EmulatorGroundStation` instances."""

    def create_ground_station(
        self,
        object_id: int,
        label: str = "unknown",
        longitude: float = 0.0,
        latitude: float = 0.0,
        altitude: float = 0.0,
        ground_object_domain: Any = None,
    ) -> EmulatorGroundStation:
        base = super().create_ground_station(
            object_id,
            label,
            longitude,
            latitude,
            altitude,
            ground_object_domain,
        )
        return EmulatorGroundStation(
            object_id=base.object_id,
            domain=base.domain,
            label=base.label,
            spatial_position_info=base.spatial_position_info,
            ground_object_domain=base.ground_object_domain,
        )


class EmulatorUserTerminalFactory(DefaultUserTerminalFactory):
    """Build :class:`EmulatorUserTerminal` instances."""

    def create_user_terminal(
        self,
        object_id: int,
        label: str = "unknown",
        longitude: float = 0.0,
        latitude: float = 0.0,
        altitude: float = 0.0,
        ground_object_domain: Any = None,
    ) -> EmulatorUserTerminal:
        base = super().create_user_terminal(
            object_id,
            label,
            longitude,
            latitude,
            altitude,
            ground_object_domain,
        )
        return EmulatorUserTerminal(
            object_id=base.object_id,
            domain=base.domain,
            label=base.label,
            spatial_position_info=base.spatial_position_info,
            ground_object_domain=base.ground_object_domain,
        )


class EmulatorPointOfPresenceFactory(DefaultPointOfPresenceFactory):
    """Build :class:`EmulatorPointOfPresence` instances."""

    def create_point_of_presence(
        self,
        object_id: int,
        label: str = "unknown",
        longitude: float = 0.0,
        latitude: float = 0.0,
        altitude: float = 0.0,
        ground_object_domain: Any = None,
    ) -> EmulatorPointOfPresence:
        base = super().create_point_of_presence(
            object_id,
            label,
            longitude,
            latitude,
            altitude,
            ground_object_domain,
        )
        return EmulatorPointOfPresence(
            object_id=base.object_id,
            domain=base.domain,
            label=base.label,
            spatial_position_info=base.spatial_position_info,
            ground_object_domain=base.ground_object_domain,
        )


def emulator_topology_object_factories() -> TopologyObjectFactories:
    """Return the factory set that builds emulator-aware V5 objects."""
    return TopologyObjectFactories(
        satellite_factory=EmulatorSatelliteFactory(),
        ground_station_factory=EmulatorGroundStationFactory(),
        user_terminal_factory=EmulatorUserTerminalFactory(),
        point_of_presence_factory=EmulatorPointOfPresenceFactory(),
    )


class MetadataISLCreatedEvent(ISLCreatedEvent):
    satellite_a_container: str | None = None
    satellite_b_container: str | None = None


class MetadataISLUpdatedEvent(ISLUpdatedEvent):
    satellite_a_container: str | None = None
    satellite_b_container: str | None = None


class MetadataISLRemovedEvent(ISLRemovedEvent):
    satellite_a_container: str | None = None
    satellite_b_container: str | None = None


class MetadataGSLCreatedEvent(GSLCreatedEvent):
    satellite_container: str | None = None
    ground_station_container: str | None = None


class MetadataGSLUpdatedEvent(GSLUpdatedEvent):
    satellite_container: str | None = None
    ground_station_container: str | None = None


class MetadataGSLRemovedEvent(GSLRemovedEvent):
    satellite_container: str | None = None
    ground_station_container: str | None = None


class MetadataUTLCreatedEvent(UTLCreatedEvent):
    satellite_container: str | None = None
    user_terminal_container: str | None = None


class MetadataUTLUpdatedEvent(UTLUpdatedEvent):
    satellite_container: str | None = None
    user_terminal_container: str | None = None


class MetadataUTLRemovedEvent(UTLRemovedEvent):
    satellite_container: str | None = None
    user_terminal_container: str | None = None


class MetadataISLEventFactory(DefaultISLEventFactory):
    """Add container names to ISL events."""

    def created(self, link):
        base = super().created(link)
        return MetadataISLCreatedEvent(
            **base.model_dump(),
            satellite_a_container=getattr(link.source, "container_name", None),
            satellite_b_container=getattr(link.destination, "container_name", None),
        )

    def updated(self, link):
        base = super().updated(link)
        return MetadataISLUpdatedEvent(
            **base.model_dump(),
            satellite_a_container=getattr(link.source, "container_name", None),
            satellite_b_container=getattr(link.destination, "container_name", None),
        )

    def removed(self, link):
        base = super().removed(link)
        return MetadataISLRemovedEvent(
            **base.model_dump(),
            satellite_a_container=getattr(link.source, "container_name", None),
            satellite_b_container=getattr(link.destination, "container_name", None),
        )


def _ground_link_endpoints(link):
    """Return (satellite, ground_object) regardless of link orientation."""
    if getattr(link.source, "type", "") == "Satellite":
        return link.source, link.destination
    return link.destination, link.source


class MetadataGSLEventFactory(DefaultGSLEventFactory):
    """Add container names to GSL events."""

    def _metadata(self, link):
        satellite, ground_station = _ground_link_endpoints(link)
        return {
            "satellite_container": getattr(satellite, "container_name", None),
            "ground_station_container": getattr(
                ground_station, "container_name", None
            ),
        }

    def created(self, link):
        base = super().created(link)
        return MetadataGSLCreatedEvent(
            **base.model_dump(), **self._metadata(link)
        )

    def updated(self, link):
        base = super().updated(link)
        return MetadataGSLUpdatedEvent(
            **base.model_dump(), **self._metadata(link)
        )

    def removed(self, link):
        base = super().removed(link)
        return MetadataGSLRemovedEvent(
            **base.model_dump(), **self._metadata(link)
        )


class MetadataUTLEventFactory(DefaultUTLEventFactory):
    """Add container names to UTL events."""

    def _metadata(self, link):
        satellite, user_terminal = _ground_link_endpoints(link)
        return {
            "satellite_container": getattr(satellite, "container_name", None),
            "user_terminal_container": getattr(
                user_terminal, "container_name", None
            ),
        }

    def created(self, link):
        base = super().created(link)
        return MetadataUTLCreatedEvent(
            **base.model_dump(), **self._metadata(link)
        )

    def updated(self, link):
        base = super().updated(link)
        return MetadataUTLUpdatedEvent(
            **base.model_dump(), **self._metadata(link)
        )

    def removed(self, link):
        base = super().removed(link)
        return MetadataUTLRemovedEvent(
            **base.model_dump(), **self._metadata(link)
        )


def emulator_event_factories() -> EventFactories:
    """Return event factories that attach emulator container metadata."""
    return EventFactories(
        isl_event_factory=MetadataISLEventFactory(),
        gsl_event_factory=MetadataGSLEventFactory(),
        utl_event_factory=MetadataUTLEventFactory(),
    )
