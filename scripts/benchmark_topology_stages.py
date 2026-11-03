#!/usr/bin/env python3
"""Offline benchmark for the topology polling path (Phase 0).

This measures ``TopologyManager.update_simulation()`` without Docker, GoNetEm,
or traffic. It is intended to establish the per-tick cost of the pull-based
path before the V5 EventBus work starts.

Run one scenario per process so ``ru_maxrss`` reflects that scenario only:

    .venv/bin/python scripts/benchmark_topology_stages.py \
        --scenario starlink-g2-basic-60 --ticks 10

The script prints a Markdown row plus a JSON object. Use ``--list`` to see the
available scenarios.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import resource
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectData,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty
from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager

START_DATE = "2026-01-01 00:00:00.000"
END_DATE = "2026-01-02 00:00:00.000"
GROUND_OBJECT_COUNT = 100


@dataclass(frozen=True)
class Scenario:
    key: str
    satellites: int
    planes: int
    satellites_per_plane: int
    inclination: float
    mean_motion: float
    model: str
    tick_seconds: int
    addressing_plan: str


SCENARIOS = {
    "starlink-g2-basic-60": Scenario(
        "starlink-g2-basic-60", 720, 36, 20, 70.0, 15.2, "basic", 60,
        "FULL_MESH_WALKER_DELTA",
    ),
    "starlink-g2-advanced-60": Scenario(
        "starlink-g2-advanced-60", 720, 36, 20, 70.0, 15.2, "advanced", 60,
        "FULL_MESH_WALKER_DELTA",
    ),
    "starlink-1008-basic-60": Scenario(
        "starlink-1008-basic-60", 1008, 36, 28, 53.0, 15.5, "basic", 60,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-1008-advanced-60": Scenario(
        "starlink-1008-advanced-60", 1008, 36, 28, 53.0, 15.5, "advanced", 60,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-g7-basic-60": Scenario(
        "starlink-g7-basic-60", 3360, 28, 120, 53.0, 15.5, "basic", 60,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-g7-advanced-60": Scenario(
        "starlink-g7-advanced-60", 3360, 28, 120, 53.0, 15.5, "advanced", 60,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-g2-basic-5": Scenario(
        "starlink-g2-basic-5", 720, 36, 20, 70.0, 15.2, "basic", 5,
        "FULL_MESH_WALKER_DELTA",
    ),
    "starlink-g2-advanced-5": Scenario(
        "starlink-g2-advanced-5", 720, 36, 20, 70.0, 15.2, "advanced", 5,
        "FULL_MESH_WALKER_DELTA",
    ),
    "starlink-1008-basic-5": Scenario(
        "starlink-1008-basic-5", 1008, 36, 28, 53.0, 15.5, "basic", 5,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-1008-advanced-5": Scenario(
        "starlink-1008-advanced-5", 1008, 36, 28, 53.0, 15.5, "advanced", 5,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-g7-basic-5": Scenario(
        "starlink-g7-basic-5", 3360, 28, 120, 53.0, 15.5, "basic", 5,
        "CHECKERBOARD_WALKER_DELTA",
    ),
    "starlink-g7-advanced-5": Scenario(
        "starlink-g7-advanced-5", 3360, 28, 120, 53.0, 15.5, "advanced", 5,
        "CHECKERBOARD_WALKER_DELTA",
    ),
}


def _ground_objects(count: int) -> list[GroundObjectData]:
    """Return a deterministic, spread-out set of ground stations."""
    objects = []
    side = 10
    for index in range(count):
        row, column = divmod(index, side)
        latitude = -60.0 + 120.0 * row / (side - 1)
        longitude = -180.0 + 360.0 * column / (side - 1)
        objects.append(
            GroundObjectData(
                id=index,
                label=f"GS{index}",
                longitude=longitude,
                latitude=latitude,
                altitude=0.0,
            )
        )
    return objects


def build_simulation_property(scenario: Scenario) -> SimulationProperty:
    shell_identifier = f"Bench-{scenario.satellites}"
    shell = WalkerShellProperty(
        type="delta",
        static_addressing_plan=scenario.addressing_plan,
        constellation_property=WalkerConstellationProperty(
            identifier=shell_identifier,
            amount_of_orbit_plane=scenario.planes,
            amount_of_satellite_per_orbit_plane=scenario.satellites_per_plane,
            inclination=scenario.inclination,
            phase_difference_between_satellites=True,
            mean_revolution_per_day=scenario.mean_motion,
        ),
        orbital_connectivity_property=OrbitalConnectivityProperty(
            adjacent_inter_satellite_shifting=0,
            maximum_connected_ground_object=100,
            maximum_connected_user_terminal=0,
            maximum_connected_ground_station=10,
        ),
        ground_object_white_list=["Bench GS"],
    )
    ground_objects = GroundObjectProperty(
        identifier="Bench GS",
        type="ground_station",
        ground_objects=_ground_objects(GROUND_OBJECT_COUNT),
        connectivity_properties=GroundConnectivityProperty(
            elevation_above_horizon=20.0,
            maximum_connected_satellites=1,
            shells_whitelist=[shell_identifier],
            ground_to_space_connections_strategy="best-angle-until-disconnection",
        ),
    )
    return SimulationProperty(
        simulation_name=f"phase0-{scenario.key}",
        start_date=START_DATE,
        end_date=END_DATE,
        walker_shells=[shell],
        ground_objects_properties=[ground_objects],
        inter_satellite_link_connection_strategy="intra-shell-static-connectivity",
        optical_inter_satellite_link_model=scenario.model,
    )


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def run_scenario(scenario: Scenario, ticks: int, graph_repeats: int) -> dict:
    gc.collect()
    started_at = time.perf_counter()
    manager = TopologyManager.from_satcom(
        build_simulation_property(scenario),
        NetworkConfig(
            update_time=scenario.tick_seconds,
            topology_events=False,
        ),
    )
    load_seconds = time.perf_counter() - started_at

    # Warm up caches and the satellite state cache before measuring.
    # ``next_step`` is decorated with the project's timing helper, which prints
    # to stdout; keep that noise off the JSON stream.
    with contextlib.redirect_stdout(sys.stderr):
        manager.next_step()

    totals: list[float] = []
    stages: dict[str, list[float]] = defaultdict(list)
    for _ in range(ticks):
        tick_started_at = time.perf_counter()
        with contextlib.redirect_stdout(sys.stderr):
            manager.next_step()
        totals.append(time.perf_counter() - tick_started_at)
        for name, value in manager.get_last_stage_timings().items():
            stages[name].append(value)

    graph_times = []
    for _ in range(graph_repeats):
        graph_started_at = time.perf_counter()
        manager.get_current_graph()
        graph_times.append(time.perf_counter() - graph_started_at)

    stage_summary = {
        name: {
            "p50": _percentile(values, 50),
            "p95": _percentile(values, 95),
        }
        for name, values in stages.items()
    }
    return {
        "scenario": scenario.key,
        "satellites": scenario.satellites,
        "optical_model": scenario.model,
        "tick_seconds": scenario.tick_seconds,
        "ticks": ticks,
        "load_seconds": load_seconds,
        "isl_count": len(manager.simulation_manager.get_inter_satellites_links()),
        "gsl_count": len(manager.simulation_manager.get_ground_stations_links()),
        "link_count": len(manager.links),
        "tick_p50_seconds": _percentile(totals, 50),
        "tick_p95_seconds": _percentile(totals, 95),
        "graph_build_p50_seconds": _percentile(graph_times, 50),
        "peak_rss_mb": _peak_rss_mb(),
        "stages": stage_summary,
    }


def _markdown_row(result: dict) -> str:
    return (
        f"| {result['scenario']} | {result['satellites']} | "
        f"{result['isl_count']} | {result['gsl_count']} | "
        f"{result['optical_model']} | {result['tick_seconds']} | "
        f"{result['load_seconds']:.2f} | {result['tick_p50_seconds']:.3f} | "
        f"{result['tick_p95_seconds']:.3f} | "
        f"{result['graph_build_p50_seconds']:.3f} | "
        f"{result['peak_rss_mb']:.0f} |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="starlink-g2-basic-60",
        help="Scenario to benchmark (default: %(default)s).",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        default=10,
        help="Measured ticks after one warm-up tick (default: %(default)s).",
    )
    parser.add_argument(
        "--graph-repeats",
        type=int,
        default=3,
        help="Repeats for the routing graph build (default: %(default)s).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List scenario names and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the JSON result.",
    )
    args = parser.parse_args()

    if args.list:
        for key in sorted(SCENARIOS):
            print(key)
        return

    if args.ticks < 1:
        parser.error("--ticks must be at least 1")
    if args.graph_repeats < 1:
        parser.error("--graph-repeats must be at least 1")

    result = run_scenario(
        SCENARIOS[args.scenario],
        ticks=args.ticks,
        graph_repeats=args.graph_repeats,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(
        "| Scenario | Sats | ISLs | GSLs | Model | Tick s | Load s | Tick p50 | "
        "Tick p95 | Graph p50 | Peak MB |"
    )
    print("|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|")
    print(_markdown_row(result))
    print()
    print("Stage medians (seconds):")
    for name in sorted(result["stages"]):
        p50 = result["stages"][name]["p50"]
        p95 = result["stages"][name]["p95"]
        print(f"- {name}: p50={p50:.4f}, p95={p95:.4f}")


if __name__ == "__main__":
    main()
