"""
GoNetem gRPC-based network launcher.

Delegates all container/link management to a running GoNetem server via gRPC.
Uses the synchronous grpc channel so every method is blocking and thread-safe (no sense doing async, thought it through).

Also owns project file generation (get_gonetem_topology / generate_project)
since it is the only launcher that needs the GoNetem .gnet format.
"""

import logging
import os
import tarfile
import subprocess
import time
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
        ground_object_capacity_kbps: int = 100_000,
        temp_path: str = "/tmp/",
    ) -> None:
        super().__init__(
            project_name,
            isl_capacity_kbps,
            gnd_capacity_kbps,
            ground_object_capacity_kbps,
        )
        self._topology_manager = topology_manager
        self._server_address = server_address
        self._temp_path = temp_path

        self._channel: Optional[grpc.Channel] = None
        self._client: Optional[netem_grpc.NetemStub] = None
        self._request: Optional[netem_pb2.ProjectRequest] = None

    # Project file generation

    def get_gonetem_topology(
        self,
        fallback: bool = False,
        controller: bool = False,
        nodes: Optional[list] = None,
        links: Optional[list] = None,
    ) -> str:
        """Build the GoNetem network.yml content from the current topology.

        Args:
            fallback: Use host networking instead of the Docker images.
            controller: Add the controller switch and its links.
            nodes: Optional filtered node set. When provided, only these
                satellites/ground stations (and links between them) are
                serialized. HIL-managed ground stations are excluded by the
                caller.
            links: Optional filtered link set. When omitted, all topology
                links between the selected nodes are used.
        """
        tm = self._topology_manager
        sat_type = "docker.host" if fallback else "docker.satellite"
        gs_type = (
            "docker.host" if fallback else "docker.satellite"
        )  # Borken if using gndstation TODO fix

        if nodes is None:
            satellite_nodes = list(tm.satellites.values())
            ground_station_nodes = list(tm.ground_stations.values())
            allowed_names = None
        else:
            allowed_names = {node.name for node in nodes}
            satellite_nodes = [
                sat for sat in tm.satellites.values() if sat.name in allowed_names
            ]
            ground_station_nodes = [
                gs for gs in tm.ground_stations.values() if gs.name in allowed_names
            ]

        if links is None:
            link_nodes = tm.links.values()
        else:
            link_nodes = links
        if allowed_names is not None:
            link_nodes = [
                link
                for link in link_nodes
                if link.source.name in allowed_names
                and link.target.name in allowed_names
            ]

        satellites = self._write_satellites(satellite_nodes, sat_type)
        groundStations = self._write_ground_stations(
            ground_station_nodes, gs_type
        )
        controller_section = self._write_controller_switch() if controller else ""
        link_section = self._write_links(link_nodes)
        controller_links = (
            self._write_controller_switch_links(
                satellite_nodes, ground_station_nodes
            )
            if controller
            else ""
        )

        return (
            "nodes: \n"
            + satellites
            + groundStations
            + controller_section
            + "links: \n"
            + link_section
            + controller_links
        )

    @staticmethod
    def _write_controller_switch_links(satellites, ground_stations) -> str:
        # Link from every node to the controller switch with high capacity and low latency
        result = ""
        for sat in satellites:
            eth_id = 50000 + sat.id
            result += (
                f" - {{peer1: Controller.{sat.id}, peer2: {sat.name}.{eth_id},"
                f" peer1qos: {{ rate: 1000000, buffer: 10000, jitter: 0, delay: 1}},"
                f" peer2qos: {{ rate: 1000000, buffer: 10000, jitter: 0, delay: 1}}}} \n"
            )
        for gs in ground_stations:
            eth_id = 50000 + gs.id
            result += (
                f" - {{peer1: Controller.{gs.id}, peer2: {gs.name}.{eth_id},"
                f" peer1qos: {{ rate: 1000000, buffer: 10000, jitter: 0, delay: 1}},"
                f" peer2qos: {{ rate: 1000000, buffer: 10000, jitter: 0, delay: 1}}}} \n"
            )
        return result

    @staticmethod
    def _write_controller_switch() -> str:
        return " Controller:\n  type: ovs\n  volumes:\n  - /tmp:/tmp\n"

    @staticmethod
    def _write_satellites(satellites, sat_type: str) -> str:
        result = ""
        for sat in satellites:
            image = "jariassuarez/sgnt:satellite" if not sat.image else sat.image
            result += f" Sat{sat.id}:\n  type: {sat_type}\n  image: {image}\n  volumes:\n  - /tmp:/tmp\n"
        return result

    @staticmethod
    def _write_ground_stations(ground_stations, gs_type: str) -> str:
        result = ""
        for gs in ground_stations:
            prefix = "Gnd" if gs.type == "GroundStation" else "Usr"
            result += f" {prefix}{gs.id}:\n  type: {gs_type}\n  image: {gs.image}\n  volumes:\n  - /tmp:/tmp\n"
        return result

    @staticmethod
    def _write_links(links) -> str:
        result = ""
        for link in links:
            delay = max(int(link.delay), 1)
            rate = link.get_capacity()
            burst = rate * 5
            result += (
                f" - {{peer1: {link.source.name}.{link.target.id},"
                f" peer2: {link.target.name}.{link.source.id},"
                f" peer1qos: {{ rate: {rate}, buffer: 10, jitter: 0, delay: {delay}, burst: {burst}}},"
                f" peer2qos: {{ rate: {rate}, buffer: 10, jitter: 0, delay: {delay}, burst: {burst}}}}}"
                f" #Latency of {delay}ms \n"
            )
        return result

    def generate_project(
        self,
        dest_dir: str,
        controller: bool = False,
        nodes: Optional[list] = None,
        links: Optional[list] = None,
    ) -> str:
        """Write a project.gnet archive to *dest_dir* and return its path.

        Args:
            dest_dir: Directory for the generated archive.
            controller: Add the controller switch and its links.
            nodes: Optional filtered node set passed to
                :meth:`get_gonetem_topology`.
            links: Optional filtered link set passed to
                :meth:`get_gonetem_topology`.
        """
        import tempfile

        data = self.get_gonetem_topology(
            controller=controller, nodes=nodes, links=links
        )
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
        link_type = getattr(link, "type", "")
        if link_type == "GroundStationLink":
            default = self.gnd_capacity_kbps
        elif link_type == "GroundObjectLink":
            default = self.ground_object_capacity_kbps
        else:
            default = self.isl_capacity_kbps
        return (p1 or fb or default), (p2 or fb or default)

    # Lifecycle

    def start_containers(
        self,
        nodes: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
        controller: bool = False,
        links: Optional[list] = None,
    ) -> tuple[float, float]:
        """Open the project on the GoNetem server and run TopologyRun.

        GoNetem starts containers and wires links in a single streaming call,
        so start_containers handles both NODE_START and LINK_SETUP progress.
        wire_links() is therefore a no-op for this launcher.

        Args:
            nodes: Nodes to launch. HIL-managed nodes must be excluded.
            workers: Unused; GoNetEm manages its own concurrency.
            progress_cb: Optional progress callback.
            controller: Add the controller switch and its links.
            links: Links to create. HIL-managed links must be excluded.

        Returns:
            A tuple of (container_time, link_time) in seconds, where
            container_time is the elapsed time across all NODE_START events
            and link_time is the elapsed time across all LINK_SETUP events.
        """
        project_file = None
        try:
            client = self._get_client()

            try:
                version = client.ServerGetVersion(empty_pb2.Empty()).version
            except Exception as exc:
                raise RuntimeError(
                    f"GoNetEmLauncher: cannot reach server "
                    f"{self._server_address}: {exc}"
                ) from exc

            logging.info(
                "GoNetEmLauncher: connected to GoNetem %s at %s",
                version,
                self._server_address,
            )

            project_file = self.generate_project(
                self._temp_path,
                controller=controller,
                nodes=nodes,
                links=links,
            )
            with open(project_file, "rb") as fd:
                open_req = netem_pb2.OpenRequest(
                    name=self.project_name, data=fd.read()
                )
            project_id = getattr(client.ProjectOpen(open_req), "id", "")
            if not project_id:
                raise RuntimeError(
                    "GoNetEmLauncher: ProjectOpen returned an empty project id"
                )
            self._request = netem_pb2.ProjectRequest(id=project_id)
            logging.info("GoNetEmLauncher: project opened with id=%s", project_id)

            node_count = link_count = node_start = link_setup = node_config = 0
            container_start_t = link_start_t = None
            container_time = link_time = 0.0
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
                        if container_start_t is None:
                            container_start_t = time.monotonic()
                        node_start += 1
                        container_time = time.monotonic() - container_start_t
                        if progress_cb:
                            progress_cb("NODE_START", node_start, node_count)
                    elif code == netem_pb2.TopologyRunMsg.LINK_SETUP:
                        if link_start_t is None:
                            link_start_t = time.monotonic()
                        link_setup += 1
                        link_time = time.monotonic() - link_start_t
                        if progress_cb:
                            progress_cb("LINK_SETUP", link_setup, link_count)
                    elif code == netem_pb2.TopologyRunMsg.NODE_LOADCONFIG:
                        node_config += 1
                        if progress_cb:
                            progress_cb("NODE_LOADCONFIG", node_config, node_count)
            except Exception as exc:
                raise RuntimeError(
                    f"GoNetEmLauncher: TopologyRun failed: {exc}"
                ) from exc

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

            return (container_time, link_time)
        except Exception:
            self._cleanup_failed_start()
            raise
        finally:
            if project_file is not None:
                try:
                    os.unlink(project_file)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logging.warning(
                        "GoNetEmLauncher: could not remove project file %s: %s",
                        project_file,
                        exc,
                    )

    def _close_transport(self) -> None:
        """Close the gRPC channel and clear client state."""
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception as exc:
                logging.warning(
                    "GoNetEmLauncher: failed to close gRPC channel: %s", exc
                )
        self._channel = None
        self._client = None

    def _cleanup_failed_start(self) -> None:
        """Best-effort cleanup after a failed start_containers() call."""
        try:
            if self._client is not None and self._request is not None:
                self.force_close_project()
        except Exception as exc:
            logging.error(
                "GoNetEmLauncher: cleanup after failed start failed: %s", exc
            )
        finally:
            self._close_transport()
            self._request = None

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

        # Attach gRPC client and project ID so nodes can use NodeRun
        for node in nodes_by_name.values():
            node.grpc_client = self._client
            node.project_id = project_id

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

    @staticmethod
    def _parse_project_containers(stdout: str, project_id: str) -> list[str]:
        """Return container IDs belonging to *project_id*.

        GoNetEm names containers ``ntm{project_id}.{node}``. Docker's
        ``name`` filter is a substring match, so filtering on ``ntm`` alone
        also matches every other project on the host. Match the exact project
        prefix instead.

        Args:
            stdout: Output of ``docker ps --format '{{.ID}}\t{{.Names}}'``.
            project_id: GoNetEm project identifier.

        Returns:
            IDs of containers scoped to the project.
        """
        prefix = f"ntm{project_id}"
        container_ids: list[str] = []
        for line in stdout.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) != 2:
                continue
            container_id, name = parts
            if name == prefix or name.startswith(prefix + "."):
                container_ids.append(container_id)
        return container_ids

    @staticmethod
    def _run_docker_command(command: list[str]) -> bool:
        """Run a Docker cleanup command and report whether it succeeded."""
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            logging.error(
                "GoNetEmLauncher: failed to run %s: %s", " ".join(command), exc
            )
            return False
        if result.returncode != 0:
            logging.error(
                "GoNetEmLauncher: %s failed (exit %s): %s",
                " ".join(command),
                result.returncode,
                (result.stderr or "").strip(),
            )
            return False
        return True

    def _list_project_containers(self, project_id: str) -> list[str]:
        """List Docker containers scoped to *project_id*."""
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}"],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            logging.error("GoNetEmLauncher: could not list Docker containers: %s", exc)
            return []
        if result.returncode != 0:
            logging.error(
                "GoNetEmLauncher: docker ps failed (exit %s): %s",
                result.returncode,
                (result.stderr or "").strip(),
            )
            return []
        return self._parse_project_containers(result.stdout, project_id)

    def force_close_project(self) -> None:
        """Forcefully close the project without waiting for progress messages.

        Only containers whose names belong to this project are stopped and
        removed. If the project id is unavailable, container cleanup is
        skipped rather than falling back to a host-wide match.
        """
        if self._client is None or self._request is None:
            return
        project_id = str(getattr(self._request, "id", "") or "")
        try:
            if not project_id:
                logging.warning(
                    "GoNetEmLauncher: project id is empty; skipping Docker cleanup"
                )
            else:
                container_ids = self._list_project_containers(project_id)
                for cid in container_ids:
                    self._run_docker_command(["docker", "stop", "-t", "0", cid])
                for cid in container_ids:
                    self._run_docker_command(["docker", "rm", cid])
                self._client.ProjectClose(self._request)
        except Exception as exc:
            logging.error("GoNetEmLauncher: force_close_project failed: %s", exc)
        finally:
            self._close_transport()
