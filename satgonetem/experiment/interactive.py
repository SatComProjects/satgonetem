"""Interactive scenario builder used by ``satgonetem create`` / ``edit``."""

from __future__ import annotations

import math
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from satgonetem.experiment.scenario import (
    EventSpec,
    GroundStationSpec,
    Scenario,
    ScenarioError,
    ShellSpec,
    TrafficSpec,
)

__all__ = ["ScenarioDraft", "ScenarioShell", "default_scenario"]


def default_scenario(name: str) -> Scenario:
    """Return the initial scenario used by ``satgonetem create``."""
    return Scenario(
        name=name,
        topology=None,
        duration_s=600.0,
        routing="static",
        shells=(ShellSpec(identifier="LEO"),),
    )


@dataclass
class ScenarioDraft:
    """Mutable working copy of a scenario."""

    name: str
    topology: str | None = None
    duration_s: float = 600.0
    tick_s: int = 1
    routing: str | None = "static"
    network: dict[str, Any] = field(default_factory=dict)
    shells: list[ShellSpec] = field(default_factory=list)
    ground_stations: list[GroundStationSpec] = field(default_factory=list)
    traffic: list[TrafficSpec] = field(default_factory=list)
    events: list[EventSpec] = field(default_factory=list)
    metrics_interval_s: float = 1.0
    connectivity: bool = False
    output_dir: str | None = None
    base_dir: str = "."

    @classmethod
    def from_scenario(cls, scenario: Scenario) -> ScenarioDraft:
        return cls(
            name=scenario.name,
            topology=scenario.topology,
            duration_s=scenario.duration_s,
            tick_s=scenario.tick_s,
            routing=scenario.routing,
            network=dict(scenario.network),
            shells=list(scenario.shells),
            ground_stations=list(scenario.ground_stations),
            traffic=list(scenario.traffic),
            events=list(scenario.events),
            metrics_interval_s=scenario.metrics_interval_s,
            connectivity=scenario.connectivity,
            output_dir=scenario.output_dir,
            base_dir=scenario.base_dir,
        )

    def to_scenario(self) -> Scenario:
        return Scenario(
            name=self.name,
            topology=self.topology,
            duration_s=self.duration_s,
            tick_s=self.tick_s,
            routing=self.routing,
            network=dict(self.network),
            shells=tuple(self.shells),
            ground_stations=tuple(self.ground_stations),
            traffic=tuple(self.traffic),
            events=tuple(self.events),
            metrics_interval_s=self.metrics_interval_s,
            connectivity=self.connectivity,
            output_dir=self.output_dir,
            base_dir=self.base_dir,
        )


def _split_kv(tokens: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise ScenarioError(f"expected key=value, got {token!r}")
        key, value = token.split("=", 1)
        if not key or not value:
            raise ScenarioError(f"expected key=value, got {token!r}")
        values[key] = value
    return values


def _first(values: dict[str, str], *names: str) -> str | None:
    for name in names:
        if name in values:
            return values.pop(name)
    return None


class ScenarioShell:
    """Line-oriented interactive editor for one scenario file."""

    def __init__(
        self,
        draft: ScenarioDraft,
        path: str | Path | None = None,
        *,
        input_func: Callable[[str], str] | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        self.draft = draft
        self.path = Path(path) if path is not None else None
        self._input = input_func or input
        self._output = output
        self._done = False

    # ------------------------------------------------------------------
    # Command loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._output(
            "SatGoNetem scenario shell. Type 'help' for commands, 'save' to "
            "write the scenario, 'quit' to exit."
        )
        while not self._done:
            try:
                line = self._input("satgonetem> ")
            except EOFError:
                self._output("")
                break
            line = line.strip()
            if not line:
                continue
            try:
                self._dispatch(shlex.split(line))
            except (ScenarioError, ValueError, TypeError, KeyError, OSError) as exc:
                self._output(f"error: {exc}")
        if self.path is not None:
            self._output(f"session ended; scenario path: {self.path}")

    def _dispatch(self, parts: list[str]) -> None:
        command = parts[0].lower()
        args = parts[1:]
        if command in {"quit", "exit"}:
            self._done = True
        elif command == "help":
            self._show_help()
        elif command == "show":
            self._show()
        elif command == "save":
            self._save(args)
        elif command == "load":
            self._load(args)
        elif command == "name":
            self._require(args, "name <scenario-name>")
            self.draft.name = args[0]
        elif command == "topology":
            self._set_topology(args)
        elif command == "duration":
            self._require(args, "duration <seconds>")
            self.draft.duration_s = _as_float(args[0], "duration")
        elif command == "tick":
            self._require(args, "tick <seconds>")
            value = _as_int(args[0], "tick")
            if value < 1:
                raise ScenarioError("tick must be >= 1")
            self.draft.tick_s = value
        elif command == "routing":
            self._require(args, "routing <method|none>")
            self.draft.routing = None if args[0].lower() == "none" else args[0]
        elif command == "metrics-interval":
            self._require(args, "metrics-interval <seconds>")
            self.draft.metrics_interval_s = _as_float(args[0], "metrics-interval")
        elif command == "connectivity":
            self._require(args, "connectivity on|off")
            self.draft.connectivity = _as_bool(args[0], "connectivity")
        elif command == "output":
            self._require(args, "output <directory|none>")
            self.draft.output_dir = None if args[0].lower() == "none" else args[0]
        elif command == "network":
            self._network(args)
        elif command == "shell":
            self._shell(args)
        elif command == "gnd":
            self._gnd(args)
        elif command == "traffic":
            self._traffic(args)
        elif command == "event":
            self._event(args)
        else:
            raise ScenarioError(f"unknown command {command!r}; type 'help'")

    @staticmethod
    def _require(args: list[str], usage: str) -> None:
        if not args:
            raise ScenarioError(f"usage: {usage}")

    def _show_help(self) -> None:
        self._output(
            "\n".join(
                [
                    "Commands:",
                    "  help                         show this help",
                    "  show                         print the current scenario YAML",
                    "  name <name>                  set the scenario name",
                    "  topology <path|none>         use a topology file instead of shells",
                    "  duration <seconds>           simulation duration",
                    "  tick <seconds>               simulation tick (1 with traffic)",
                    "  routing <method|none>        routing method (e.g. static)",
                    "  metrics-interval <seconds>   metric sampling interval",
                    "  connectivity on|off          run an all-pairs matrix at the end",
                    "  output <dir|none>            default run directory",
                    "  network set <key> <value>    set a NetworkConfig override",
                    "  network unset <key>          remove an override",
                    "  network list                 show overrides",
                    "  shell add <id> <planes> <sats> <incl> <revs> [star|delta]",
                    "  shell list | shell remove <id>",
                    "  gnd add <name> <lat> <lon> [elev_km]",
                    "  gnd list | gnd remove <name>",
                    "  traffic add <src> <dst> <ping|iperf3> [start=] [duration=] [count=]",
                    "  traffic list | traffic remove <index>",
                    "  event add action=<...> at=<...> [src=] [dst=] ...",
                    "  event list | event remove <index>",
                    "  save [path]                  write the scenario YAML",
                    "  load <path>                  replace the working scenario",
                    "  quit                         leave the shell",
                    "",
                    "Ground stations become Gnd<N>, Gnd<N+1>, ... after the generated satellites.",
                ]
            )
        )

    def _show(self) -> None:
        import yaml

        self._output(
            yaml.safe_dump(self.draft.to_scenario().to_dict(), sort_keys=False)
        )

    def _save(self, args: list[str]) -> None:
        target = Path(args[0]) if args else self.path
        if target is None:
            raise ScenarioError("usage: save <path> (no session path is set)")
        scenario = self.draft.to_scenario()
        if scenario.network:
            from satgonetem.services.topology_satcom import NetworkConfig

            NetworkConfig.from_mapping(dict(scenario.network))
        scenario.write_manifest(target)
        self.path = target
        self.draft.base_dir = str(target.resolve().parent)
        self._output(f"Saved scenario to {target}")

    def _load(self, args: list[str]) -> None:
        self._require(args, "load <path>")
        scenario = Scenario.from_file(args[0])
        self.draft = ScenarioDraft.from_scenario(scenario)
        self.path = Path(args[0])
        self._output(f"Loaded scenario from {self.path}")

    def _set_topology(self, args: list[str]) -> None:
        self._require(args, "topology <path|none>")
        if args[0].lower() == "none":
            self.draft.topology = None
            return
        if self.draft.shells or self.draft.ground_stations:
            raise ScenarioError(
                "topology and shells/ground_stations cannot be combined; "
                "remove shells and ground stations first"
            )
        self.draft.topology = args[0]

    def _network(self, args: list[str]) -> None:
        self._require(args, "network set|unset|list")
        action = args[0].lower()
        if action == "list":
            self._output(str(self.draft.network))
            return
        if action == "set":
            if len(args) < 3:
                raise ScenarioError("usage: network set <key> <value>")
            self.draft.network[args[1]] = _as_scalar(args[2])
            return
        if action == "unset":
            if len(args) < 2:
                raise ScenarioError("usage: network unset <key>")
            self.draft.network.pop(args[1], None)
            return
        raise ScenarioError("usage: network set|unset|list")

    def _shell(self, args: list[str]) -> None:
        self._require(args, "shell add|list|remove")
        action = args[0].lower()
        if action == "list":
            if not self.draft.shells:
                self._output("no shells")
            for shell in self.draft.shells:
                self._output(
                    f"{shell.identifier}: {shell.planes}x{shell.satellites_per_plane} "
                    f"incl={shell.inclination_deg:g} revs/day={shell.revs_per_day:g} "
                    f"pattern={shell.pattern}"
                )
            return
        if action == "remove":
            self._require(args[1:], "shell remove <id>")
            identifier = args[1]
            if not any(shell.identifier == identifier for shell in self.draft.shells):
                raise ScenarioError(f"unknown shell {identifier!r}")
            self.draft.shells = [
                shell for shell in self.draft.shells if shell.identifier != identifier
            ]
            return
        if action != "add":
            raise ScenarioError("usage: shell add|list|remove")
        if self.draft.topology is not None:
            raise ScenarioError(
                "cannot add shells while a topology is selected; "
                "run 'topology none' first"
            )
        if len(args) < 6:
            raise ScenarioError(
                "usage: shell add <id> <planes> <sats> <incl> <revs> [star|delta]"
            )
        identifier = args[1]
        if any(shell.identifier == identifier for shell in self.draft.shells):
            raise ScenarioError(f"shell {identifier!r} already exists")
        pattern = args[6].lower() if len(args) > 6 else "star"
        if pattern not in {"star", "delta"}:
            raise ScenarioError("shell pattern must be star or delta")
        self.draft.shells.append(
            ShellSpec.from_mapping(
                {
                    "identifier": identifier,
                    "planes": _as_int(args[2], "planes"),
                    "satellites_per_plane": _as_int(args[3], "satellites_per_plane"),
                    "inclination_deg": _as_float(args[4], "inclination"),
                    "revs_per_day": _as_float(args[5], "revs_per_day"),
                    "pattern": pattern,
                }
            )
        )

    def _gnd(self, args: list[str]) -> None:
        self._require(args, "gnd add|list|remove")
        action = args[0].lower()
        if action == "list":
            satellite_count = sum(
                shell.planes * shell.satellites_per_plane for shell in self.draft.shells
            )
            if not self.draft.ground_stations:
                self._output("no ground stations")
            for index, station in enumerate(self.draft.ground_stations):
                self._output(
                    f"Gnd{satellite_count + index}: {station.name} "
                    f"lat={station.latitude:g} lon={station.longitude:g} "
                    f"elev={station.elevation_km:g} km"
                )
            return
        if action == "remove":
            self._require(args[1:], "gnd remove <name>")
            name = args[1]
            if not any(station.name == name for station in self.draft.ground_stations):
                raise ScenarioError(f"unknown ground station {name!r}")
            self.draft.ground_stations = [
                station
                for station in self.draft.ground_stations
                if station.name != name
            ]
            return
        if action != "add":
            raise ScenarioError("usage: gnd add|list|remove")
        if self.draft.topology is not None:
            raise ScenarioError(
                "cannot add ground stations while a topology is selected; "
                "run 'topology none' first"
            )
        if len(args) < 4:
            raise ScenarioError("usage: gnd add <name> <lat> <lon> [elev_km]")
        name = args[1]
        if any(station.name == name for station in self.draft.ground_stations):
            raise ScenarioError(f"ground station {name!r} already exists")
        latitude = _as_float(args[2], "latitude")
        longitude = _as_float(args[3], "longitude")
        if not -90.0 <= latitude <= 90.0:
            raise ScenarioError("latitude must be in [-90, 90]")
        if not -180.0 <= longitude <= 180.0:
            raise ScenarioError("longitude must be in [-180, 180]")
        self.draft.ground_stations.append(
            GroundStationSpec.from_mapping(
                {
                    "name": name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "elevation_km": (
                        _as_float(args[4], "elevation_km") if len(args) > 4 else 0.0
                    ),
                }
            )
        )

    def _traffic(self, args: list[str]) -> None:
        self._require(args, "traffic add|list|remove")
        action = args[0].lower()
        if action == "list":
            if not self.draft.traffic:
                self._output("no traffic")
            for index, flow in enumerate(self.draft.traffic, start=1):
                self._output(
                    f"{index}: {flow.tool} {flow.src}->{flow.dst} "
                    f"start={flow.start_s:g}s"
                )
            return
        if action == "remove":
            self._require(args[1:], "traffic remove <index>")
            index = _as_int(args[1], "index")
            if not 1 <= index <= len(self.draft.traffic):
                raise ScenarioError(f"traffic index {index} out of range")
            self.draft.traffic.pop(index - 1)
            return
        if action != "add":
            raise ScenarioError("usage: traffic add|list|remove")
        if len(args) < 4:
            raise ScenarioError(
                "usage: traffic add <src> <dst> <ping|iperf3> [key=value...]"
            )
        src, dst, tool = args[1], args[2], args[3].lower()
        if tool not in {"ping", "iperf3"}:
            raise ScenarioError("traffic tool must be ping or iperf3")
        values = _split_kv(args[4:])
        start = _first(values, "start", "start_s")
        duration = _first(values, "duration", "duration_s")
        count = _first(values, "count")
        protocol = _first(values, "protocol")
        bandwidth = _first(values, "bandwidth", "bandwidth_mbps")
        if values:
            raise ScenarioError(
                "unknown traffic option(s): " + ", ".join(sorted(values))
            )
        parsed_count = _as_int(count, "count") if count is not None else 10
        if parsed_count < 1:
            raise ScenarioError("count must be >= 1")
        parsed_protocol = (protocol or "TCP").upper()
        if parsed_protocol not in {"TCP", "UDP"}:
            raise ScenarioError("protocol must be TCP or UDP")
        parsed_duration = (
            _as_float(duration, "duration") if duration is not None else 10.0
        )
        if parsed_duration <= 0:
            raise ScenarioError("duration must be > 0")
        self.draft.traffic.append(
            TrafficSpec.from_mapping(
                {
                    "src": src,
                    "dst": dst,
                    "tool": tool,
                    "start_s": (
                        _as_float(start, "start") if start is not None else 0.0
                    ),
                    "duration_s": parsed_duration,
                    "count": parsed_count,
                    "protocol": parsed_protocol,
                    "bandwidth_mbps": (
                        _as_float(bandwidth, "bandwidth")
                        if bandwidth is not None
                        else 0.0
                    ),
                }
            )
        )

    def _event(self, args: list[str]) -> None:
        self._require(args, "event add|list|remove")
        action = args[0].lower()
        if action == "list":
            if not self.draft.events:
                self._output("no events")
            for index, event in enumerate(self.draft.events, start=1):
                self._output(
                    f"{index}: {event.action} at={event.at_s:g}s "
                    f"src={event.src} dst={event.dst}"
                )
            return
        if action == "remove":
            self._require(args[1:], "event remove <index>")
            index = _as_int(args[1], "index")
            if not 1 <= index <= len(self.draft.events):
                raise ScenarioError(f"event index {index} out of range")
            self.draft.events.pop(index - 1)
            return
        if action != "add":
            raise ScenarioError("usage: event add|list|remove")
        values = _split_kv(args[1:])
        mapping: dict[str, Any] = {}
        aliases = {
            "at": "at_s",
            "action": "action",
            "src": "src",
            "dst": "dst",
            "duration": "duration_s",
            "loss": "loss_percent",
            "jitter": "jitter_ms",
        }
        for key, value in values.items():
            target = aliases.get(key, key)
            if target in {"at_s", "duration_s", "loss_percent"}:
                mapping[target] = _as_float(value, key)
            elif target == "jitter_ms":
                mapping[target] = _as_int(value, key)
            else:
                mapping[target] = value
        self.draft.events.append(EventSpec.from_mapping(mapping))


def _as_int(value: str | None, label: str) -> int:
    if value is None:
        raise ScenarioError(f"{label} is required")
    try:
        return int(value)
    except ValueError as exc:
        raise ScenarioError(f"{label} must be an integer, got {value!r}") from exc


def _as_float(value: str | None, label: str) -> float:
    if value is None:
        raise ScenarioError(f"{label} is required")
    try:
        number = float(value)
    except ValueError as exc:
        raise ScenarioError(f"{label} must be a number, got {value!r}") from exc
    if not math.isfinite(number):
        raise ScenarioError(f"{label} must be finite, got {value!r}")
    return number


def _as_bool(value: str, label: str) -> bool:
    lowered = value.lower()
    if lowered in {"on", "true", "yes", "1"}:
        return True
    if lowered in {"off", "false", "no", "0"}:
        return False
    raise ScenarioError(f"{label} must be on/off, got {value!r}")


def _as_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
