"""Synchronous iperf3 runner."""

from __future__ import annotations

import contextlib
import shlex
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_io import (
    _extract_iperf3_json,
    _wait_for_iperf3_server,
)
from satgonetem.traffic.iperf3_ports import (
    _IPERF3_RESULT_POLL_INTERVAL,
    _IPERF3_RESULT_WAIT_TIMEOUT,
)
from satgonetem.traffic.iperf3_results import Iperf3Results

if TYPE_CHECKING:
    from satgonetem.models.node import Node


def _read_temp_file(node: Node, path: str) -> str | None:
    """Read a temporary file inside a node, returning None on failure.

    Args:
        node: Node to read from.
        path: Container path of the file.

    Returns:
        The file content, or None when the command failed or returned no text.
    """
    try:
        output = node.execute_command(f"cat {shlex.quote(path)}")
    except Exception:
        return None
    return output if isinstance(output, str) else None


def _read_iperf3_json(node: Node, path: str) -> str | None:
    """Read and extract the JSON object iperf3 wrote inside a node.

    iperf3 redirects warnings to the same file as its JSON output, so the
    object must be isolated from surrounding text before parsing.

    Args:
        node: Node to read from.
        path: Container path of the JSON file.

    Returns:
        The JSON object as text, or None when it could not be read.
    """
    return _extract_iperf3_json(_read_temp_file(node, path))


def _wait_for_iperf3_json(
    node: Node, path: str, timeout_s: float | None = None
) -> str | None:
    """Poll for an iperf3 JSON file until it is complete or the wait expires."""
    timeout = _IPERF3_RESULT_WAIT_TIMEOUT if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout
    while True:
        raw = _read_iperf3_json(node, path)
        if raw is not None or time.monotonic() >= deadline:
            return raw
        time.sleep(_IPERF3_RESULT_POLL_INTERVAL)


def _remove_temp_file(node: Node, path: str) -> None:
    """Best-effort removal of a temporary JSON file inside a node.

    Cleanup failures must never change the run outcome, so every error is
    suppressed.

    Args:
        node: Node that holds the file.
        path: Container path of the file.
    """
    with contextlib.suppress(Exception):
        node.execute_command(f"rm -f {shlex.quote(path)}", detach=True)


def _kill_server(node: Node, dst_ip: str, port: int) -> None:
    """Best-effort kill of this run's detached one-shot iperf3 server.

    The pattern is scoped to the run's bind address and allocated port so
    servers belonging to other runs are left alone. It starts with the
    bracket expression ``[i]perf3`` so ``pkill -f`` cannot match the wrapper
    ``sh -c`` command line that carries the pattern: the expression matches
    the literal ``iperf3`` in the real server's argv, while the wrapper's
    literal text ``[i]perf3`` does not match it. Without the bracket, pkill
    would signal its own parent shell before ``; true`` runs. Errors are
    suppressed.

    Args:
        node: Node that runs the server.
        dst_ip: Address the server is bound to.
        port: Port the server is listening on.
    """
    pattern = f"[i]perf3 -s -1 -B {dst_ip} -p {port}"
    with contextlib.suppress(Exception):
        node.execute_command(f"pkill -f {shlex.quote(pattern)} 2>/dev/null; true")


def run_iperf3_flow(
    source: Node,
    destination: Node,
    config: Iperf3Config,
) -> Iperf3Results:
    """Launch an iperf3 flow between two nodes and return parsed results.

    Starts a one-shot iperf3 server on destination bound to
    destination.loopback.ipv4, then runs the iperf3 client on source bound
    to source.loopback.ipv4 (-B flag). The client runs synchronously; this
    function blocks until it completes.

    The server exits automatically after the first client connection (-1).
    An unused server port is reserved for the run, so concurrent runs cannot
    collide; the supplied config.port is not modified. Every exit path -
    success or failure - kills the detached server and removes both temporary
    JSON files, so a failed run cannot hold the port or leak files.

    Args:
        source: Node that runs the iperf3 client.
        destination: Node that runs the iperf3 server.
        config: Full iperf3 configuration for this run.

    Returns:
        Iperf3Results populated with the parsed JSON output. The JSON is read
        from the endpoint that owns the metrics: UDP metrics (jitter, packet
        loss, out-of-order) are reported by the receiver, and TCP metrics
        (RTT, cwnd, retransmits) by the sender. ``config.reverse`` swaps those
        endpoints, so UDP reverse prefers the client JSON and TCP reverse
        prefers the server JSON. The other side is used as a fallback.
        Bidirectional runs always prefer the client JSON because it contains
        both direction streams. Text iperf3 prints around its JSON object
        (e.g. warnings) is skipped.

    Raises:
        ValueError: If the retrieved JSON is empty or unparseable.
        RuntimeError: If neither client nor server JSON could be retrieved
            from the containers.
    """
    run_id = uuid.uuid4().hex[:8]
    client_json = f"/tmp/iperf3_cli_{source.name}_{destination.name}_{run_id}.json"
    server_json = f"/tmp/iperf3_srv_{source.name}_{destination.name}_{run_id}.json"

    dst_ip = destination.loopback.ipv4
    src_ip = source.loopback.ipv4

    # Reserve an exclusive port for this run; config.port is not modified.
    port = iperf3_ports._allocate_port()
    run_config = replace(config, port=port)
    try:
        server_cmd = (
            f'sh -c "iperf3 -s -1 -B {dst_ip} -p {port} --json > {server_json} 2>&1"'
        )
        destination.execute_command(server_cmd, detach=True)
        if not _wait_for_iperf3_server(destination, dst_ip, port):
            raise RuntimeError(
                f"iperf3 server did not start on {dst_ip}:{port}; "
                f"client {source.name} -> {destination.name} was not started."
            )

        client_cmd = run_config.build_client_command(dst_ip, src_ip, client_json)
        source.execute_command(client_cmd, detach=False)

        raw: str | None
        if run_config.bidir:
            # The client JSON contains both direction streams; the server
            # prints before exchanging results, so its reverse summary is
            # empty (same rule as Iperf3Flow._json_side).
            raw = _wait_for_iperf3_json(source, client_json)
            if raw is None:
                raw = _wait_for_iperf3_json(destination, server_json)
        else:
            # UDP receiver metrics live on the receiving endpoint and TCP
            # sender metrics on the sending one; --reverse swaps those
            # endpoints (same rule as Iperf3Flow._json_side). The preferred
            # file may appear after the client exits, so poll it before
            # accepting the other side.
            is_udp = run_config.protocol.upper() == "UDP"
            use_source = run_config.reverse if is_udp else not run_config.reverse
            if use_source:
                preferred_node, preferred_json = source, client_json
                fallback_node, fallback_json = destination, server_json
            else:
                preferred_node, preferred_json = destination, server_json
                fallback_node, fallback_json = source, client_json
            raw = _wait_for_iperf3_json(preferred_node, preferred_json)
            if raw is None:
                raw = _wait_for_iperf3_json(fallback_node, fallback_json)

        if raw is None:
            raise RuntimeError(
                f"Could not retrieve iperf3 JSON output for flow "
                f"{source.name} -> {destination.name}. "
                "Verify iperf3 is installed in both containers and a route exists."
            )

        return Iperf3Results(raw_json=raw, config=run_config)
    finally:
        _kill_server(destination, dst_ip, port)
        _remove_temp_file(source, client_json)
        _remove_temp_file(destination, server_json)
        iperf3_ports._release_port(port)
