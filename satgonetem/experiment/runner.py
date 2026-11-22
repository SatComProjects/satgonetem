"""Execute a declarative scenario against a topology."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from satgonetem.data import resolve_topology
from satgonetem.telemetry import (
    JsonlMetricsWriter,
    MetricsCollector,
    build_report,
    write_json,
)

from .scenario import EventSpec, Scenario, TrafficSpec

__all__ = ["ExperimentRunner", "RunResult"]


@dataclass
class RunResult:
    """Outcome of one scenario run."""

    run_dir: Path
    summary: dict[str, Any]
    samples: list[dict[str, Any]] = field(default_factory=list)
    flows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ExperimentRunner:
    """Load a scenario, drive the topology, and write run artifacts.

    The runner is deliberately thin: all topology operations go through
    public ``TopologyManager`` methods, so the same code works with a test
    double. A live run requires Docker and GoNetEm; ``live=False`` is for
    validation and offline sampling.
    """

    def __init__(
        self,
        scenario: Scenario,
        *,
        topology_factory: Callable[[Path | None, Any], Any] | None = None,
        collector_factory: Callable[[Any], Any] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.scenario = scenario
        self._topology_factory = topology_factory
        self._collector_factory = collector_factory or MetricsCollector
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._events = self._expand_events(scenario.events)
        self._event_index = 0
        self._started_traffic: set[int] = set()
        self._flows: list[Any] = []
        self._errors: list[str] = []
        self._started_backend = False

    # ------------------------------------------------------------------
    # Scenario helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _expand_events(events: tuple[EventSpec, ...]) -> list[EventSpec]:
        expanded: list[EventSpec] = []
        for event in events:
            expanded.append(event)
            if event.action == "link_down" and event.duration_s > 0:
                expanded.append(
                    EventSpec(
                        at_s=event.at_s + event.duration_s,
                        action="link_up",
                        src=event.src,
                        dst=event.dst,
                    )
                )
        return sorted(expanded, key=lambda value: value.at_s)

    def _resolve_topology(self) -> Path:
        topology = self.scenario.topology
        if topology is None:
            raise RuntimeError("scenario has no topology path")
        raw = Path(topology)
        candidates = [raw]
        if not raw.is_absolute():
            candidates.insert(0, Path(self.scenario.base_dir) / raw)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return Path(resolve_topology(topology))

    def _load_topology(self) -> Any:
        from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

        if self.scenario.topology is None:
            config = (
                NetworkConfig.from_mapping(dict(self.scenario.network))
                if self.scenario.network
                else None
            )
            if self._topology_factory is not None:
                return self._topology_factory(None, config)
            from satgonetem.experiment.builder import build_scenario_topology

            return build_scenario_topology(self.scenario, config)

        topology_path = self._resolve_topology()
        config = None
        if self.scenario.network:
            payload = json.loads(topology_path.read_text(encoding="utf-8"))
            base = NetworkConfig(**payload.get("network_config", {}))
            config = NetworkConfig.from_mapping(self.scenario.network, base=base)
        if self._topology_factory is not None:
            return self._topology_factory(topology_path, config)
        return TopologyManager.from_file(str(topology_path), network_config=config)

    def _output_dir(self, output_dir: str | Path | None) -> Path:
        if output_dir is not None:
            root = Path(output_dir)
        elif self.scenario.output_dir:
            root = Path(self.scenario.output_dir)
        else:
            stamp = self._now().strftime("%Y%m%dT%H%M%SZ")
            root = Path("runs") / f"{self.scenario.name}_{stamp}"
        if root.exists() and any(root.iterdir()):
            for index in range(1, 10_000):
                candidate = root.with_name(f"{root.name}_{index}")
                if not candidate.exists():
                    root = candidate
                    break
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _sim_time(topology: Any, tick_s: int) -> float:
        step = int(getattr(topology, "current_time_step", 0) or 0)
        tick = getattr(topology, "update_time", tick_s) or tick_s
        return float(step) * float(tick)

    # ------------------------------------------------------------------
    # Runtime actions
    # ------------------------------------------------------------------

    def _find_link(self, topology: Any, source: str | None, target: str | None) -> Any:
        if not source or not target:
            raise RuntimeError("Link event requires src and dst")
        wanted = {source, target}
        for link in getattr(topology, "links", {}).values():
            names = {
                getattr(getattr(link, "source", None), "name", ""),
                getattr(getattr(link, "target", None), "name", ""),
            }
            if names == wanted:
                return link
        raise RuntimeError(f"Link {source}--{target} is not in the topology")

    def _apply_events(self, topology: Any, sim_s: float) -> None:
        while self._event_index < len(self._events):
            event = self._events[self._event_index]
            if event.at_s > sim_s:
                break
            self._event_index += 1
            try:
                self._apply_event(topology, event)
            except Exception as exc:  # noqa: BLE001 - record and continue
                message = f"event {event.action} at {event.at_s}s failed: {exc}"
                logging.error(message)
                self._errors.append(message)

    def _apply_event(self, topology: Any, event: EventSpec) -> None:
        if event.action == "node_restart":
            topology.restart_node(event.src)
            return

        link = self._find_link(topology, event.src, event.dst)
        if event.action == "link_down":
            set_state = getattr(topology, "set_link_state", None)
            if callable(set_state):
                set_state(link, up=False)
            else:
                topology.simulate_link_failure([link])
        elif event.action == "link_up":
            set_state = getattr(topology, "set_link_state", None)
            if not callable(set_state):
                raise RuntimeError("link_up requires set_link_state support")
            set_state(link, up=True)
        elif event.action == "impairment":
            set_impairment = getattr(topology, "set_link_impairment", None)
            if not callable(set_impairment):
                raise RuntimeError("impairment events require set_link_impairment")
            set_impairment(
                link,
                loss_percent=event.loss_percent,
                jitter_ms=event.jitter_ms,
            )
        else:
            raise RuntimeError(f"Unsupported event action {event.action!r}")

    def _start_traffic(self, topology: Any, sim_s: float, live: bool) -> None:
        if not live:
            return
        for index, spec in enumerate(self.scenario.traffic):
            if index in self._started_traffic or spec.start_s > sim_s:
                continue
            self._started_traffic.add(index)
            try:
                self._flows.extend(self._create_flow(topology, spec))
            except Exception as exc:  # noqa: BLE001 - record and continue
                message = f"traffic {spec.src}->{spec.dst} failed: {exc}"
                logging.error(message)
                self._errors.append(message)

    def _create_flow(self, topology: Any, spec: TrafficSpec) -> list[Any]:
        source = topology.get_node_by_name(spec.src)
        destination = topology.get_node_by_name(spec.dst)
        if source is None:
            raise RuntimeError(f"Unknown source node {spec.src!r}")
        if destination is None:
            raise RuntimeError(f"Unknown destination node {spec.dst!r}")

        if spec.tool == "ping":
            from satgonetem.traffic import PingConfig

            return list(
                topology.ping(
                    source,
                    destination,
                    PingConfig(count=spec.count),
                )
            )

        from satgonetem.traffic import Iperf3Config

        return list(
            topology.run_iperf3(
                source,
                destination,
                Iperf3Config(
                    protocol=spec.protocol,
                    duration=max(1, int(round(spec.duration_s))),
                    bandwidth_mbps=spec.bandwidth_mbps,
                ),
            )
        )

    def _collect_flows(self, timeout_s: float = 300.0) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for flow in self._flows:
            source = getattr(getattr(flow, "source", None), "name", "")
            destination = getattr(getattr(flow, "destination", None), "name", "")
            entry: dict[str, Any] = {
                "source": source,
                "destination": destination,
                "tool": type(flow).__name__,
                "error": None,
            }
            try:
                join = getattr(flow, "join", None)
                if callable(join):
                    join(timeout=timeout_s)
                if getattr(flow, "is_alive", lambda: False)():
                    stop = getattr(flow, "stop", None)
                    if callable(stop):
                        stop()
                    entry["error"] = "flow did not finish before the timeout"
                else:
                    result = flow.results()
                    to_json = getattr(result, "to_json", None)
                    entry["result"] = to_json() if callable(to_json) else repr(result)
            except Exception as exc:  # noqa: BLE001 - one flow must not abort
                entry["error"] = str(exc)
            if entry["error"]:
                self._errors.append(
                    f"flow {entry['source']}->{entry['destination']}: {entry['error']}"
                )
            collected.append(entry)
        return collected

    def _stop_backend(self, topology: Any) -> None:
        if not self._started_backend:
            return
        try:
            stop = getattr(topology, "stop", None)
            if callable(stop):
                stop()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logging.warning("Stopping the simulation loop failed: %s", exc)
        try:
            topology.stop_gonetem()
        except Exception as exc:  # noqa: BLE001 - preserve primary error
            logging.warning("Stopping GoNetEm failed: %s", exc)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        output_dir: str | Path | None = None,
        *,
        live: bool = True,
        dry_run: bool = False,
        realtime: bool | None = None,
    ) -> RunResult:
        """Run the scenario and write artifacts.

        Args:
            output_dir: Explicit run directory. Defaults to the scenario's
                ``output_dir`` or ``runs/<name>_<UTC timestamp>``.
            live: Start GoNetEm and run traffic. ``False`` loads the topology
                and samples it without starting containers.
            dry_run: Validate and write the manifest without loading the
                topology.
            realtime: Pace ticks at ``tick_s`` wall-clock seconds. Defaults to
                True for a live run with traffic, so flow durations and
                simulation ticks share a time base. Tests pass False.
        """
        if realtime is None:
            realtime = bool(live and self.scenario.traffic)
        run_dir = self._output_dir(output_dir)
        manifest = self.scenario.to_dict()
        manifest["run_dir"] = str(run_dir)
        manifest["started_at"] = self._now().isoformat()
        (run_dir / "manifest.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )

        if dry_run:
            (run_dir / "metrics.jsonl").write_text("", encoding="utf-8")
            write_json(run_dir / "flows.json", [])
            summary = build_report(run_dir)
            return RunResult(run_dir=run_dir, summary=summary, errors=[])

        topology = self._load_topology()
        topology.update_time = self.scenario.tick_s
        if hasattr(topology, "update_factor"):
            topology.update_factor = 1.0
        collector = self._collector_factory(topology)
        samples: list[dict[str, Any]] = []
        flows: list[dict[str, Any]] = []

        try:
            if live:
                topology.start_gonetem()
                self._started_backend = True
                topology.set_ip_addresses()
                if self.scenario.routing:
                    result = topology.init_routing(self.scenario.routing)
                    if isinstance(result, (int, float)) and result < 0:
                        raise RuntimeError(
                            f"Routing initialization failed for "
                            f"{self.scenario.routing!r}"
                        )

            with JsonlMetricsWriter(run_dir / "metrics.jsonl") as writer:
                sample = collector.sample()
                samples.append(sample)
                writer.write(sample)
                last_sample_s = 0.0

                self._start_traffic(topology, 0.0, live)
                if live:
                    self._apply_events(topology, 0.0)
                t0 = time.monotonic()
                steps = 0

                while True:
                    sim_s = self._sim_time(topology, self.scenario.tick_s)
                    if sim_s >= self.scenario.duration_s:
                        break
                    topology.next_step()
                    steps += 1
                    sim_s = self._sim_time(topology, self.scenario.tick_s)
                    if live:
                        self._apply_events(topology, sim_s)
                    self._start_traffic(topology, sim_s, live)
                    if sim_s - last_sample_s >= self.scenario.metrics_interval_s:
                        sample = collector.sample()
                        samples.append(sample)
                        writer.write(sample)
                        last_sample_s = sim_s
                    if realtime:
                        target = t0 + steps * self.scenario.tick_s
                        delay = target - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)

            flows = self._collect_flows()
            write_json(run_dir / "flows.json", flows)
            if live and self.scenario.connectivity:
                try:
                    matrix = topology.connectivity_matrix()
                    write_json(run_dir / "connectivity.json", matrix.to_dict())
                except Exception as exc:  # noqa: BLE001 - report and continue
                    message = f"connectivity matrix failed: {exc}"
                    logging.error(message)
                    self._errors.append(message)
        finally:
            self._stop_backend(topology)

        summary = build_report(run_dir)
        return RunResult(
            run_dir=run_dir,
            summary=summary,
            samples=samples,
            flows=flows,
            errors=list(self._errors),
        )
