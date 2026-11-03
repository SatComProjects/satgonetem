#!/usr/bin/env python3
"""End-to-end connectivity check for the IPv4 address plan.

Builds a three-node topology (two satellites, one ground station), starts it on
GoNetEm, assigns planned addresses, installs static routes, and pings loopback
addresses across a GSL and an ISL.

Examples:
    .venv/bin/python scripts/verify_connectivity.py
    .venv/bin/python scripts/verify_connectivity.py --transport docker

``--transport docker`` bypasses GoNetEm NodeRun and executes commands with
docker-py.  Use it when the GoNetEm server cannot nsenter into containers.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from satgonetem.services.topology_satcom import NetworkConfig, TopologyManager
from satgonetem.traffic import PingConfig, PingStatus
from satgonetem.utils.project_builder import (
    add_custom_ground_station,
    create_and_load_simulation,
    create_custom_ground_station,
    create_custom_satellite,
    create_empty_project,
)

PING_TIMEOUT_SEC = 20.0


def _read_tle(path: str, count: int) -> list[dict[str, str]]:
    lines = Path(path).read_text().splitlines()
    if len(lines) < count * 3:
        raise RuntimeError(f"{path} does not contain {count} TLE entries")
    return [
        {
            "name": lines[3 * index].strip(),
            "tle_line1": lines[3 * index + 1].strip(),
            "tle_line2": lines[3 * index + 2].strip(),
        }
        for index in range(count)
    ]


def _wait_for_ping(flow) -> object:
    deadline = time.time() + PING_TIMEOUT_SEC
    while flow.status() == PingStatus.RUNNING and time.time() < deadline:
        time.sleep(0.2)
    if flow.status() != PingStatus.DONE:
        raise RuntimeError(f"ping did not finish: {flow.status()}")
    return flow.results()


def run(transport: str) -> int:
    project = create_empty_project(
        simulation_name="AddressConnectivity",
        start_date="03/11/2026 13:00:00",
        end_date="03/11/2026 13:10:00",
    )
    simulation = create_and_load_simulation(
        project.model_dump(), project.simulation_name
    )

    for tle in _read_tle("tle_files/oneweb.txt", 2):
        create_custom_satellite(tle, simulation)
    ground_station = create_custom_ground_station(
        "Paris", 48.8566, 2.3522, 0.035
    )
    add_custom_ground_station(ground_station, simulation)
    simulation.update_ground_station_links()

    satcom_sats = simulation.get_satellites()
    satcom_gss = simulation.get_ground_stations()
    simulation.create_and_add_inter_satellite_link_connection(
        satcom_sats[0], satcom_sats[1]
    )
    simulation.create_and_add_ground_station_link_connection(
        satcom_sats[0], satcom_gss[0]
    )

    topology = TopologyManager.from_simulation_manager(
        simulation, NetworkConfig(update_time=1)
    )
    sat0 = topology.satellites[satcom_sats[0].topology_uniq_id]
    sat1 = topology.satellites[satcom_sats[1].topology_uniq_id]
    gs = topology.ground_stations[satcom_gss[0].topology_uniq_id]
    topology.set_addressable_satellites([sat0, sat1])

    try:
        print("starting GoNetEm ...", flush=True)
        topology.start_gonetem()

        if transport == "docker":
            missing = []
            for node in (sat0, sat1, gs):
                if node.container is None:
                    missing.append(node.name)
                # Force docker-py instead of GoNetEm NodeRun.
                node.grpc_client = None
            if missing:
                raise RuntimeError(
                    "launcher did not attach docker containers: "
                    + ", ".join(missing)
                )

        print("pushing planned addresses ...", flush=True)
        topology.set_ip_addresses()
        print("installing static routes ...", flush=True)
        topology.init_routing(routing_method="static")

        for node in (sat0, sat1, gs):
            print(
                f"{node.name}: block={node.address_block} "
                f"loopback={node.loopback.ipv4}/32 "
                f"links={[f'{i.ipv4}/{i.prefix_len}' for i in node.interfaces]}",
                flush=True,
            )

        cases = (
            ("GS -> Sat1 loopback (GSL+ISL)", gs, sat1),
            ("Sat1 -> GS loopback (ISL+GSL)", sat1, gs),
            ("Sat0 -> Sat1 loopback (ISL)", sat0, sat1),
        )
        for label, source, destination in cases:
            flows = topology.ping(
                source,
                destination,
                PingConfig(count=3, interval_sec=0.2, preload=1),
            )
            result = _wait_for_ping(flows[0])
            print(
                f"{label}: transmitted={result.packets_transmitted} "
                f"received={result.packets_received} "
                f"loss={result.packet_loss_percent}% "
                f"rtt_avg={result.rtt_avg_ms} ms",
                flush=True,
            )
            if result.packets_received == 0:
                raise RuntimeError(f"{label}: no ICMP replies")
        print("OK: loopback connectivity works", flush=True)
        return 0
    finally:
        try:
            topology.stop_gonetem()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            print(f"cleanup warning: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("grpc", "docker"),
        default="grpc",
        help="command transport for container operations (default: grpc)",
    )
    args = parser.parse_args()
    return run(args.transport)


if __name__ == "__main__":
    raise SystemExit(main())
