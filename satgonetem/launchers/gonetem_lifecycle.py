"""GoNetEm lifecycle and per-link operation orchestration."""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Optional

import grpc
from google.protobuf import empty_pb2

from satgonetem.launchers.gonetem_client import GoNetEmClientMixin
from satgonetem.proto import netem_pb2

LINK_UPDATE_CHUNK_SIZE = 512


class GoNetEmLifecycleMixin(GoNetEmClientMixin):
    """Start, wire, update, and close a GoNetEm project."""

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
                version_resp = client.ServerGetVersion(empty_pb2.Empty())
            except Exception as exc:
                raise RuntimeError(
                    f"GoNetEmLauncher: cannot reach server "
                    f"{self._server_address}: {exc}"
                ) from exc
            if version_resp.status.code == netem_pb2.StatusCode.ERROR:
                raise RuntimeError(
                    f"GoNetEmLauncher: ServerGetVersion failed: "
                    f"{version_resp.status.error}"
                )
            version = version_resp.version

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
            open_resp = client.ProjectOpen(open_req)
            if open_resp.status.code == netem_pb2.StatusCode.ERROR:
                raise RuntimeError(
                    f"GoNetEmLauncher: ProjectOpen failed: "
                    f"{open_resp.status.error}"
                )
            project_id = getattr(open_resp, "id", "")
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
        """Resolve container objects and PIDs for each node after TopologyRun."""
        nodes_by_name = {node.name: node for node in nodes}
        attached = 0

        for node_name, node in nodes_by_name.items():
            container_name = f"ntm{project_id}.{node_name}"
            try:
                container = self._container_runtime.get(container_name)
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
            self._close_transport()
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

    def update_links(self, links) -> netem_pb2.LinkUpdateManyResponse:
        """Update a burst of links with chunked LinkUpdateMany calls.

        Args:
            links: Links whose delay/rate/QoS should be refreshed.

        Returns:
            A single response with results in request order. When the burst is
            larger than :data:`LINK_UPDATE_CHUNK_SIZE`, per-chunk responses are
            concatenated and their ``updated`` counters are summed.

        Raises:
            RuntimeError: If the project is not open or a non-gRPC transport
                failure occurs.
            grpc.RpcError: Propagated unchanged so callers can detect an old
                server via ``UNIMPLEMENTED`` or fail on other transport errors.
        """
        if self._client is None or self._request is None:
            raise RuntimeError(
                "GoNetEmLauncher: update_links called before project is open"
            )

        links = list(links)
        merged = netem_pb2.LinkUpdateManyResponse(
            status=netem_pb2.Status(code=netem_pb2.OK)
        )
        if not links:
            return merged

        for start in range(0, len(links), LINK_UPDATE_CHUNK_SIZE):
            chunk = links[start : start + LINK_UPDATE_CHUNK_SIZE]
            request = netem_pb2.LinkUpdateManyRequest(
                prjId=self._request.id,
                links=[self._link_request(link).link for link in chunk],
                sync=False,
            )
            try:
                response = self._client.LinkUpdateMany(request)
            except grpc.RpcError:
                # Preserve the status code for the caller's fallback logic.
                raise
            except Exception as exc:
                raise RuntimeError(
                    "GoNetEmLauncher: LinkUpdateMany failed for "
                    f"{len(chunk)} link(s): {exc}"
                ) from exc

            if len(response.results) != len(chunk):
                raise RuntimeError(
                    "GoNetEmLauncher: LinkUpdateMany returned "
                    f"{len(response.results)} result(s) for {len(chunk)} link(s)"
                )

            merged.status.CopyFrom(response.status)
            merged.results.extend(response.results)
            merged.updated += response.updated

        return merged

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
            if not getattr(link, "use_budget", False):
                link_type = getattr(link, "type", "")
                if link_type == "GroundStationLink":
                    capacity = gnd_kbps
                elif link_type == "GroundObjectLink":
                    capacity = self.ground_object_capacity_kbps
                else:
                    capacity = isl_kbps

                link.default_capacity_kbps = capacity
                link.peer1_capacity = capacity
                link.peer2_capacity = capacity

            self.update_link(link)
