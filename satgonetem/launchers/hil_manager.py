"""
Hardware-in-the-Loop (HIL) manager for satellite network emulation.

Works alongside the GoNetEmLauncher.
The launcher manages satellites, ISLs, and non-HIL ground stations as normal.
HILManager takes over only for ground stations declared in gnd_hardware_map.

For each HIL ground station, HILManager:
  - Prevents the launcher from creating a Docker container for it.
  - Creates a veth pair with the SAT end in the satellite container namespace
    and the GND end in the host namespace.
  - Bridges the GND-side veth with the nominated physical hardware interface.
  - Applies SAT-side netem+TBF qdiscs for orbital propagation delay.

Operational model:

  SAT container ns          host ns              physical HW
  +------------+           +-----------+        +-----------+
  | eth{gnd_id}|<---veth-->| hil{id}   |--+    |           |
  | (netem+TBF)|           | (bridge   |  +--> | {hw_iface}|
  +------------+           |  slave)   |       | (bridge   |
                           +-----------+       |  slave)   |
                                               +-----------+
                           brhil{gnd_id} (Linux bridge)

Handover: when the active satellite for a HIL ground station changes,
TopologyManager calls teardown_link on the old link and setup_link on the
new one. HILManager tears down the bridge, rebuilds it with the same
hardware interface enslaved to the new satellite's veth.

PID resolution: satellite container PIDs are resolved from node.container
at call time and refreshed into node.container_pid. When no container is
attached, node.container_pid (set during start_containers) is used as a
fallback. If Docker cannot refresh the attached container's state, the
cached PID is used as a last resort so teardown can still remove the veth;
the cached PID may be stale (a PID-reuse race at namespace entry).
HILManager does not depend on any launcher class internally.

Privileges required:
  CAP_SYS_ADMIN (setns into container namespaces, nsenter)
  CAP_NET_ADMIN  (veth creation, bridge management, tc)
"""

import ctypes
import errno
import logging
import subprocess

from pyroute2 import IPRoute

from satgonetem.ports import CommandRunner, default_runner

CLONE_NEWNET = 0x40000000
HOST_NS_PATH = "/proc/1/ns/net"


def _is_enodev(exc: Exception) -> bool:
    """Return True if *exc* represents ENODEV (no such device)."""
    return (
        getattr(exc, "code", None) == errno.ENODEV
        or getattr(exc, "errno", None) == errno.ENODEV
    )


class _NamespaceRestoreError(RuntimeError):
    """Raised when a thread cannot return to the host network namespace."""


def _run_tc_batch(
    pid: int, commands: list[str], runner: CommandRunner = default_runner
) -> None:
    """Run tc in batch mode inside the network namespace of pid.

    Args:
        pid: Kernel PID of the target container process.
        commands: List of tc commands to execute in batch mode.

    Raises:
        RuntimeError: If tc exits with a non-zero return code.
    """
    batch_input = "\n".join(commands) + "\n"
    r = runner.run(
        ["nsenter", f"--net=/proc/{pid}/ns/net", "tc", "-batch", "-"],
        input=batch_input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())


def _run_ip_batch(
    pid: int, commands: list[str], runner: CommandRunner = default_runner
) -> None:
    """Run ip in batch mode inside the network namespace of pid.

    Args:
        pid: Kernel PID of the target container process.
        commands: List of ip commands to execute in batch mode.

    Raises:
        RuntimeError: If ip exits with a non-zero return code.
    """
    batch_input = "\n".join(commands) + "\n"
    r = runner.run(
        ["nsenter", f"--net=/proc/{pid}/ns/net", "ip", "-batch", "-"],
        input=batch_input,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())


class HILManager:
    """
    Standalone Hardware-in-the-Loop manager.

    Works alongside any NetworkLauncher without subclassing it. The caller
    (TopologyManager) is responsible for:
      - Excluding HIL ground station nodes from launcher.start_containers().
      - Excluding HIL ground station links from launcher.wire_links() and
        the per-step launcher.add_link() / delete_link() / update_link() calls.
      - Calling HILManager.wire_links(), setup_link(), teardown_link(),
        update_link(), and teardown_all() for HIL-managed links.

    Satellite container PIDs are resolved from node.container at call time
    and refreshed into node.container_pid, so a restarted container is not
    mistaken for a stale PID. Without an attached container, the
    launcher-populated node.container_pid is used. If the attached container
    cannot be refreshed, the cached PID is used as a bounded-cleanup
    fallback, accepting a residual PID-reuse race. No launcher reference is
    held internally.

    Attributes:
        _gnd_hardware_map (dict[str, str]): Maps ground station node names
            to physical hardware interface names (e.g. {"Gnd0": "eth1"}).
        _gnd_capacity_kbps (int): Default link capacity for ground station
            links in kbps.
        _netem_limit (int): netem queue limit in packets applied to the
            SAT-side interfaces. 0 keeps the kernel default.
        _txqueuelen (int): Transmit queue length in packets applied to the
            SAT-side interfaces. 0 keeps the kernel default.
        _hil_bridges (dict[str, str]): Active bridge name per ground station
            name (e.g. {"Gnd0": "brhil0"}).
        _hil_veths (dict[str, str]): Active host-namespace veth name per
            ground station name (e.g. {"Gnd0": "hil0"}).
    """

    def __init__(
        self,
        gnd_hardware_map: dict[str, str],
        gnd_capacity_kbps: int = 100_000,
        netem_limit: int = 1000,
        txqueuelen: int = 1000,
        command_runner: CommandRunner | None = None,
    ) -> None:
        """
        Initialise the HIL manager.

        Args:
            gnd_hardware_map: Mapping of ground station node names to
                physical hardware interface names.
                Example: {"Gnd0": "eth1", "Gnd1": "enp3s0"}.
            gnd_capacity_kbps: Default SAT-side link capacity in kbps,
                used when the link object does not carry capacity attributes.
            netem_limit: netem queue limit in packets for the SAT-side
                interfaces. 0 keeps the kernel default (1000 packets).
            txqueuelen: Transmit queue length in packets for the SAT-side
                interfaces. 0 keeps the kernel default (1000 packets).
        """
        self._gnd_hardware_map: dict[str, str] = gnd_hardware_map
        self._gnd_capacity_kbps: int = gnd_capacity_kbps
        self._netem_limit: int = netem_limit
        self._txqueuelen: int = txqueuelen
        self._command_runner: CommandRunner = command_runner or default_runner
        self._hil_bridges: dict[str, str] = {}
        self._hil_veths: dict[str, str] = {}

    def is_hil_node(self, node_name: str) -> bool:
        """
        Return True if node_name is a HIL-managed ground station.

        Args:
            node_name: Node identifier (e.g. "Gnd0", "Sat3").

        Returns:
            True if node_name appears in gnd_hardware_map.
        """
        return node_name in self._gnd_hardware_map

    def is_hil_link(self, link) -> bool:
        """
        Return True if either endpoint of link is a HIL-managed ground station.

        Args:
            link: Link model instance. Checked only if link.type is
                "GroundStationLink".

        Returns:
            True if the link connects a satellite to a HIL ground station.
        """
        if getattr(link, "type", "") != "GroundStationLink":
            return False
        return (
            link.source.name in self._gnd_hardware_map
            or link.target.name in self._gnd_hardware_map
        )

    def _get_hil_gnd(self, link):
        """
        Return the HIL ground station endpoint of a link.

        Args:
            link: A GroundStationLink with one HIL endpoint.

        Returns:
            The Node endpoint whose name is in gnd_hardware_map.
        """
        if link.target.name in self._gnd_hardware_map:
            return link.target
        return link.source

    def _get_sat(self, link):
        """
        Return the satellite endpoint of a HIL ground station link.

        Args:
            link: A GroundStationLink with one HIL endpoint.

        Returns:
            The Node endpoint that is not the HIL ground station.
        """
        if link.target.name in self._gnd_hardware_map:
            return link.source
        return link.target

    @staticmethod
    def _positive_int(value: object) -> int:
        """Convert a capacity value to a positive int, or 0 when unusable."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        try:
            capacity = int(value)
        except (OverflowError, ValueError):
            return 0
        return capacity if capacity > 0 else 0

    def _link_capacity_kbps(self, link) -> int:
        """Return the satellite-side capacity for a link in kbps.

        Preference order matches the documented QoS model: the satellite peer's
        capacity (``peer2_capacity`` when the ground station is ``source``,
        otherwise ``peer1_capacity``), then the other peer / link capacity,
        then the configured ground-station default.
        """
        source_name = getattr(getattr(link, "source", None), "name", None)
        sat_is_target = source_name in self._gnd_hardware_map
        primary_attr = "peer2_capacity" if sat_is_target else "peer1_capacity"
        secondary_attr = "peer1_capacity" if sat_is_target else "peer2_capacity"

        for attr in (primary_attr, secondary_attr, "capacity"):
            cap = self._positive_int(getattr(link, attr, 0))
            if cap:
                return cap

        get_capacity = getattr(link, "get_capacity", None)
        if callable(get_capacity):
            try:
                modeled = self._positive_int(get_capacity())
            except Exception as exc:
                logging.warning("HIL: could not read link capacity: %s", exc)
            else:
                if modeled:
                    return modeled

        return self._gnd_capacity_kbps

    @staticmethod
    def _cached_sat_pid(sat) -> int | None:
        """Return the launcher-cached container PID, or None when unusable."""
        return HILManager._positive_int(getattr(sat, "container_pid", 0)) or None

    def _resolve_sat_pid(self, sat) -> int | None:
        """Return the satellite container's current network-namespace PID.

        Reads ``node.container`` at call time and refreshes
        ``node.container_pid`` so a restarted container (possibly with a
        recycled PID) is not confused with a stale snapshot. Without an
        attached container, the launcher-populated ``container_pid`` is used
        as a fallback.

        When Docker cannot refresh the container state, the cached
        ``container_pid`` is used if it is a positive int. This keeps teardown
        able to remove the veth after the container state became unavailable,
        but accepts a residual TOCTOU risk: the cached PID may have been
        recycled by an unrelated process before ``nsenter`` runs. A container
        reported as stopped is never used.

        Args:
            sat: Satellite node endpoint of the HIL link.

        Returns:
            The current PID, or None when the container is known stopped, its
            refreshed state is unusable, or no cached PID is available.
        """
        container = getattr(sat, "container", None)
        attrs = getattr(container, "attrs", None) if container is not None else None
        if isinstance(attrs, dict):
            reload = getattr(container, "reload", None)
            if callable(reload):
                try:
                    reload()
                except Exception as exc:
                    cached_pid = self._cached_sat_pid(sat)
                    if cached_pid is None:
                        logging.error(
                            "HIL: could not refresh container state for %s: %s; "
                            "no cached container_pid available",
                            sat.name,
                            exc,
                        )
                        return None
                    logging.warning(
                        "HIL: could not refresh container state for %s: %s; "
                        "falling back to cached container_pid %d",
                        sat.name,
                        exc,
                        cached_pid,
                    )
                    return cached_pid
                attrs = getattr(container, "attrs", None)
                if not isinstance(attrs, dict):
                    logging.error(
                        "HIL: no container state for %s after refresh", sat.name
                    )
                    return None

            state = attrs.get("State") or {}
            pid = self._positive_int(state.get("Pid"))
            if state.get("Running") is False or not pid:
                logging.error(
                    "HIL: container for %s is not running (pid=%r)",
                    sat.name,
                    state.get("Pid"),
                )
                return None
            sat.container_pid = pid
            return pid

        return self._cached_sat_pid(sat)

    def _create_veth(self, sat_name: str, sat_pid: int, gnd_id: int) -> bool:
        """
        Create a veth pair with the SAT end in the satellite container
        namespace and the GND end (hil{gnd_id}) in the host namespace.

        The method temporarily enters the satellite container's network
        namespace via setns(). The peer is placed in the root network
        namespace using HOST_NS_PATH (/proc/1/ns/net).

        Args:
            sat_name: Satellite node name, used only for logging.
            sat_pid: Kernel PID of the satellite container process.
            gnd_id: Numeric ID of the ground station node.

        Returns:
            True on success. False if the veth could not be created.

        Raises:
            _NamespaceRestoreError: If the host network namespace cannot be
                restored. The veth pair is deleted before the error propagates.
        """
        sat_iface = f"eth{gnd_id}"
        host_iface = f"hil{gnd_id}"

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        try:
            saved_ns = open("/proc/thread-self/ns/net", "rb")  # noqa: SIM115 - fd must stay open across namespaces
        except FileNotFoundError:
            saved_ns = open("/proc/self/ns/net", "rb")  # noqa: SIM115 - fd must stay open across namespaces
        entered_satellite_ns = False
        restored_host_ns = False
        veth_created = False
        succeeded = False
        try:
            with open(f"/proc/{sat_pid}/ns/net", "rb") as ns_fd:
                if libc.setns(ns_fd.fileno(), CLONE_NEWNET) != 0:
                    logging.error(
                        "HIL: failed to enter namespace of %s (pid %d)",
                        sat_name,
                        sat_pid,
                    )
                    return False
                entered_satellite_ns = True

            with IPRoute() as ipr:
                try:
                    ipr.link(
                        "add",
                        ifname=sat_iface,
                        kind="veth",
                        peer={"ifname": host_iface, "net_ns_fd": HOST_NS_PATH},
                    )
                except Exception as exc:
                    if "exist" not in str(exc).lower():
                        raise
                    # A previous rollback may have left the SAT-side veth
                    # behind, or left only the host-side peer (hil{gnd_id}).
                    # Remove whichever stale side exists and retry once.
                    try:
                        stale = ipr.link("get", ifname=sat_iface)
                    except Exception as get_exc:
                        if not _is_enodev(get_exc):
                            raise
                        stale = []
                    if stale:
                        ipr.link("del", index=stale[0]["index"])
                    else:
                        if libc.setns(saved_ns.fileno(), CLONE_NEWNET) != 0:
                            logging.error(
                                "HIL: failed to restore host network namespace "
                                "for %s (pid %d)",
                                sat_name,
                                sat_pid,
                            )
                            raise _NamespaceRestoreError(
                                "HIL: failed to restore host network namespace "
                                f"for {sat_name} (pid {sat_pid})"
                            ) from exc
                        restored_host_ns = True
                        with IPRoute() as host_ipr:
                            try:
                                host_stale = host_ipr.link("get", ifname=host_iface)
                            except Exception as host_exc:
                                if not _is_enodev(host_exc):
                                    raise
                                host_stale = []
                            if host_stale:
                                host_ipr.link("del", index=host_stale[0]["index"])
                        with open(f"/proc/{sat_pid}/ns/net", "rb") as reenter_fd:
                            if libc.setns(reenter_fd.fileno(), CLONE_NEWNET) != 0:
                                logging.error(
                                    "HIL: failed to re-enter namespace of %s (pid %d)",
                                    sat_name,
                                    sat_pid,
                                )
                                return False
                            restored_host_ns = False
                    ipr.link(
                        "add",
                        ifname=sat_iface,
                        kind="veth",
                        peer={"ifname": host_iface, "net_ns_fd": HOST_NS_PATH},
                    )
                veth_created = True
                lnks = ipr.link("get", ifname=sat_iface)
                if lnks:
                    ipr.link("set", index=lnks[0]["index"], state="up")

            if libc.setns(saved_ns.fileno(), CLONE_NEWNET) != 0:
                logging.error(
                    "HIL: failed to restore host network namespace for %s (pid %d)",
                    sat_name,
                    sat_pid,
                )
                raise _NamespaceRestoreError(
                    "HIL: failed to restore host network namespace for "
                    f"{sat_name} (pid {sat_pid})"
                )
            restored_host_ns = True

            with IPRoute() as ipr:
                lnks = ipr.link("get", ifname=host_iface)
                if lnks:
                    ipr.link("set", index=lnks[0]["index"], state="up")

            logging.debug(
                "HIL: veth created: %s (SAT) <-> %s (host) for %s<->Gnd%d",
                sat_iface,
                host_iface,
                sat_name,
                gnd_id,
            )
            succeeded = True
            return True

        except _NamespaceRestoreError:
            # Fatal: this thread may still be inside the container namespace,
            # so the caller must not continue as if the operation succeeded.
            raise
        except Exception as exc:
            logging.error(
                "HIL: failed to create veth for %s<->Gnd%d: %s",
                sat_name,
                gnd_id,
                exc,
            )
            return False
        finally:
            restore_failed = False
            try:
                # Retry the restore once. A failure here is fatal even when the
                # body already failed for another reason: this thread may still
                # be inside the container namespace, so the caller must not
                # continue as if the operation had failed safely.
                if (
                    entered_satellite_ns
                    and not restored_host_ns
                    and libc.setns(saved_ns.fileno(), CLONE_NEWNET) != 0
                ):
                    logging.error(
                        "HIL: failed to restore host network namespace for %s (pid %d)",
                        sat_name,
                        sat_pid,
                    )
                    restore_failed = True
                # Any failure after the pair was added must not leak it.
                if veth_created and not succeeded:
                    self._delete_sat_veth(sat_name, sat_pid, gnd_id)
            finally:
                saved_ns.close()
            if restore_failed:
                raise _NamespaceRestoreError(
                    "HIL: failed to restore host network namespace for "
                    f"{sat_name} (pid {sat_pid})"
                )

    def _create_bridge(self, gnd_name: str, gnd_id: int) -> bool:
        """
        Create a Linux bridge and attach the host-side veth and the
        physical hardware interface.

        Args:
            gnd_name: Ground station node name (e.g. "Gnd0").
            gnd_id: Numeric ID of the ground station node.

        Returns:
            True if the bridge was created and all interfaces attached.
            False on any error.
        """
        hw_iface = self._gnd_hardware_map[gnd_name]
        host_veth = f"hil{gnd_id}"
        br_name = f"brhil{gnd_id}"

        try:
            with IPRoute() as ipr:
                ipr.link("add", ifname=br_name, kind="bridge")
                # Track the bridge as soon as it exists. A later failure must
                # be able to delete it, or retry deleting it at teardown.
                self._hil_bridges[gnd_name] = br_name
                br_links = ipr.link("get", ifname=br_name)
                br_idx = br_links[0]["index"]
                ipr.link("set", index=br_idx, state="up")

                veth_links = ipr.link("get", ifname=host_veth)
                ipr.link("set", index=veth_links[0]["index"], master=br_idx)

                hw_links = ipr.link("get", ifname=hw_iface)
                ipr.link("set", index=hw_links[0]["index"], master=br_idx)
                ipr.link("set", index=hw_links[0]["index"], state="up")

            self._hil_veths[gnd_name] = host_veth
            logging.info(
                "HIL: bridge %s up for %s (veth=%s hw=%s)",
                br_name,
                gnd_name,
                host_veth,
                hw_iface,
            )
            return True

        except Exception as exc:
            logging.error("HIL: failed to create bridge for %s: %s", gnd_name, exc)
            # Delete the partial bridge. If that also fails, _teardown_bridge
            # keeps it tracked so a later teardown can retry.
            self._teardown_bridge(gnd_name)
            return False

    def _apply_qos(
        self, sat_name: str, sat_pid: int, gnd_id: int, delay_ms: int, rate_kbps: int
    ) -> bool:
        """
        Apply netem+TBF qdiscs to the SAT-side interface of a HIL link.

        Only the satellite side carries emulated qdiscs. The host-side veth
        and bridge are transparent L2 paths to the physical hardware.

        Args:
            sat_name: Satellite node name, used for logging.
            sat_pid: Kernel PID of the satellite container process.
            gnd_id: Numeric ID of the ground station, used to derive the
                SAT-side interface name (eth{gnd_id}).
            delay_ms: Propagation delay in milliseconds.
            rate_kbps: Link capacity in kbps.

        Returns:
            True when the qdisc commands succeeded. ``txqueuelen`` is a
            best-effort kernel tuning hint and does not fail the link.
        """
        sat_iface = f"eth{gnd_id}"
        delay_ms = max(delay_ms, 1)
        rate_mbit = max(rate_kbps / 1000, 0.001)
        burst_bytes = max(rate_kbps * delay_ms // 8, 1500)
        netem_limit = f" limit {self._netem_limit}" if self._netem_limit > 0 else ""
        success = True

        if self._txqueuelen > 0:
            try:
                _run_ip_batch(
                    sat_pid,
                    [f"link set dev {sat_iface} txqueuelen {self._txqueuelen}"],
                    runner=self._command_runner,
                )
            except RuntimeError as exc:
                logging.warning(
                    "HIL: txqueuelen setup failed on %s in %s: %s",
                    sat_iface,
                    sat_name,
                    exc,
                )

        try:
            _run_tc_batch(
                sat_pid,
                [
                    f"qdisc add dev {sat_iface} root handle 1: netem"
                    f" delay {delay_ms}ms{netem_limit}",
                    f"qdisc add dev {sat_iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
                runner=self._command_runner,
            )
        except RuntimeError as exc:
            success = False
            logging.warning(
                "HIL: qdisc setup failed on %s in %s: %s", sat_iface, sat_name, exc
            )

        return success

    def _teardown_bridge(self, gnd_name: str) -> bool:
        """
        Delete the Linux bridge for a HIL ground station.

        Deleting the bridge automatically releases all enslaved interfaces.
        The hardware interface returns to an unmastered state and is ready
        for the next handover. The host-side veth is removed by the kernel
        when the SAT-side veth is subsequently deleted.

        Args:
            gnd_name: Ground station node name (e.g. "Gnd0").

        Returns:
            True when the bridge is gone (or was never tracked).
        """
        br_name = self._hil_bridges.get(gnd_name)

        if not br_name:
            self._hil_veths.pop(gnd_name, None)
            return True

        try:
            with IPRoute() as ipr:
                try:
                    br_links = ipr.link("get", ifname=br_name)
                except Exception as exc:
                    if not _is_enodev(exc):
                        raise
                    br_links = []
                if br_links:
                    ipr.link("del", index=br_links[0]["index"])
            logging.info("HIL: bridge %s torn down for %s", br_name, gnd_name)
        except Exception as exc:
            # Keep the tracking entry: a failed delete must be retryable.
            logging.error("HIL: failed to teardown bridge %s: %s", br_name, exc)
            return False

        self._hil_bridges.pop(gnd_name, None)
        self._hil_veths.pop(gnd_name, None)
        return True

    def _delete_sat_veth(self, sat_name: str, sat_pid: int, gnd_id: int) -> bool:
        """
        Delete the SAT-side veth interface.

        The kernel automatically removes the host-side peer (hil{gnd_id})
        when the SAT-side interface is deleted.

        Args:
            sat_name: Satellite node name, used for logging.
            sat_pid: Kernel PID of the satellite container process.
            gnd_id: Numeric ID of the ground station node.

        Returns:
            True when the SAT-side veth was deleted.
        """
        sat_iface = f"eth{gnd_id}"
        r = self._command_runner.run(
            [
                "nsenter",
                f"--net=/proc/{sat_pid}/ns/net",
                "ip",
                "link",
                "delete",
                sat_iface,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            stderr = (r.stderr or "").strip()
            lowered = stderr.lower()
            if "cannot find device" in lowered or "does not exist" in lowered:
                logging.debug(
                    "HIL: veth %s in %s is already absent", sat_iface, sat_name
                )
                return True
            logging.error(
                "HIL: failed to delete veth %s in %s: %s",
                sat_iface,
                sat_name,
                stderr,
            )
            return False
        logging.debug("HIL: deleted veth %s in %s", sat_iface, sat_name)
        return True

    def setup_link(self, link) -> bool:
        """
        Set up a HIL link: create veth, bridge, and SAT-side qdiscs.

        Called by TopologyManager for HIL ground station links during
        initial wiring (wire_links) and on handover arrival (add_link).

        Requires a running satellite container: the PID is resolved from
        node.container at call time (refreshing node.container_pid), or
        falls back to node.container_pid when no container is attached.

        Args:
            link: A GroundStationLink whose ground station endpoint is
                listed in gnd_hardware_map.

        Returns:
            True only when the veth, bridge and SAT-side QoS are all in place.
            Callers must keep the link pending when this returns False.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = self._resolve_sat_pid(sat)

        if not sat_pid:
            logging.error(
                "HIL: no container_pid on %s, cannot set up link to %s",
                sat.name,
                gnd.name,
            )
            return False

        if not self._create_veth(sat.name, sat_pid, gnd.id):
            return False
        if not self._create_bridge(gnd.name, gnd.id):
            # The bridge failed; remove the veth pair created above rather
            # than leaking it until container teardown.
            self._delete_sat_veth(sat.name, sat_pid, gnd.id)
            return False

        delay_ms = max(int(link.delay), 1)
        rate_kbps = self._link_capacity_kbps(link)
        if not self._apply_qos(sat.name, sat_pid, gnd.id, delay_ms, rate_kbps):
            # A partially configured QoS path is not a working link. Tear the
            # pair and bridge down so a retry does not hit EEXIST, and report
            # failure so the caller keeps the link pending.
            self._teardown_bridge(gnd.name)
            self._delete_sat_veth(sat.name, sat_pid, gnd.id)
            return False
        return True

    def teardown_link(self, link) -> bool:
        """
        Tear down a HIL link: delete bridge and SAT-side veth.

        Called by TopologyManager on handover departure (delete_link).
        The bridge is deleted first to release the hardware interface cleanly.
        The SAT-side veth deletion cascades to the host-side peer.

        Args:
            link: A GroundStationLink whose ground station endpoint is
                listed in gnd_hardware_map.

        Returns:
            True only when the bridge and SAT-side veth are both gone.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = self._resolve_sat_pid(sat)

        if not self._teardown_bridge(gnd.name):
            # Keep the veth pair intact: deleting it would leave the tracked
            # bridge pointing at a vanished host-side peer and make the next
            # handover fail with EEXIST.
            return False

        if not sat_pid:
            logging.error(
                "HIL: no container_pid on %s, cannot delete veth for %s",
                sat.name,
                gnd.name,
            )
            return False

        return self._delete_sat_veth(sat.name, sat_pid, gnd.id)

    def update_link(self, link) -> bool:
        """
        Update SAT-side QoS for a HIL link.

        Called by TopologyManager at each simulation step when orbital
        parameters change. Only the satellite container interface carries
        emulated qdiscs; the hardware side is unmodified.

        Args:
            link: A GroundStationLink with updated delay and capacity values.

        Returns:
            True when the SAT-side qdiscs were updated. Callers keep the link
            pending on False so the transient failure is retried.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = self._resolve_sat_pid(sat)
        sat_iface = f"eth{gnd.id}"

        if not sat_pid:
            logging.warning(
                "HIL: no container_pid on %s, cannot update QoS on %s",
                sat.name,
                sat_iface,
            )
            return False

        delay_ms = max(int(link.delay), 1)
        rate_kbps = self._link_capacity_kbps(link)
        rate_mbit = max(rate_kbps / 1000, 0.001)
        burst_bytes = max(rate_kbps * delay_ms // 8, 1500)
        netem_limit = f" limit {self._netem_limit}" if self._netem_limit > 0 else ""

        try:
            _run_tc_batch(
                sat_pid,
                [
                    f"qdisc change dev {sat_iface} root handle 1: netem"
                    f" delay {delay_ms}ms{netem_limit}",
                    f"qdisc change dev {sat_iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
                runner=self._command_runner,
            )
            logging.debug(
                "HIL: updated %s in %s: delay=%dms rate=%dkbps",
                sat_iface,
                sat.name,
                delay_ms,
                rate_kbps,
            )
        except RuntimeError as exc:
            logging.error(
                "HIL: failed to update QoS on %s (pid %d): %s",
                sat_iface,
                sat_pid,
                exc,
            )
            return False
        return True

    def wire_links(self, links: list) -> None:
        """
        Set up all active HIL links for initial wiring.

        Called by TopologyManager after the launcher's wire_links completes,
        for the subset of links involving HIL ground stations.

        Args:
            links: HIL GroundStationLink objects that are currently active.

        Raises:
            RuntimeError: If any link cannot be fully set up, so a failed
                initial wiring is not reported as a successful start.
        """
        wired: list = []
        try:
            for link in links:
                if not self.setup_link(link):
                    raise RuntimeError(
                        "HIL: failed to set up link "
                        f"{link.source.name}<->{link.target.name}"
                    )
                wired.append(link)
        except BaseException:
            for wired_link in reversed(wired):
                try:
                    self.teardown_link(wired_link)
                except Exception:  # noqa: BLE001 - preserve the setup error
                    logging.exception(
                        "HIL: failed to roll back link %s<->%s",
                        wired_link.source.name,
                        wired_link.target.name,
                    )
            raise

    def teardown_all(self) -> None:
        """
        Tear down all active HIL bridges.

        Called by TopologyManager during project teardown, before the launcher
        removes satellite containers. Deleting bridges releases hardware
        interfaces cleanly. Container removal cascades to veth peer deletion.

        Raises:
            RuntimeError: If one or more bridges failed to tear down.
        """
        failed: list[str] = []
        for gnd_name in list(self._hil_bridges.keys()):
            if self._teardown_bridge(gnd_name) is False:
                failed.append(gnd_name)
        if failed:
            names = ", ".join(failed)
            raise RuntimeError(f"HIL: failed to tear down bridge(s) for: {names}")
