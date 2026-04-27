"""
GoNetem gRPC-based network launcher.

Delegates all container/link management to a running GoNetem server via gRPC.
Uses the synchronous grpc channel so every method is blocking and thread-safe (no sense doing async, thought it through).

Also owns project file generation (get_gonetem_topology / generate_project)
since it is the only launcher that needs the GoNetem .gnet format.
"""

import logging
import os
import re
import sys
import tarfile
import subprocess
from typing import Callable, Optional

import grpc
from google.protobuf import empty_pb2

from satgonetem.proto import netem_pb2
import satgonetem.proto.netem_pb2_grpc as netem_grpc
from satgonetem.launchers.base_launcher import NetworkLauncher


class GoNetEmLauncher(NetworkLauncher):
    """Launch and manage a network topology through a GoNetem gRPC server."""

    def __init__(
        self,
        topology_manager,
        server_address: str,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        temp_path: str = "/tmp/",
    ) -> None:
        super().__init__(project_name, isl_capacity_kbps, gnd_capacity_kbps)
        self._topology_manager = topology_manager
        self._server_address = server_address
        self._temp_path = temp_path

        self._channel: Optional[grpc.Channel] = None
        self._client: Optional[netem_grpc.NetemStub] = None
        self._request: Optional[netem_pb2.ProjectRequest] = None

    # Project file generation

    def get_gonetem_topology(self, fallback: bool = False) -> str:
        """Build the GoNetem network.yml content from the current topology."""
        tm = self._topology_manager
        sat_type = "docker.host" if fallback else "docker.satellite"
        gs_type = (
            "docker.host" if fallback else "docker.satellite"
        )  # Borken if using gndstation TODO fix

        satellites = self._write_satellites(tm.satellites.values(), sat_type)
        groundStations = self._write_ground_stations(
            tm.ground_stations.values(), gs_type
        )
        links = self._write_links(tm.links.values())

        return "nodes: \n" + satellites + groundStations + "links: \n" + links

    @staticmethod
    def _write_satellites_old(satellites, sat_type: str) -> str:
        result = ""
        for sat in satellites:
            result += f" Sat{sat.id}:\n  type: {sat_type}\n  volumes:\n  - /tmp:/tmp\n"
        return result

    @staticmethod
    def _write_satellites(satellites, sat_type: str) -> str:
        result = ""
        for sat in satellites:
            result += f" Sat{sat.id}:\n  type: docker.host\n  image: jariassuarez/sgnt:satellite\n  volumes:\n  - /tmp:/tmp\n"
        return result

    @staticmethod
    def _write_ground_stations(ground_stations, gs_type: str) -> str:
        result = ""
        for gs in ground_stations:
            prefix = "Gnd" if gs.type == "GroundStation" else "Usr"
            result += (
                f" {prefix}{gs.id}:\n  type: {gs_type}\n  volumes:\n  - /tmp:/tmp\n"
            )
        return result

    @staticmethod
    def _write_links(links) -> str:
        result = ""
        for link in links:
            delay = max(int(link.delay), 1)
            rate = link.get_capacity()
            result += (
                f" - {{peer1: {link.source.name}.{link.target.id},"
                f" peer2: {link.target.name}.{link.source.id},"
                f" rate: {rate}, buffer: 1, jitter: 0, delay: {delay}}}"
                f" #Latency of {delay}ms \n"
            )
        return result

    def generate_project(self, dest_dir: str) -> str:
        """Write a project.gnet archive to *dest_dir* and return its path."""
        import tempfile

        data = self.get_gonetem_topology()
        with tempfile.TemporaryDirectory() as tmp:
            network_yml = os.path.join(tmp, "network.yml")
            with open(network_yml, "w") as fd:
                fd.write(data)
            fd, gnet_path = tempfile.mkstemp(suffix=".gnet", dir=dest_dir)
            os.close(fd)
            with tarfile.open(gnet_path, "w:gz") as tar:
                tar.add(network_yml, arcname="network.yml")
        return gnet_path

    # Internal helpers

    def _get_client(self) -> netem_grpc.NetemStub:
        if self._client is None:
            # NOTE: insecure_channel is intentional. GoNetem is expected to run
            # on the same trusted localhost; TLS can be enabled if the server is
            # exposed to an untrusted network.
            self._channel = grpc.insecure_channel(self._server_address)
            self._client = netem_grpc.NetemStub(self._channel)
        return self._client

    def _link_request(self, link) -> netem_pb2.LinkRequest:
        delay_ms = max(int(link.delay), 1)
        loss = float(getattr(link, "loss", 0) or 0)
        jitter = int(getattr(link, "jitter", 0) or 0)
        p1_kbps, p2_kbps = self._link_capacities_for(link)
        qos1 = netem_pb2.LinkConfig.QoSConfig(
            delay=delay_ms, rate=p1_kbps, loss=loss, jitter=jitter
        )
        qos2 = netem_pb2.LinkConfig.QoSConfig(
            delay=delay_ms, rate=p2_kbps, loss=loss, jitter=jitter
        )
        link_cfg = netem_pb2.LinkConfig(
            peer1=f"{link.source.name}.{link.target.id}",
            peer2=f"{link.target.name}.{link.source.id}",
            peer1qos=qos1,
            peer2qos=qos2,
        )
        return netem_pb2.LinkRequest(prjId=self._request.id, link=link_cfg)

    def _link_capacities_for(self, link) -> tuple[int, int]:
        p1 = int(getattr(link, "peer1_capacity", 0) or 0)
        p2 = int(getattr(link, "peer2_capacity", 0) or 0)
        fb = int(getattr(link, "capacity", 0) or 0)
        default = (
            self.gnd_capacity_kbps
            if getattr(link, "type", "") == "GroundStationLink"
            else self.isl_capacity_kbps
        )
        return (p1 or fb or default), (p2 or fb or default)

    # Lifecycle

    def start_containers(
        self,
        nodes: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Open the project on the GoNetem server and run TopologyRun.

        GoNetem starts containers and wires links in a single streaming call,
        so start_containers handles both NODE_START and LINK_SETUP progress.
        wire_links() is therefore a no-op for this launcher.
        """
        client = self._get_client()

        try:
            version = client.ServerGetVersion(empty_pb2.Empty()).version
            logging.info(
                "GoNetEmLauncher: connected to GoNetem %s at %s",
                version,
                self._server_address,
            )
        except Exception as exc:
            logging.error(
                "GoNetEmLauncher: cannot reach server %s: %s", self._server_address, exc
            )
            sys.exit(1)

        project_file = self.generate_project(self._temp_path)
        with open(project_file, "rb") as fd:
            open_req = netem_pb2.OpenRequest(name=self.project_name, data=fd.read())
        project_id = client.ProjectOpen(open_req).id
        self._request = netem_pb2.ProjectRequest(id=project_id)
        logging.info("GoNetEmLauncher: project opened with id=%s", project_id)

        node_count = link_count = node_start = link_setup = node_config = 0
        try:
            for msg in client.TopologyRun(self._request):
                code = msg.code
                if code == netem_pb2.TopologyRunMsg.NODE_COUNT:
                    node_count = msg.total
                    if progress_cb:
                        progress_cb("NODE_COUNT", 0, node_count)
                elif code == netem_pb2.TopologyRunMsg.LINK_COUNT:
                    link_count = msg.total
                    if progress_cb:
                        progress_cb("LINK_COUNT", 0, link_count)
                elif code == netem_pb2.TopologyRunMsg.NODE_START:
                    node_start += 1
                    if progress_cb:
                        progress_cb("NODE_START", node_start, node_count)
                elif code == netem_pb2.TopologyRunMsg.LINK_SETUP:
                    link_setup += 1
                    if progress_cb:
                        progress_cb("LINK_SETUP", link_setup, link_count)
                elif code == netem_pb2.TopologyRunMsg.NODE_LOADCONFIG:
                    node_config += 1
                    if progress_cb:
                        progress_cb("NODE_LOADCONFIG", node_config, node_count)
        except Exception as exc:
            logging.error("GoNetEmLauncher: TopologyRun failed: %s", exc)
            sys.exit(1)

        logging.info(
            "GoNetEmLauncher: topology running (%d nodes, %d links)",
            node_count,
            link_count,
        )
        if progress_cb:
            progress_cb("COMPLETED", 0, 0)

        # Expose client/request on topology_manager for callers that need them
        self._topology_manager.project_id = project_id
        self._topology_manager.client = client
        self._topology_manager.request = self._request

        # Attach docker-py container objects and PIDs to each node so that
        # subsequent operations (exec_run, enable_mpls, FRR init, etc.) work.
        self._attach_containers(nodes, project_id)

    def _attach_containers(self, nodes: list, project_id: str) -> None:
        """Resolve docker-py Container objects and PIDs for each node after TopologyRun."""
        import docker as docker_sdk

        try:
            docker_client = docker_sdk.from_env()
        except Exception as exc:
            logging.warning("GoNetEmLauncher: could not create docker client: %s", exc)
            return

        nodes_by_name = {node.name: node for node in nodes}
        attached = 0

        for node_name, node in nodes_by_name.items():
            container_name = f"ntm{project_id}.{node_name}"
            try:
                container = docker_client.containers.get(container_name)
                node.container = container
                pid = container.attrs.get("State", {}).get("Pid")
                if pid:
                    node.container_pid = int(pid)
                attached += 1
            except Exception as exc:
                logging.warning(
                    "GoNetEmLauncher: could not attach container for %s: %s",
                    node_name,
                    exc,
                )

        logging.info(
            "GoNetEmLauncher: attached %d/%d containers.", attached, len(nodes_by_name)
        )

    def wire_links(
        self,
        links: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """No-op: GoNetem already wired links during TopologyRun."""

    def close_project(
        self,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        if self._client is None or self._request is None:
            return
        node_count = node_close = 0
        try:
            for msg in self._client.ProjectClose(self._request):
                if msg.code == netem_pb2.ProjectCloseMsg.NODE_COUNT:
                    node_count = msg.total
                    if progress_cb:
                        progress_cb("CLOSE_NODE_COUNT", 0, node_count)
                elif msg.code == netem_pb2.ProjectCloseMsg.NODE_CLOSE:
                    node_close += 1
                    if progress_cb:
                        progress_cb("CLOSE_NODE_CLOSE", node_close, node_count)
        except Exception as exc:
            logging.error("GoNetEmLauncher: ProjectClose failed: %s", exc)
            if progress_cb:
                progress_cb("CLOSE_ERROR", 0, 0)
            raise
        finally:
            if self._channel:
                self._channel.close()
                self._channel = None
                self._client = None
            if progress_cb:
                progress_cb("CLOSE_COMPLETED", node_close, node_count)

    # Per-step link management

    def update_link(self, link) -> None:
        if self._client is None or self._request is None:
            logging.warning(
                "GoNetEmLauncher: update_link called before project is open"
            )
            return
        try:
            self._client.LinkUpdate(self._link_request(link))
        except Exception as exc:
            logging.error(
                "GoNetEmLauncher: LinkUpdate failed for %s<->%s: %s",
                link.source.name,
                link.target.name,
                exc,
            )

    def add_link(self, link) -> None:
        if self._client is None or self._request is None:
            logging.warning("GoNetEmLauncher: add_link called before project is open")
            return
        try:
            self._client.LinkAdd(self._link_request(link))
        except Exception as exc:
            logging.error(
                "GoNetEmLauncher: LinkAdd failed for %s<->%s: %s",
                link.source.name,
                link.target.name,
                exc,
            )

    def delete_link(self, link) -> None:
        if self._client is None or self._request is None:
            logging.warning(
                "GoNetEmLauncher: delete_link called before project is open"
            )
            return
        try:
            self._client.LinkDel(self._link_request(link))
        except Exception as exc:
            logging.error(
                "GoNetEmLauncher: LinkDel failed for %s<->%s: %s",
                link.source.name,
                link.target.name,
                exc,
            )

    def set_link_capacities(self, isl_kbps: int, gnd_kbps: int, links: list) -> None:
        self.isl_capacity_kbps = isl_kbps
        self.gnd_capacity_kbps = gnd_kbps
        for link in links:
            self.update_link(link)

    def force_close_project(self) -> None:
        """Forcefully close the project without waiting for progress messages."""
        if self._client is None or self._request is None:
            return
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "-q", "--filter", "name=ntm"],
                capture_output=True,
                text=True,
            )
            container_ids = result.stdout.strip().split()
            for cid in container_ids:
                subprocess.run(
                    ["docker", "stop", "-t", "0", cid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            for cid in container_ids:
                subprocess.run(
                    ["docker", "rm", cid],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            self._client.ProjectClose(self._request)
        except Exception as exc:
            logging.error("GoNetEmLauncher: force_close_project failed: %s", exc)
        finally:
            if self._channel:
                self._channel.close()
                self._channel = None
                self._client = None
