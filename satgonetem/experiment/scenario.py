"""Declarative experiment scenarios.

A scenario is intentionally small: it describes a topology, a simulation
window, optional network overrides, traffic, and timed events.  It does not
replace the Python API; it is a reproducible wrapper around it.
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml

__all__ = [
    "EventSpec",
    "GroundStationSpec",
    "Scenario",
    "ScenarioError",
    "ShellSpec",
    "TrafficSpec",
]

_EVENT_ACTIONS = {"link_down", "link_up", "node_restart", "impairment"}
_TRAFFIC_TOOLS = {"ping", "iperf3"}


class ScenarioError(ValueError):
    """Invalid scenario file or field."""


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScenarioError(f"{label} must be a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ScenarioError(f"{label} has unknown field(s): {names}")


def _non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"{label} must be a non-empty string")
    return value


def _number(
    value: Any,
    label: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ScenarioError(f"{label} must be finite, got {value!r}")
    if number < minimum:
        raise ScenarioError(f"{label} must be >= {minimum}, got {value}")
    if maximum is not None and number > maximum:
        raise ScenarioError(f"{label} must be <= {maximum}, got {value}")
    return number


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ScenarioError(f"{label} must be a positive integer, got {value!r}")
    return value


def _boolean(value: Any, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ScenarioError(f"{label} must be a bool, got {value!r}")
    return value


def _scenario_name(value: Any) -> str:
    """Validate a scenario name that becomes part of the default run path."""
    name = _non_empty_str(value, "scenario.name")
    windows_path = PureWindowsPath(name)
    if (
        os.path.isabs(name)
        or PurePosixPath(name).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ScenarioError("scenario.name must not be an absolute path")
    if "/" in name or "\\" in name:
        raise ScenarioError("scenario.name must not contain path separators")
    if name in {".", ".."}:
        raise ScenarioError("scenario.name must not be a path segment")
    return name


@dataclass(frozen=True)
class TrafficSpec:
    """One traffic flow to launch during an experiment."""

    src: str
    dst: str
    tool: str = "ping"
    start_s: float = 0.0
    duration_s: float = 10.0
    count: int = 10
    protocol: str = "TCP"
    bandwidth_mbps: float = 0.0

    def __post_init__(self) -> None:
        _non_empty_str(self.src, "traffic.src")
        _non_empty_str(self.dst, "traffic.dst")
        if self.tool not in _TRAFFIC_TOOLS:
            raise ScenarioError("traffic.tool must be ping or iperf3")
        if self.protocol not in {"TCP", "UDP"}:
            raise ScenarioError("traffic.protocol must be 'TCP' or 'UDP'")
        _number(self.start_s, "traffic.start_s")
        _number(self.duration_s, "traffic.duration_s", minimum=0.001)
        _number(self.bandwidth_mbps, "traffic.bandwidth_mbps")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
        ):
            raise ScenarioError("traffic.count must be a positive integer")

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], label: str = "traffic"
    ) -> TrafficSpec:
        data = _require_mapping(data, label)
        _reject_unknown(
            data,
            {
                "src",
                "dst",
                "tool",
                "start_s",
                "duration_s",
                "count",
                "protocol",
                "bandwidth_mbps",
            },
            label,
        )
        tool = str(data.get("tool", "ping")).lower()
        if tool not in _TRAFFIC_TOOLS:
            raise ScenarioError(
                f"{label}.tool must be one of {sorted(_TRAFFIC_TOOLS)}, got {tool!r}"
            )
        protocol = str(data.get("protocol", "TCP")).upper()
        if protocol not in {"TCP", "UDP"}:
            raise ScenarioError(
                f"{label}.protocol must be 'TCP' or 'UDP', got {protocol!r}"
            )
        return cls(
            src=_non_empty_str(data.get("src"), f"{label}.src"),
            dst=_non_empty_str(data.get("dst"), f"{label}.dst"),
            tool=tool,
            start_s=_number(data.get("start_s", 0.0), f"{label}.start_s"),
            duration_s=_number(
                data.get("duration_s", 10.0), f"{label}.duration_s", minimum=0.001
            ),
            count=_positive_int(data.get("count", 10), f"{label}.count"),
            protocol=protocol,
            bandwidth_mbps=_number(
                data.get("bandwidth_mbps", 0.0), f"{label}.bandwidth_mbps"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "src": self.src,
            "dst": self.dst,
            "tool": self.tool,
            "start_s": self.start_s,
            "duration_s": self.duration_s,
            "count": self.count,
            "protocol": self.protocol,
            "bandwidth_mbps": self.bandwidth_mbps,
        }


@dataclass(frozen=True)
class EventSpec:
    """One timed event. Link events may carry a duration."""

    at_s: float
    action: str
    src: str | None = None
    dst: str | None = None
    duration_s: float = 0.0
    loss_percent: float | None = None
    jitter_ms: int | None = None

    def __post_init__(self) -> None:
        if self.action not in _EVENT_ACTIONS:
            raise ScenarioError(
                f"event.action must be one of {sorted(_EVENT_ACTIONS)}, "
                f"got {self.action!r}"
            )
        if self.action in {"link_down", "link_up", "impairment"}:
            _non_empty_str(self.src, "event.src")
            _non_empty_str(self.dst, "event.dst")
        _number(self.at_s, "event.at_s")
        _number(self.duration_s, "event.duration_s")
        if self.loss_percent is not None:
            _number(
                self.loss_percent,
                "event.loss_percent",
                maximum=100.0,
            )
        if self.jitter_ms is not None and (
            isinstance(self.jitter_ms, bool)
            or not isinstance(self.jitter_ms, int)
            or self.jitter_ms < 0
        ):
            raise ScenarioError("event.jitter_ms must be a non-negative integer")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], label: str = "event") -> EventSpec:
        data = _require_mapping(data, label)
        _reject_unknown(
            data,
            {
                "at_s",
                "action",
                "src",
                "dst",
                "duration_s",
                "loss_percent",
                "jitter_ms",
            },
            label,
        )
        action = str(data.get("action", "")).lower()
        if action not in _EVENT_ACTIONS:
            raise ScenarioError(
                f"{label}.action must be one of {sorted(_EVENT_ACTIONS)}, "
                f"got {action!r}"
            )
        src = data.get("src")
        dst = data.get("dst")
        if action in {"link_down", "link_up", "impairment"}:
            src = _non_empty_str(src, f"{label}.src")
            dst = _non_empty_str(dst, f"{label}.dst")
        elif src is not None:
            src = _non_empty_str(src, f"{label}.src")

        loss = data.get("loss_percent")
        jitter = data.get("jitter_ms")
        if loss is not None:
            loss = _number(
                loss,
                f"{label}.loss_percent",
                maximum=100.0,
            )
        if jitter is not None and (
            isinstance(jitter, bool) or not isinstance(jitter, int) or jitter < 0
        ):
            raise ScenarioError(f"{label}.jitter_ms must be a non-negative integer")
        if action == "impairment" and loss is None and jitter is None:
            raise ScenarioError(
                f"{label} impairment requires loss_percent or jitter_ms"
            )
        return cls(
            at_s=_number(data.get("at_s", 0.0), f"{label}.at_s"),
            action=action,
            src=src,
            dst=dst,
            duration_s=_number(data.get("duration_s", 0.0), f"{label}.duration_s"),
            loss_percent=loss,
            jitter_ms=jitter,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "at_s": self.at_s,
            "action": self.action,
            "src": self.src,
            "dst": self.dst,
            "duration_s": self.duration_s,
            "loss_percent": self.loss_percent,
            "jitter_ms": self.jitter_ms,
        }


@dataclass(frozen=True)
class ShellSpec:
    """A Walker shell included in a scenario-generated topology."""

    identifier: str
    planes: int = 6
    satellites_per_plane: int = 11
    inclination_deg: float = 86.4
    revs_per_day: float = 14.35
    pattern: str = "star"
    phase_difference: bool = True

    def __post_init__(self) -> None:
        _non_empty_str(self.identifier, "shell.identifier")
        if self.planes < 1 or self.satellites_per_plane < 1:
            raise ScenarioError("shell planes and satellites_per_plane must be >= 1")
        _number(
            self.inclination_deg,
            "shell.inclination_deg",
            maximum=180.0,
        )
        _number(self.revs_per_day, "shell.revs_per_day", minimum=0.001)
        if self.pattern not in {"star", "delta"}:
            raise ScenarioError("shell pattern must be star or delta")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], label: str = "shell") -> ShellSpec:
        data = _require_mapping(data, label)
        _reject_unknown(
            data,
            {
                "identifier",
                "planes",
                "satellites_per_plane",
                "inclination_deg",
                "revs_per_day",
                "pattern",
                "phase_difference",
            },
            label,
        )
        pattern = str(data.get("pattern", "star")).lower()
        if pattern not in {"star", "delta"}:
            raise ScenarioError(
                f"{label}.pattern must be 'star' or 'delta', got {pattern!r}"
            )
        return cls(
            identifier=_non_empty_str(data.get("identifier"), f"{label}.identifier"),
            planes=_positive_int(data.get("planes", 6), f"{label}.planes"),
            satellites_per_plane=_positive_int(
                data.get("satellites_per_plane", 11),
                f"{label}.satellites_per_plane",
            ),
            inclination_deg=_number(
                data.get("inclination_deg", 86.4), f"{label}.inclination_deg"
            ),
            revs_per_day=_number(
                data.get("revs_per_day", 14.35),
                f"{label}.revs_per_day",
                minimum=0.001,
            ),
            pattern=pattern,
            phase_difference=_boolean(
                data.get("phase_difference"),
                f"{label}.phase_difference",
                default=True,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "planes": self.planes,
            "satellites_per_plane": self.satellites_per_plane,
            "inclination_deg": self.inclination_deg,
            "revs_per_day": self.revs_per_day,
            "pattern": self.pattern,
            "phase_difference": self.phase_difference,
        }


@dataclass(frozen=True)
class GroundStationSpec:
    """A fixed ground station included in a scenario-generated topology."""

    name: str
    latitude: float
    longitude: float
    elevation_km: float = 0.0

    def __post_init__(self) -> None:
        _non_empty_str(self.name, "ground_station.name")
        if any(separator in self.name for separator in (",", "\r", "\n")):
            raise ScenarioError(
                "ground_station.name must not contain commas or line breaks"
            )
        _number(
            self.latitude,
            "ground_station.latitude",
            minimum=-90.0,
            maximum=90.0,
        )
        _number(
            self.longitude,
            "ground_station.longitude",
            minimum=-180.0,
            maximum=180.0,
        )
        _number(self.elevation_km, "ground_station.elevation_km")

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], label: str = "ground_station"
    ) -> GroundStationSpec:
        data = _require_mapping(data, label)
        _reject_unknown(
            data,
            {"name", "latitude", "longitude", "elevation_km"},
            label,
        )
        latitude = _number(data.get("latitude"), f"{label}.latitude")
        longitude = _number(data.get("longitude"), f"{label}.longitude")
        if not -90.0 <= latitude <= 90.0:
            raise ScenarioError(f"{label}.latitude must be in [-90, 90]")
        if not -180.0 <= longitude <= 180.0:
            raise ScenarioError(f"{label}.longitude must be in [-180, 180]")
        return cls(
            name=_non_empty_str(data.get("name"), f"{label}.name"),
            latitude=latitude,
            longitude=longitude,
            elevation_km=_number(
                data.get("elevation_km", 0.0), f"{label}.elevation_km"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "elevation_km": self.elevation_km,
        }


@dataclass(frozen=True)
class Scenario:
    """A validated experiment manifest."""

    name: str
    topology: str | None
    duration_s: float
    tick_s: int = 1
    routing: str | None = None
    network: Mapping[str, Any] = field(default_factory=dict)
    shells: tuple[ShellSpec, ...] = ()
    ground_stations: tuple[GroundStationSpec, ...] = ()
    traffic: tuple[TrafficSpec, ...] = ()
    events: tuple[EventSpec, ...] = ()
    metrics_interval_s: float = 1.0
    connectivity: bool = False
    output_dir: str | None = None
    base_dir: str = "."

    def __post_init__(self) -> None:
        _scenario_name(self.name)
        if self.topology is not None and (self.shells or self.ground_stations):
            raise ScenarioError(
                "Use either topology or shells/ground_stations, not both"
            )
        if self.topology is None and not self.shells:
            raise ScenarioError("Scenario requires topology or at least one shell")
        if self.ground_stations and not self.shells:
            raise ScenarioError("ground_stations require at least one shell")
        _number(self.duration_s, "duration_s", minimum=0.001)
        if (
            isinstance(self.tick_s, bool)
            or not isinstance(self.tick_s, int)
            or self.tick_s < 1
        ):
            raise ScenarioError("tick_s must be a positive integer")
        if self.traffic and self.tick_s != 1:
            raise ScenarioError(
                "tick_s must be 1 when traffic is present; "
                "flow durations are wall-clock seconds in this pass"
            )
        _number(
            self.metrics_interval_s,
            "metrics_interval_s",
            minimum=0.001,
        )
        shell_ids = [shell.identifier for shell in self.shells]
        if len(shell_ids) != len(set(shell_ids)):
            raise ScenarioError("shell identifiers must be unique")
        station_names = [station.name for station in self.ground_stations]
        if len(station_names) != len(set(station_names)):
            raise ScenarioError("ground-station names must be unique")

    @classmethod
    def from_file(cls, path: str | Path) -> Scenario:
        """Load and validate a YAML scenario file."""
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"Scenario file does not exist: {source}")
        try:
            data = yaml.safe_load(source.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ScenarioError(f"Invalid YAML in {source}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ScenarioError("Scenario root must be a mapping")

        _reject_unknown(
            data,
            {
                "name",
                "topology",
                "duration_s",
                "tick_s",
                "routing",
                "network",
                "shells",
                "ground_stations",
                "traffic",
                "events",
                "metrics_interval_s",
                "connectivity",
                "output_dir",
                "run_dir",
                "started_at",
            },
            "scenario",
        )
        run_dir_value = data.get("run_dir")
        run_base: Path | None = None
        if run_dir_value is not None:
            candidate = Path(str(run_dir_value))
            if not candidate.is_absolute():
                candidate = (source.resolve().parent / candidate).resolve()
            run_base = candidate
        network = _require_mapping(data.get("network", {}), "scenario.network")
        if network:
            try:
                from satgonetem.services.topology_satcom import NetworkConfig

                NetworkConfig.from_mapping(dict(network))
            except (TypeError, ValueError) as exc:
                raise ScenarioError(f"scenario.network is invalid: {exc}") from exc
        traffic_data = data.get("traffic", [])
        events_data = data.get("events", [])
        shells_data = data.get("shells", [])
        ground_data = data.get("ground_stations", [])
        for label, value in (
            ("scenario.traffic", traffic_data),
            ("scenario.events", events_data),
            ("scenario.shells", shells_data),
            ("scenario.ground_stations", ground_data),
        ):
            if not isinstance(value, list):
                raise ScenarioError(f"{label} must be a list")

        tick_s = _positive_int(data.get("tick_s", 1), "scenario.tick_s")
        traffic = tuple(
            TrafficSpec.from_mapping(item, f"scenario.traffic[{index}]")
            for index, item in enumerate(traffic_data)
        )
        if traffic and tick_s != 1:
            raise ScenarioError(
                "scenario.tick_s must be 1 when traffic is present; "
                "flow durations are wall-clock seconds in this pass"
            )
        shells = tuple(
            ShellSpec.from_mapping(item, f"scenario.shells[{index}]")
            for index, item in enumerate(shells_data)
        )
        ground_stations = tuple(
            GroundStationSpec.from_mapping(item, f"scenario.ground_stations[{index}]")
            for index, item in enumerate(ground_data)
        )
        topology = (
            _non_empty_str(data["topology"], "scenario.topology")
            if data.get("topology") is not None
            else None
        )
        if topology is not None and not Path(topology).is_absolute():
            candidates = []
            if run_base is not None:
                candidates.append(run_base / topology)
            candidates.append(source.resolve().parent / topology)
            for candidate in candidates:
                if candidate.is_file():
                    topology = str(candidate.resolve())
                    break

        return cls(
            name=_non_empty_str(data.get("name"), "scenario.name"),
            topology=topology,
            duration_s=_number(
                data.get("duration_s"), "scenario.duration_s", minimum=0.001
            ),
            tick_s=tick_s,
            routing=(
                _non_empty_str(data["routing"], "scenario.routing")
                if data.get("routing") is not None
                else None
            ),
            network=dict(network),
            shells=shells,
            ground_stations=ground_stations,
            traffic=traffic,
            events=tuple(
                EventSpec.from_mapping(item, f"scenario.events[{index}]")
                for index, item in enumerate(events_data)
            ),
            metrics_interval_s=_number(
                data.get("metrics_interval_s", 1.0),
                "scenario.metrics_interval_s",
                minimum=0.001,
            ),
            connectivity=_boolean(
                data.get("connectivity"), "scenario.connectivity", default=False
            ),
            output_dir=(
                _non_empty_str(data["output_dir"], "scenario.output_dir")
                if data.get("output_dir") is not None
                else None
            ),
            base_dir=str(source.resolve().parent),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a YAML/JSON-serializable scenario description."""
        return {
            "name": self.name,
            "topology": self.topology,
            "duration_s": self.duration_s,
            "tick_s": self.tick_s,
            "routing": self.routing,
            "network": dict(self.network),
            "shells": [item.to_dict() for item in self.shells],
            "ground_stations": [item.to_dict() for item in self.ground_stations],
            "traffic": [item.to_dict() for item in self.traffic],
            "events": [item.to_dict() for item in self.events],
            "metrics_interval_s": self.metrics_interval_s,
            "connectivity": self.connectivity,
            "output_dir": self.output_dir,
        }

    def write_manifest(self, path: str | Path) -> Path:
        """Write the scenario as YAML and return the path.

        The content is written to a temporary file in the target directory and
        moved into place with :func:`os.replace`, so a crash or serialization
        error cannot truncate an existing manifest.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = yaml.safe_dump(self.to_dict(), sort_keys=False)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        return target
