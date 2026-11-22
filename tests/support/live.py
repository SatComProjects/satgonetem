"""Shared helpers for the live integration tier.

These helpers are imported only by ``tests/integration``. They connect to the
real Docker daemon and GoNetEm server, so they must never be used by the
default unit suite.
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any

import docker
import grpc
import pytest
from google.protobuf import empty_pb2

from satgonetem.cli.main import DoctorCheck, doctor_checks
from satgonetem.proto import netem_pb2, netem_pb2_grpc
from satgonetem.traffic import PingFlow, PingResults, PingStatus

#: Default GoNetEm gRPC endpoint, matching ``NetworkConfig.gonetem_server``.
GONETEM_SERVER = "localhost:10110"

#: Images required by the live tier (satellite, ground station, root netns).
REQUIRED_IMAGES = (
    "jariassuarez/sgnt:satellite",
    "jariassuarez/sgnt:ground-station",
    "alpine:3.20",
)

_WAIT_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class LiveEnvironment:
    """Verified live dependencies shared by the integration session."""

    docker: docker.DockerClient
    server_address: str
    server_version: str
    required_images: tuple[str, ...]
    doctor_checks: tuple[DoctorCheck, ...]


def unique_project_name(prefix: str) -> str:
    """Return a GoNetEm project name that cannot collide with another run."""
    if not prefix:
        raise ValueError("project name prefix must not be empty")
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def require_live_environment() -> LiveEnvironment:
    """Verify every live prerequisite and fail with an actionable report.

    All checks run even when an earlier one fails, so a single run reports every
    missing prerequisite. This is a hard failure, never a skip: a skipped live
    test would make an unverified Docker/GoNetEm path look green.
    """
    failures: list[str] = []

    checks: tuple[DoctorCheck, ...] = ()
    try:
        checks = tuple(doctor_checks(GONETEM_SERVER))
    except Exception as exc:  # noqa: BLE001 - report prerequisite failure
        failures.append(f"doctor checks could not run: {exc!r}")
    else:
        failures.extend(
            f"doctor check {check.name!r} failed: {check.detail}"
            for check in checks
            if not check.ok
        )

    docker_client: docker.DockerClient | None = None
    try:
        docker_client = docker.from_env(timeout=5)
        docker_client.ping()
    except Exception as exc:  # noqa: BLE001 - report prerequisite failure
        failures.append(f"Docker daemon ping failed: {exc}")

    version = ""
    try:
        channel = grpc.insecure_channel(GONETEM_SERVER)
        try:
            stub = netem_pb2_grpc.NetemStub(channel)
            response = stub.ServerGetVersion(empty_pb2.Empty(), timeout=5)
        finally:
            channel.close()
    except Exception as exc:  # noqa: BLE001 - report prerequisite failure
        failures.append(f"gRPC ServerGetVersion failed at {GONETEM_SERVER}: {exc}")
    else:
        if response.status.code == netem_pb2.StatusCode.OK:
            version = response.version
            if not version:
                failures.append("ServerGetVersion returned an empty version")
        else:
            failures.append(
                "ServerGetVersion returned status "
                f"{response.status.code}: {response.status.error}"
            )

    if docker_client is not None:
        for image in REQUIRED_IMAGES:
            try:
                docker_client.images.get(image)
            except Exception as exc:  # noqa: BLE001 - report prerequisite failure
                failures.append(f"required Docker image {image!r} is missing: {exc}")
    else:
        failures.append(
            "required Docker images could not be verified without Docker: "
            + ", ".join(REQUIRED_IMAGES)
        )

    for binary in ("ip", "tc"):
        if shutil.which(binary) is None:
            failures.append(f"required host binary {binary!r} is not on PATH")

    if failures:
        if docker_client is not None:
            docker_client.close()
        report = "\n".join(f"  - {failure}" for failure in failures)
        remediation_lines = [
            "  - Start Docker and make this user able to access the socket "
            "(usually membership in the 'docker' group).",
            f"  - Start a GoNetEm 0.9.x server listening on {GONETEM_SERVER}.",
            *(f"  - docker pull {image}" for image in REQUIRED_IMAGES),
            "  - Install iproute2 so 'ip' and 'tc' are on PATH.",
            "  - Re-run: uv run --frozen --extra dev pytest tests/integration "
            '-q -m "integration and not root and not slow"',
        ]
        remediation = "\n".join(remediation_lines)
        pytest.fail(
            "Live integration prerequisites are not satisfied:\n"
            f"{report}\n\nRemediation:\n{remediation}",
            pytrace=False,
        )

    assert docker_client is not None
    return LiveEnvironment(
        docker=docker_client,
        server_address=GONETEM_SERVER,
        server_version=version,
        required_images=REQUIRED_IMAGES,
        doctor_checks=checks,
    )


def project_containers(
    docker_client: docker.DockerClient, project_id: str
) -> list[str]:
    """Return containers named with the exact GoNetEm prefix for *project_id*.

    GoNetEm names containers ``ntm{project_id}.{node}``. A bare ``ntm`` match
    would return containers from every project on the host, so assertions and
    cleanup must use this exact prefix.
    """
    if not project_id:
        raise ValueError("project_id must not be empty")
    prefix = f"ntm{project_id}"
    names = [
        container.name
        for container in docker_client.containers.list(all=True)
        if container.name == prefix or container.name.startswith(prefix + ".")
    ]
    return sorted(names)


def wait_for_flow(flow: PingFlow, timeout: float) -> PingResults:
    """Wait for *flow* to finish before a monotonic deadline and return results.

    Raises:
        TimeoutError: The flow was still running at the deadline. The flow is
            stopped before raising.
        AssertionError: The flow ended in ``ERROR`` or was never started.
    """
    deadline = time.monotonic() + timeout
    while flow.status() == PingStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(_WAIT_POLL_SECONDS)

    status = flow.status()
    if status == PingStatus.RUNNING:
        stop_detail = ""
        try:
            flow.stop()
        except Exception as exc:  # noqa: BLE001 - include stop failure in report
            stop_detail = f"; failed to stop the flow: {exc!r}"
        raise TimeoutError(
            f"ping {flow.source.name} -> {flow.destination.name} did not finish "
            f"within {timeout:.1f}s{stop_detail}"
        )

    if status != PingStatus.DONE:
        detail = ""
        try:
            flow.results()
        except Exception as exc:  # noqa: BLE001 - surface the flow error
            detail = f": {exc}"
        raise AssertionError(
            f"ping {flow.source.name} -> {flow.destination.name} ended in state "
            f"{status.value}{detail}"
        )

    return flow.results()


def assert_ping(
    flow: PingFlow,
    *,
    timeout: float = 60.0,
    max_rtt_ms: float = 5000.0,
) -> PingResults:
    """Assert zero packet loss and a generous RTT sanity bound."""
    result = wait_for_flow(flow, timeout)
    label = f"{flow.source.name} -> {flow.destination.name}"
    assert result.packets_transmitted > 0, f"{label}: ping sent no packets"
    assert result.packets_received == result.packets_transmitted, (
        f"{label}: received {result.packets_received} of "
        f"{result.packets_transmitted} packets "
        f"({result.packet_loss_percent:.1f}% loss)"
    )
    assert result.packet_loss_percent == 0.0, (
        f"{label}: packet loss {result.packet_loss_percent:.1f}% is not zero"
    )
    assert 0.0 < result.rtt_avg_ms <= max_rtt_ms, (
        f"{label}: average RTT {result.rtt_avg_ms:.3f} ms is outside the sanity "
        f"bound (0, {max_rtt_ms:.0f}] ms"
    )
    return result


def teardown_live_topology(
    topology: Any,
    docker_client: docker.DockerClient,
    project_id: str | None,
) -> None:
    """Stop a live topology and verify no project-prefix containers remain.

    A graceful stop is attempted first, then ``force_stop_gonetem()`` when it
    fails. Regardless of the stop path, the exact ``ntm{project_id}`` prefix is
    swept and every cleanup failure is collected and raised, never swallowed.
    """
    errors: list[str] = []

    try:
        topology.stop_gonetem()
    except Exception as exc:  # noqa: BLE001 - force-close is the fallback
        errors.append(f"stop_gonetem failed: {exc!r}")
        try:
            topology.force_stop_gonetem()
        except Exception as force_exc:  # noqa: BLE001 - report both failures
            errors.append(f"force_stop_gonetem failed: {force_exc!r}")

    if not project_id:
        errors.append(
            "no GoNetEm project id was recorded; cannot verify project-prefix "
            "container cleanup"
        )
    else:
        try:
            leftovers = project_containers(docker_client, project_id)
        except Exception as exc:  # noqa: BLE001 - report Docker failure
            errors.append(f"could not list project containers: {exc!r}")
        else:
            if leftovers:
                errors.append(
                    "teardown left project container(s): " + ", ".join(leftovers)
                )
                for name in leftovers:
                    try:
                        docker_client.containers.get(name).remove(force=True)
                    except Exception as exc:  # noqa: BLE001 - collect every failure
                        errors.append(f"could not remove container {name}: {exc!r}")
                try:
                    remaining = project_containers(docker_client, project_id)
                except Exception as exc:  # noqa: BLE001 - report Docker failure
                    errors.append(f"could not re-list project containers: {exc!r}")
                else:
                    if remaining:
                        errors.append(
                            "project container(s) survived forced removal: "
                            + ", ".join(remaining)
                        )

    if errors:
        raise RuntimeError(
            "live topology teardown failed:\n"
            + "\n".join(f"  - {error}" for error in errors)
        )
