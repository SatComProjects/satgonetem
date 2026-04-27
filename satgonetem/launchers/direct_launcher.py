"""

Direct Docker/veth/tc network management.

Replaces GoNetem gRPC control with:
  - docker run    (container startup, parallel)
  - pyroute2      (veth pair creation inside container network namespaces)
  - nsenter + tc  (netem+TBF qdisc setup and per-step delay updates)
  - docker rm -f  (teardown)

Container naming convention : <project_name>.<NodeName>   e.g. Small.Sat0
Interface naming convention  : eth<peer_id> inside each container
  - In Sat0's container, the veth to Sat1 (id=1) is named eth1
  - In Sat1's container, the veth to Sat0 (id=0) is named eth0
"""

import ctypes
import logging
import subprocess
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import docker
from pyroute2 import IPRoute

from satgonetem.launchers.base_launcher import NetworkLauncher
from satgonetem.models.node import Node

CLONE_NEWNET = 0x40000000  # Linux namespace flag for a network namespace from sched.h
DEFAULT_IMAGE = "jariassuarez/sgnt:satellite"


# Helpers


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _nsenter_batch(pid: int, tool: str, commands: list[str]) -> tuple[int, str]:
    """Run *tool* in batch mode inside the network namespace of *pid*."""
    batch_input = "\n".join(commands) + "\n"
    r = subprocess.run(
        ["nsenter", f"--net=/proc/{pid}/ns/net", tool, "-batch", "-"],
        input=batch_input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return r.returncode, r.stderr


# DirectLauncher


class DirectLauncher(NetworkLauncher):
    """Replace GoNetem gRPC with direct Docker/veth/tc network management."""

    def __init__(
        self,
        project_name: str,
        isl_capacity_kbps: int = 100_000,
        gnd_capacity_kbps: int = 100_000,
        satellite_image: Optional[str] = None,
    ) -> None:
        super().__init__(project_name, isl_capacity_kbps, gnd_capacity_kbps)
        self.satellite_image = satellite_image or DEFAULT_IMAGE
        # node_name (e.g. 'Sat0', 'Gnd0') -> kernel PID of container process
        self._node_pids: dict[str, int] = {}

    #  Helpers

    def _container_name(self, node_name: str) -> str:
        safe_project = self.project_name.replace(" ", "_")
        return f"{safe_project}.{node_name}"

    def _image_for_node(self, node_name: str) -> str:
        """Return the Docker image to use for *node_name* (configurable)."""
        return self.satellite_image

    def _get_pid(self, node) -> int:
        """Resolve a node's kernel PID from the launcher dict or node attribute."""
        name = node.name if hasattr(node, "name") else str(node)
        return self._node_pids.get(name) or int(getattr(node, "container_pid", 0) or 0)

    @staticmethod
    def _link_capacities(link) -> tuple[int, int]:
        """Return (peer1_rate_kbps, peer2_rate_kbps) for a link.
        link.capacity / peer1_capacity / peer2_capacity are all in kbps."""
        p1 = int(getattr(link, "peer1_capacity", 0) or 0)
        p2 = int(getattr(link, "peer2_capacity", 0) or 0)
        return p1, p2

    #  Container startup

    def _start_one_container(self, node_name: str) -> tuple[str, str]:
        """Start a single container.  Returns (node_name, container_id_or_empty)."""
        name = self._container_name(node_name)
        image = self._image_for_node(node_name)
        r = _run(
            [
                "docker",
                "run",
                "-d",
                "-v",
                "/tmp/:/tmp/",  # We want to share /tmp for interface logging and stuff, might be unsecure but w/e
                "--name",
                name,
                "--hostname",
                name,
                "--network",
                "none",  # no default network, we'll set up veth pairs manually
                "--privileged",  # needed for tc and iproute2 inside container
                image,
            ]
        )
        if r.returncode != 0:
            logging.error("Failed to start container %s: %s", name, r.stderr.strip())
            return node_name, ""
        return node_name, r.stdout.strip()

    def start_containers(
        self,
        nodes: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Start all node containers in parallel, resolve PIDs, and attach
        docker-py container objects to each node (node.container / node.container_pid).
        """
        n = len(nodes)
        if progress_cb:
            progress_cb("NODE_COUNT", 0, n)

        # Index nodes by name for fast lookup
        nodes_by_name: dict[str, object] = {node.name: node for node in nodes}

        started_ids: dict[str, str] = {}  # node_name -> container_id
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._start_one_container, node.name): node.name
                for node in nodes
            }
            for fut in as_completed(futures):
                node_name, cid = fut.result()
                done += 1
                if cid:
                    started_ids[node_name] = cid
                if progress_cb:
                    progress_cb("NODE_START", done, n)

        if not started_ids:
            logging.info("DirectLauncher: no containers started.")
            return

        # Batch-resolve PIDs and attach docker-py objects via a single inspect call
        cids = list(started_ids.values())
        r = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}}\t{{.Name}}\t{{.State.Pid}}",
                *cids,
            ]
        )

        if r.returncode == 0:
            try:
                docker_client = docker.from_env()
            except Exception as e:
                logging.warning(
                    "Could not create docker client for container association: %s", e
                )
                docker_client = None

            for line in r.stdout.strip().splitlines():
                parts = line.strip().split("\t")
                if len(parts) != 3:
                    continue
                cid, full_name, pid_str = parts
                name_parts = full_name.lstrip("/").split(".", 1)
                if len(name_parts) < 2:
                    continue
                node_name = name_parts[1]

                result = nodes_by_name.get(node_name)

                match result:
                    case Node():
                        node = result
                    case _:
                        raise ValueError(
                            f"Expected Node instance for {node_name}, got {type(result)}"
                        )

                try:
                    self._node_pids[node_name] = int(pid_str)
                    node.container_pid = int(pid_str)
                except ValueError:
                    logging.warning(
                        "Could not parse PID for %s: %s", node_name, pid_str
                    )

                if docker_client is not None:
                    try:
                        node.container = docker_client.containers.get(cid)
                    except Exception as e:
                        logging.warning(
                            "Could not attach container object to %s: %s", node_name, e
                        )

        logging.info(
            "DirectLauncher: started %d/%d containers, resolved %d PIDs.",
            len(started_ids),
            n,
            len(self._node_pids),
        )

    #  Veth pair creation
    # Creation is done directly inside the network namespace so we reduce the number of
    # nsenter calls from 3 to 1

    def _create_links_for_source_sync(
        self,
        src_name: str,
        peers: list[
            tuple[str, str, str, int]
        ],  # (local_iface, peer_iface, peer_name, peer_pid)
    ) -> tuple[int, int]:
        """
        Create veth pairs from *src_name* to each peer node.
        Runs inside a thread so setns() affects only this thread's netns.
        Returns (ok_count, fail_count).
        """
        src_pid = self._node_pids.get(src_name, 0)
        if not src_pid:
            logging.error("No PID for %s, skipping link creation", src_name)
            return 0, len(peers)

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        ok = fail = 0
        saved_ns = open("/proc/self/ns/net", "rb")
        by_peer: dict[int, list[str]] = defaultdict(list)

        try:
            with open(f"/proc/{src_pid}/ns/net", "rb") as ns_fd:
                if libc.setns(ns_fd.fileno(), CLONE_NEWNET) != 0:
                    return 0, len(peers)

            with IPRoute() as ipr:
                for local_iface, peer_iface, peer_name, peer_pid in peers:
                    try:
                        ipr.link(
                            "add",
                            ifname=local_iface,
                            kind="veth",
                            peer={
                                "ifname": peer_iface,
                                "net_ns_fd": f"/proc/{peer_pid}/ns/net",
                            },
                        )
                        lnks = ipr.link("get", ifname=local_iface)
                        if lnks:
                            ipr.link("set", index=lnks[0]["index"], state="up")
                        by_peer[peer_pid].append(peer_iface)
                        ok += 1
                    except Exception as exc:
                        logging.error(
                            "Failed to create veth %s<->%s: %s",
                            local_iface,
                            peer_iface,
                            exc,
                        )
                        fail += 1

            # Bring up the peer ends (each in its own namespace)
            for peer_pid, ifaces in by_peer.items():
                try:
                    with open(f"/proc/{peer_pid}/ns/net", "rb") as peer_ns_fd:
                        libc.setns(peer_ns_fd.fileno(), CLONE_NEWNET)
                    with IPRoute() as ipr:
                        for iface in ifaces:
                            try:
                                lnks = ipr.link("get", ifname=iface)
                                if lnks:
                                    ipr.link("set", index=lnks[0]["index"], state="up")
                            except Exception:
                                pass
                except Exception as exc:
                    logging.warning(
                        "Failed to bring up peer interfaces in pid %d: %s",
                        peer_pid,
                        exc,
                    )

        except Exception as exc:
            logging.error("Error in link creation for %s: %s", src_name, exc)
            fail += len(peers)
        finally:
            libc.setns(saved_ns.fileno(), CLONE_NEWNET)
            saved_ns.close()

        return ok, fail

    #  qdisc helpers

    def _apply_qdiscs_to_node(
        self,
        node_name: str,
        iface_info: list[tuple[str, int, int]],  # (iface, delay_ms, rate_kbps)
        pid: int = 0,
    ) -> None:
        """Apply a netem+TBF qdisc stack to a set of interfaces in one container."""
        pid = pid or self._node_pids.get(node_name, 0)
        if not pid:
            logging.warning("No PID for %s, cannot apply qdiscs", node_name)
            return

        cmds: list[str] = []
        for iface, delay_ms, rate_kbps in iface_info:
            delay_ms = max(delay_ms, 1)
            rate_mbit = max(rate_kbps / 1000, 0.001)
            # burst = BW x propagation_delay in bytes; minimum 1 MTU (1500 B)
            burst_bytes = max(rate_kbps * delay_ms // 8, 1500)
            # limit = burst: no additional queue beyond the burst window
            cmds += [
                f"qdisc add dev {iface} root handle 1: netem delay {delay_ms}ms",
                f"qdisc add dev {iface} parent 1:1 handle 10: tbf"
                f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
            ]

        if cmds:
            rc, err = _nsenter_batch(pid, "tc", cmds)
            if rc != 0:
                logging.warning(
                    "tc qdisc setup failed for %s: %s", node_name, err.strip()
                )

    #  Initial link wiring

    def wire_links(
        self,
        links: list,
        workers: int = 64,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Create all veth pairs for active links and apply initial netem+TBF qdiscs."""
        active = [lnk for lnk in links if lnk.is_active]
        n = len(active)
        if progress_cb:
            progress_cb("LINK_COUNT", 0, n)

        # Group by source node: each source issues its outgoing veth ends
        node_links: dict[str, list[tuple[str, str, str, int]]] = {}
        for lnk in active:
            src_name = lnk.source.name
            tgt_name = lnk.target.name
            tgt_pid = self._node_pids.get(tgt_name, 0)
            if not tgt_pid:
                logging.warning(
                    "No PID for %s, skipping link %s<->%s", tgt_name, src_name, tgt_name
                )
                continue
            local_iface = f"eth{lnk.target.id}"  # inside source container
            peer_iface = f"eth{lnk.source.id}"  # inside target container
            node_links.setdefault(src_name, []).append(
                (local_iface, peer_iface, tgt_name, tgt_pid)
            )

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(self._create_links_for_source_sync, src, peers): src
                for src, peers in node_links.items()
            }
            for fut in as_completed(futs):
                ok, _ = fut.result()
                done += ok
                if progress_cb:
                    progress_cb("LINK_SETUP", done, n)

        # Collect per-node qdisc info: both sides of every active link
        node_qdisc: dict[str, list[tuple[str, int, int]]] = {}
        for lnk in active:
            delay_ms = max(int(lnk.delay), 1)
            p1_kbps, p2_kbps = self._link_capacities(lnk)
            p1_kbps = p1_kbps or self.isl_capacity_kbps
            p2_kbps = p2_kbps or self.isl_capacity_kbps
            src_iface = f"eth{lnk.target.id}"
            tgt_iface = f"eth{lnk.source.id}"
            node_qdisc.setdefault(lnk.source.name, []).append(
                (src_iface, delay_ms, p1_kbps)
            )
            node_qdisc.setdefault(lnk.target.name, []).append(
                (tgt_iface, delay_ms, p2_kbps)
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs2 = [
                pool.submit(self._apply_qdiscs_to_node, node_name, info)
                for node_name, info in node_qdisc.items()
            ]
            for fut in as_completed(futs2):
                try:
                    fut.result()
                except Exception as exc:
                    logging.error("qdisc apply error: %s", exc)

        if progress_cb:
            progress_cb("COMPLETED", 0, 0)

    #  Per-simulation-step link operations

    def _default_capacity_for_link(self, link) -> int:
        """Return the default capacity in kbps for a link based on its type."""
        if getattr(link, "type", "") == "GroundStationLink":
            return self.gnd_capacity_kbps
        return self.isl_capacity_kbps

    def update_link(self, link) -> None:
        """Update netem delay and TBF rate on both ends of a link via tc qdisc change."""
        delay_ms = max(int(link.delay), 1)
        p1_kbps, p2_kbps = self._link_capacities(link)
        default_kbps = self._default_capacity_for_link(link)
        p1_kbps = p1_kbps or default_kbps
        p2_kbps = p2_kbps or default_kbps

        src_pid = self._get_pid(link.source)
        tgt_pid = self._get_pid(link.target)
        src_iface = f"eth{link.target.id}"
        tgt_iface = f"eth{link.source.id}"

        for pid, iface, node_name, rate_kbps in [
            (src_pid, src_iface, link.source.name, p1_kbps),
            (tgt_pid, tgt_iface, link.target.name, p2_kbps),
        ]:
            if not pid:
                logging.warning(
                    "No PID for %s, cannot update link on %s", node_name, iface
                )
                continue
            rate_mbit = max(rate_kbps / 1000, 0.001)
            burst_bytes = max(rate_kbps * delay_ms // 8, 1500)
            rc, err = _nsenter_batch(
                pid,
                "tc",
                [
                    f"qdisc change dev {iface} root handle 1: netem delay {delay_ms}ms",
                    f"qdisc change dev {iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
            )
            if rc != 0:
                logging.error(
                    "Failed to update link on %s (pid %d): %s", iface, pid, err.strip()
                )
            else:
                logging.debug(
                    "Updated link on %s in %s: delay=%dms rate=%dkbps",
                    iface,
                    node_name,
                    delay_ms,
                    rate_kbps,
                )

    def _force_link_rate(self, link, rate_kbps: int) -> None:
        """Apply a specific TBF rate to both ends of a link, ignoring per-link capacity attributes."""
        delay_ms = max(int(link.delay), 1)
        rate_mbit = max(rate_kbps / 1000, 0.001)
        burst_bytes = max(rate_kbps * delay_ms // 8, 1500)

        src_pid = self._get_pid(link.source)
        tgt_pid = self._get_pid(link.target)
        src_iface = f"eth{link.target.id}"
        tgt_iface = f"eth{link.source.id}"

        for pid, iface, node_name in [
            (src_pid, src_iface, link.source.name),
            (tgt_pid, tgt_iface, link.target.name),
        ]:
            if not pid:
                logging.warning(
                    "No PID for %s, cannot update rate on %s", node_name, iface
                )
                continue
            rc, err = _nsenter_batch(
                pid,
                "tc",
                [
                    f"qdisc change dev {iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
            )
            if rc != 0:
                logging.error(
                    "Failed to set rate on %s (pid %d): %s", iface, pid, err.strip()
                )
            else:
                logging.debug(
                    "Set rate on %s in %s to %d kbps", iface, node_name, rate_kbps
                )

    def set_link_capacities(self, isl_kbps: int, gnd_kbps: int, links: list) -> None:
        """Update the default capacities and force the new TBF rates onto all links."""
        self.isl_capacity_kbps = isl_kbps
        self.gnd_capacity_kbps = gnd_kbps
        workers = min(64, len(links)) if links else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(
                    self._force_link_rate, lnk, self._default_capacity_for_link(lnk)
                )
                for lnk in links
            ]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:
                    logging.error("set_link_capacities error: %s", exc)

    # Keep old name as alias so existing callers don't break
    def update_link_delay(self, link) -> None:
        self.update_link(link)

    def add_link(self, link) -> None:
        """Create a new veth pair between two nodes and apply netem+TBF qdiscs."""
        src_name = link.source.name
        tgt_name = link.target.name
        src_pid = self._get_pid(link.source)
        tgt_pid = self._get_pid(link.target)

        if not src_pid or not tgt_pid:
            logging.error(
                "Cannot add link %s<->%s: missing PIDs (src=%s tgt=%s)",
                src_name,
                tgt_name,
                src_pid,
                tgt_pid,
            )
            return

        local_iface = f"eth{link.target.id}"
        peer_iface = f"eth{link.source.id}"

        ok, fail = self._create_links_for_source_sync(
            src_name, [(local_iface, peer_iface, tgt_name, tgt_pid)]
        )
        if fail:
            logging.error("Failed to create veth for link %s<->%s", src_name, tgt_name)
            return

        delay_ms = max(int(link.delay), 1)
        p1_kbps, p2_kbps = self._link_capacities(link)
        p1_kbps = p1_kbps or self.isl_capacity_kbps
        p2_kbps = p2_kbps or self.isl_capacity_kbps

        self._apply_qdiscs_to_node(
            src_name, [(local_iface, delay_ms, p1_kbps)], pid=src_pid
        )
        self._apply_qdiscs_to_node(
            tgt_name, [(peer_iface, delay_ms, p2_kbps)], pid=tgt_pid
        )

    def delete_link(self, link) -> None:
        """Remove a veth pair by deleting the local end (peer end auto-removed by kernel)."""
        src_name = link.source.name
        src_pid = self._get_pid(link.source)
        src_iface = f"eth{link.target.id}"

        if not src_pid:
            logging.error(
                "Cannot delete link %s<->%s: no PID for %s",
                src_name,
                link.target.name,
                src_name,
            )
            return

        r = subprocess.run(
            [
                "nsenter",
                f"--net=/proc/{src_pid}/ns/net",
                "ip",
                "link",
                "delete",
                src_iface,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            logging.error(
                "Failed to delete veth %s in %s: %s",
                src_iface,
                src_name,
                r.stderr.strip(),
            )
        else:
            logging.debug(
                "Deleted veth %s (link %s<->%s)", src_iface, src_name, link.target.name
            )

    #  Teardown

    def close_project(
        self,
        progress_cb: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        """Force-remove all containers belonging to this project."""
        safe_project = self.project_name.replace(" ", "_")
        r = _run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"name=^/{safe_project}\\.",
            ]
        )
        container_ids = [
            cid.strip() for cid in r.stdout.strip().splitlines() if cid.strip()
        ]
        n = len(container_ids)
        if progress_cb:
            progress_cb("CLOSE_NODE_COUNT", 0, n)

        done = 0

        def _rm(cid: str) -> None:
            _run(["docker", "rm", "-f", cid])

        with ThreadPoolExecutor(max_workers=64) as pool:
            futs = {pool.submit(_rm, cid): cid for cid in container_ids}
            for fut in as_completed(futs):
                done += 1
                if progress_cb:
                    progress_cb("CLOSE_NODE_CLOSE", done, n)

        self._node_pids.clear()
        logging.info(
            "DirectLauncher: removed %d containers for project '%s'.",
            done,
            self.project_name,
        )

    def force_close_project(self) -> None:
        """Alias for close_project to match GoNetemLauncher interface. It is already forceful by default, so just call close_project."""
        self.close_project()
