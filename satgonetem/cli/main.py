"""Command-line interface for SatGoNetem."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["build_parser", "main"]


@dataclass(frozen=True)
class DoctorCheck:
    """One prerequisite check result."""

    name: str
    ok: bool
    detail: str


def _check_python() -> DoctorCheck:
    version = sys.version_info
    ok = version >= (3, 10)
    return DoctorCheck(
        "python",
        ok,
        f"{version.major}.{version.minor}.{version.micro}"
        + ("" if ok else " (requires >= 3.10)"),
    )


def _check_docker() -> DoctorCheck:
    try:
        import docker

        client = docker.from_env(timeout=2)
        client.ping()
        return DoctorCheck("docker", True, "daemon reachable")
    except Exception as exc:  # noqa: BLE001 - report prerequisite failure
        return DoctorCheck("docker", False, str(exc))


def _check_server(address: str) -> DoctorCheck:
    try:
        host, port_text = address.rsplit(":", 1)
        port = int(port_text)
    except (ValueError, AttributeError):
        return DoctorCheck("gonetem", False, f"invalid server address {address!r}")
    try:
        with socket.create_connection((host, port), timeout=2):
            return DoctorCheck("gonetem", True, f"{host}:{port} reachable")
    except OSError as exc:
        return DoctorCheck("gonetem", False, str(exc))


def _check_topology_data() -> DoctorCheck:
    try:
        from satgonetem.data import sample_topology_names

        names = sample_topology_names()
        if "test_topology.json" in names:
            return DoctorCheck(
                "topology-data", True, f"{len(names)} bundled topology file(s)"
            )
        return DoctorCheck(
            "topology-data", False, "test_topology.json is missing from the package"
        )
    except Exception as exc:  # noqa: BLE001 - report prerequisite failure
        return DoctorCheck("topology-data", False, str(exc))


def doctor_checks(server: str = "localhost:10110") -> list[DoctorCheck]:
    """Run all prerequisite checks."""
    return [
        _check_python(),
        _check_docker(),
        _check_server(server),
        _check_topology_data(),
    ]


def _print_checks(checks: Iterable[DoctorCheck], as_json: bool) -> None:
    values = [
        {"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks
    ]
    if as_json:
        print(json.dumps(values, indent=2))
        return
    for check in values:
        marker = "OK" if check["ok"] else "FAIL"
        print(f"[{marker:4}] {check['name']}: {check['detail']}")


def _cmd_version(_args: argparse.Namespace) -> int:
    import satgonetem

    print(satgonetem.__version__)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    checks = doctor_checks(args.server)
    _print_checks(checks, args.json)
    return 0 if all(check.ok for check in checks) else 1


def _cmd_summary(args: argparse.Namespace) -> int:
    from satgonetem.data import resolve_topology
    from satgonetem.services.topology_satcom import TopologyManager

    path = resolve_topology(args.topology)
    topology = TopologyManager.from_file(path)
    link_types: dict[str, int] = {}
    for link in topology.get_links():
        link_type = str(getattr(link, "type", ""))
        link_types[link_type] = link_types.get(link_type, 0) + 1

    summary = {
        "path": path,
        "satellites": len(topology.get_satellites()),
        "ground_stations": len(topology.get_ground_stations()),
        "links": len(topology.get_links()),
        "link_types": link_types,
        "ipv4_plan": topology.address_plan is not None,
        "ipv6_enabled": bool(getattr(topology, "ipv6_enabled", False)),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Topology: {path}")
        print(f"Satellites: {summary['satellites']}")
        print(f"Ground stations: {summary['ground_stations']}")
        print(f"Links: {summary['links']} {link_types}")
        print(
            f"IPv6 addressing: {'enabled' if summary['ipv6_enabled'] else 'disabled'}"
        )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from satgonetem.experiment import ExperimentRunner, Scenario

    scenario = Scenario.from_file(args.scenario)
    runner = ExperimentRunner(scenario)
    result = runner.run(
        output_dir=args.out,
        live=not args.offline,
        dry_run=args.dry_run,
    )
    print(result.run_dir)
    if result.errors:
        for error in result.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from satgonetem.telemetry import build_report

    summary = build_report(args.run_dir)
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Run: {summary['name']}")
        print(f"Samples: {summary['metrics']['samples']}")
        print(
            f"Flows: {summary['flows']['count']} ({summary['flows']['errors']} errors)"
        )
        print(Path(args.run_dir) / "report.md")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="satgonetem",
        description="SatGoNetem satellite network emulation tool",
    )
    subparsers = parser.add_subparsers(dest="command")

    version = subparsers.add_parser("version", help="print the package version")
    version.set_defaults(func=_cmd_version)

    doctor = subparsers.add_parser("doctor", help="check host prerequisites")
    doctor.add_argument("--server", default="localhost:10110")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    summary = subparsers.add_parser(
        "summary", help="summarize a topology file without starting it"
    )
    summary.add_argument("topology")
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=_cmd_summary)

    run = subparsers.add_parser("run", help="run a scenario manifest")
    run.add_argument("scenario")
    run.add_argument("--out", default=None)
    run.add_argument(
        "--offline",
        action="store_true",
        help="load and sample the topology without starting containers",
    )
    run.add_argument("--dry-run", action="store_true", help="validate only")
    run.set_defaults(func=_cmd_run)

    report = subparsers.add_parser("report", help="summarize a run directory")
    report.add_argument("run_dir")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
