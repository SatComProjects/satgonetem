"""Synchronous iperf3 runner."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from satgonetem.traffic import iperf3_ports
from satgonetem.traffic.iperf3_config import Iperf3Config
from satgonetem.traffic.iperf3_io import _wait_for_iperf3_server
from satgonetem.traffic.iperf3_results import Iperf3Results

if TYPE_CHECKING:
    from satgonetem.models.node import Node


def run_iperf3_flow(
    source: "Node",
    destination: "Node",
    config: Iperf3Config,
) -> Iperf3Results:
    """Launch an iperf3 flow between two nodes and return parsed results.

    Starts a one-shot iperf3 server on destination bound to
    destination.loopback.ipv4, then runs the iperf3 client on source bound
    to source.loopback.ipv4 (-B flag). The client runs synchronously; this
    function blocks until it completes.

    The server exits automatically after the first client connection (-1).
    An unused server port is reserved for the run, so concurrent runs cannot
    collide; the supplied config.port is not modified. Temporary JSON files
    written inside the containers are cleaned up after the results are
    retrieved.

    Args:
        source: Node that runs the iperf3 client.
        destination: Node that runs the iperf3 server.
        config: Full iperf3 configuration for this run.

    Returns:
        Iperf3Results populated with the parsed JSON output. For UDP flows,
        the server-side JSON is used because receiver metrics (jitter, packet
        loss, out-of-order) are only reported on the receiving end. For TCP,
        the client-side JSON is used, with the server as fallback.

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
    port = iperf3_ports._IPERF3_PORT_ALLOCATOR.allocate()
    run_config = replace(config, port=port)
    try:
        server_cmd = (
            f'sh -c "iperf3 -s -1 -B {dst_ip} -p {port} '
            f'--json > {server_json} 2>&1"'
        )
        destination.execute_command(server_cmd, detach=True)
        _wait_for_iperf3_server(destination, dst_ip, port)

        client_cmd = run_config.build_client_command(
            dst_ip, src_ip, client_json
        )
        source.execute_command(client_cmd, detach=False)
        time.sleep(0.3)

        if run_config.protocol.upper() == "UDP":
            raw: Any = destination.execute_command(f"cat {server_json}")
            if not isinstance(raw, str) or not raw.strip().startswith("{"):
                raw = source.execute_command(f"cat {client_json}")
        else:
            raw = source.execute_command(f"cat {client_json}")
            if not isinstance(raw, str) or not raw.strip().startswith("{"):
                raw = destination.execute_command(f"cat {server_json}")

        source.execute_command(f"rm -f {client_json}", detach=True)
        destination.execute_command(f"rm -f {server_json}", detach=True)

        if not isinstance(raw, str) or not raw.strip().startswith("{"):
            raise RuntimeError(
                f"Could not retrieve iperf3 JSON output for flow "
                f"{source.name} -> {destination.name}. "
                "Verify iperf3 is installed in both containers and a route exists."
            )

        return Iperf3Results(raw_json=raw, config=run_config)
    finally:
        iperf3_ports._IPERF3_PORT_ALLOCATOR.release(port)
