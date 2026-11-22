"""GoNetEm lifecycle and per-link operation orchestration."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

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
        progress_cb: Callable[[str, int, int], None] | None = None,
        controller: bool = False,
        links: list | None = None,
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
                open_req = netem_pb2.OpenRequest(name=self.project_name, data=fd.read())
            open_resp = client.ProjectOpen(open_req)
            if open_resp.status.code == netem_pb2.StatusCode.ERROR:
                raise RuntimeError(
                    f"GoNetEmLauncher: ProjectOpen failed: {open_resp.status.error}"
                )
            project_id = getattr(open_resp, "id", "")
            if not project_id:
                raise RuntimeError(
                    "GoNetEmLauncher: ProjectOpen returned an empty project id"
                )
            self._request = netem_pb2.ProjectRequest(id=project_id)
            logging.info("GoNetEmLauncher: project opened with id=%s", project_id)

            node_count = link_count = node_start = link_setup = node_config = 0
            node_count_seen = link_count_seen = False
            saw_node_messages = False
            container_start_t = link_start_t = None
            container_time = link_time = 0.0
            try:
                for msg in client.TopologyRun(self._request):
                    code = msg.code
                    if code == netem_pb2.TopologyRunMsg.NODE_MESSAGES:
                        # The server sends this terminal message only after
                        # Topology.Run completed successfully. Progress events
                        # are batched best-effort and can be dropped when the
                        # stream closes, so the terminal marker is authoritative.
                        saw_node_messages = True
                    elif code == netem_pb2.TopologyRunMsg.NODE_COUNT:
                        node_count = msg.total
                        node_count_seen = True
                        if progress_cb:
                            progress_cb("NODE_COUNT", 0, node_count)
                    elif code == netem_pb2.TopologyRunMsg.LINK_COUNT:
                        link_count = msg.total
                        link_count_seen = True
                        if progress_cb:
                            progress_cb("LINK_COUNT", 0, link_count)
                    elif code == netem_pb2.TopologyRunMsg.NODE_START:
                        if container_start_t is None:
                            container_start_t = time.monotonic()
                        node_start += max(int(msg.total or 0), 1)
                        container_time = time.monotonic() - container_start_t
                        if progress_cb:
                            progress_cb("NODE_START", node_start, node_count)
                    elif code == netem_pb2.TopologyRunMsg.LINK_SETUP:
                        if link_start_t is None:
                            link_start_t = time.monotonic()
                        link_setup += max(int(msg.total or 0), 1)
                        link_time = time.monotonic() - link_start_t
                        if progress_cb:
                            progress_cb("LINK_SETUP", link_setup, link_count)
                    elif code == netem_pb2.TopologyRunMsg.NODE_LOADCONFIG:
                        node_config += max(int(msg.total or 0), 1)
                        if progress_cb:
                            progress_cb("NODE_LOADCONFIG", node_config, node_count)
            except Exception as exc:
                raise RuntimeError(
                    f"GoNetEmLauncher: TopologyRun failed: {exc}"
                ) from exc

            # A clean EOF without the terminal NODE_MESSAGES marker is a
            # partial (crash/timeout) run. With the marker, batched or dropped
            # progress counters are informational only.
            if not saw_node_messages:
                if node_count_seen and node_start != node_count:
                    raise RuntimeError(
                        "GoNetEmLauncher: TopologyRun incomplete: "
                        f"started {node_start} of {node_count} node(s)"
                    )
                if link_count_seen and link_setup != link_count:
                    raise RuntimeError(
                        "GoNetEmLauncher: TopologyRun incomplete: "
                        f"set up {link_setup} of {link_count} link(s)"
                    )

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

    def _cleanup_failed_start(self) -> bool:
        """Best-effort cleanup after a failed start_containers() call.

        Returns:
            True when cleanup completed or there was nothing retryable;
            False when the project handles must be retained for a retry.
        """
        request = self._request
        if request is None:
            self._close_transport()
            return True
        if not str(getattr(request, "id", "") or ""):
            # An empty project id cannot target a server-side project, so
            # retaining the transport would only leak it.
            self._close_transport()
            self._request = None
            return True
        try:
            cleaned = self.force_close_project()
        except Exception as exc:
            logging.error("GoNetEmLauncher: cleanup after failed start failed: %s", exc)
            return False
        if cleaned:
            self._close_transport()
            self._request = None
            return True
        logging.error(
            "GoNetEmLauncher: force-close after failed start did not complete; "
            "retaining the project handle for retry"
        )
        return False

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
        progress_cb: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """No-op: GoNetem already wired links during TopologyRun."""

    def close_project(
        self,
        progress_cb: Callable[[str, int, int], None] | None = None,
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
                    node_close += max(int(msg.total or 0), 1)
                    if progress_cb:
                        progress_cb("CLOSE_NODE_CLOSE", node_close, node_count)
            if node_count and node_close != node_count:
                raise RuntimeError(
                    "GoNetEmLauncher: ProjectClose incomplete: "
                    f"closed {node_close} of {node_count} node(s)"
                )
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

    @staticmethod
    def _check_link_ack(response, action: str, link) -> None:
        """Raise when a link RPC returns a non-OK ``AckResponse``.

        gRPC itself reports transport failures as exceptions; an application
        failure is carried in ``AckResponse.status`` and must surface too, or
        LinkOps records the operation as applied and drops its retry flag.
        """
        status = getattr(response, "status", None)
        if status is None:
            raise RuntimeError(
                f"GoNetEmLauncher: {action} failed for "
                f"{link.source.name}<->{link.target.name}: missing status"
            )
        if status.code != netem_pb2.StatusCode.OK:
            detail = getattr(status, "error", "") or "server returned a non-OK status"
            raise RuntimeError(
                f"GoNetEmLauncher: {action} failed for "
                f"{link.source.name}<->{link.target.name}: {detail}"
            )

    def update_link(self, link) -> None:
        """Update *link* on the emulator, raising when the backend fails."""
        if self._client is None or self._request is None:
            raise RuntimeError(
                "GoNetEmLauncher: update_link called before project is open"
            )
        try:
            response = self._client.LinkUpdate(self._link_request(link))
        except Exception as exc:
            raise RuntimeError(
                "GoNetEmLauncher: LinkUpdate failed for "
                f"{link.source.name}<->{link.target.name}: {exc}"
            ) from exc
        self._check_link_ack(response, "LinkUpdate", link)

    def update_links(self, links) -> netem_pb2.LinkUpdateManyResponse:  # type: ignore[name-defined]
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
        """Create *link* on the emulator, raising when the backend fails."""
        if self._client is None or self._request is None:
            raise RuntimeError(
                "GoNetEmLauncher: add_link called before project is open"
            )
        try:
            response = self._client.LinkAdd(self._link_request(link))
        except Exception as exc:
            raise RuntimeError(
                "GoNetEmLauncher: LinkAdd failed for "
                f"{link.source.name}<->{link.target.name}: {exc}"
            ) from exc
        self._check_link_ack(response, "LinkAdd", link)

    def delete_link(self, link) -> None:
        """Delete *link* on the emulator, raising when the backend fails."""
        if self._client is None or self._request is None:
            raise RuntimeError(
                "GoNetEmLauncher: delete_link called before project is open"
            )
        try:
            response = self._client.LinkDel(self._link_request(link))
        except Exception as exc:
            raise RuntimeError(
                "GoNetEmLauncher: LinkDel failed for "
                f"{link.source.name}<->{link.target.name}: {exc}"
            ) from exc
        self._check_link_ack(response, "LinkDel", link)

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
