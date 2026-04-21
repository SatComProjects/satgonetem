"""
Hardware-in-the-Loop (HIL) manager for satellite network emulation.

Works alongside any NetworkLauncher (DirectLauncher or GoNetEmLauncher).
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

PID resolution: satellite container PIDs are read from node.container_pid,
which any launcher sets during start_containers. HILManager does not
depend on any launcher class internally.

Privileges required:
  CAP_SYS_ADMIN (setns into container namespaces, nsenter)
  CAP_NET_ADMIN  (veth creation, bridge management, tc)
"""

import ctypes
import logging
import subprocess
from pyroute2 import IPRoute

CLONE_NEWNET = 0x40000000
HOST_NS_PATH = "/proc/1/ns/net"


def _run_tc_batch(pid: int, commands: list[str]) -> None:
    """Run tc in batch mode inside the network namespace of pid.

    Args:
        pid: Kernel PID of the target container process.
        commands: List of tc commands to execute in batch mode.

    Raises:
        RuntimeError: If tc exits with a non-zero return code.
    """
    batch_input = "\n".join(commands) + "\n"
    r = subprocess.run(
        ["nsenter", f"--net=/proc/{pid}/ns/net", "tc", "-batch", "-"],
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

    Satellite container PIDs are resolved from node.container_pid, which is
    set by any launcher during start_containers. No launcher reference is
    held internally.

    Attributes:
        _gnd_hardware_map (dict[str, str]): Maps ground station node names
            to physical hardware interface names (e.g. {"Gnd0": "eth1"}).
        _gnd_capacity_kbps (int): Default link capacity for ground station
            links in kbps.
        _hil_bridges (dict[str, str]): Active bridge name per ground station
            name (e.g. {"Gnd0": "brhil0"}).
        _hil_veths (dict[str, str]): Active host-namespace veth name per
            ground station name (e.g. {"Gnd0": "hil0"}).
    """

    def __init__(
        self,
        gnd_hardware_map: dict[str, str],
        gnd_capacity_kbps: int = 100_000,
    ) -> None:
        """
        Initialise the HIL manager.

        Args:
            gnd_hardware_map: Mapping of ground station node names to
                physical hardware interface names.
                Example: {"Gnd0": "eth1", "Gnd1": "enp3s0"}.
            gnd_capacity_kbps: Default SAT-side link capacity in kbps,
                used when the link object does not carry capacity attributes.
        """
        self._gnd_hardware_map: dict[str, str] = gnd_hardware_map
        self._gnd_capacity_kbps: int = gnd_capacity_kbps
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

    def _link_capacity_kbps(self, link) -> int:
        """Return the satellite-side capacity for a link in kbps."""
        p1 = int(getattr(link, "peer1_capacity", 0) or 0)
        fb = int(getattr(link, "capacity", 0) or 0)
        return p1 or fb or self._gnd_capacity_kbps

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
        """
        sat_iface = f"eth{gnd_id}"
        host_iface = f"hil{gnd_id}"

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        saved_ns = open("/proc/self/ns/net", "rb")
        try:
            with open(f"/proc/{sat_pid}/ns/net", "rb") as ns_fd:
                if libc.setns(ns_fd.fileno(), CLONE_NEWNET) != 0:
                    logging.error(
                        "HIL: failed to enter namespace of %s (pid %d)",
                        sat_name,
                        sat_pid,
                    )
                    return False

            with IPRoute() as ipr:
                ipr.link(
                    "add",
                    ifname=sat_iface,
                    kind="veth",
                    peer={"ifname": host_iface, "net_ns_fd": HOST_NS_PATH},
                )
                lnks = ipr.link("get", ifname=sat_iface)
                if lnks:
                    ipr.link("set", index=lnks[0]["index"], state="up")

            libc.setns(saved_ns.fileno(), CLONE_NEWNET)

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
            return True

        except Exception as exc:
            logging.error(
                "HIL: failed to create veth for %s<->Gnd%d: %s",
                sat_name,
                gnd_id,
                exc,
            )
            return False
        finally:
            libc.setns(saved_ns.fileno(), CLONE_NEWNET)
            saved_ns.close()

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
                br_links = ipr.link("get", ifname=br_name)
                br_idx = br_links[0]["index"]
                ipr.link("set", index=br_idx, state="up")

                veth_links = ipr.link("get", ifname=host_veth)
                ipr.link("set", index=veth_links[0]["index"], master=br_idx)

                hw_links = ipr.link("get", ifname=hw_iface)
                ipr.link("set", index=hw_links[0]["index"], master=br_idx)
                ipr.link("set", index=hw_links[0]["index"], state="up")

            self._hil_bridges[gnd_name] = br_name
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
            return False

    def _apply_qos(self, sat_name: str, sat_pid: int, gnd_id: int, delay_ms: int, rate_kbps: int) -> None:
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
        """
        sat_iface = f"eth{gnd_id}"
        delay_ms = max(delay_ms, 1)
        rate_mbit = max(rate_kbps / 1000, 0.001)
        burst_bytes = max(rate_kbps * delay_ms // 8, 1500)

        try:
            _run_tc_batch(
                sat_pid,
                [
                    f"qdisc add dev {sat_iface} root handle 1: netem delay {delay_ms}ms",
                    f"qdisc add dev {sat_iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
            )
        except RuntimeError as exc:
            logging.warning("HIL: qdisc setup failed on %s in %s: %s", sat_iface, sat_name, exc)

    def _teardown_bridge(self, gnd_name: str) -> None:
        """
        Delete the Linux bridge for a HIL ground station.

        Deleting the bridge automatically releases all enslaved interfaces.
        The hardware interface returns to an unmastered state and is ready
        for the next handover. The host-side veth is removed by the kernel
        when the SAT-side veth is subsequently deleted.

        Args:
            gnd_name: Ground station node name (e.g. "Gnd0").
        """
        br_name = self._hil_bridges.pop(gnd_name, None)
        self._hil_veths.pop(gnd_name, None)

        if not br_name:
            return

        try:
            with IPRoute() as ipr:
                br_links = ipr.link("get", ifname=br_name)
                if br_links:
                    ipr.link("del", index=br_links[0]["index"])
            logging.info("HIL: bridge %s torn down for %s", br_name, gnd_name)
        except Exception as exc:
            logging.error("HIL: failed to teardown bridge %s: %s", br_name, exc)

    def _delete_sat_veth(self, sat_name: str, sat_pid: int, gnd_id: int) -> None:
        """
        Delete the SAT-side veth interface.

        The kernel automatically removes the host-side peer (hil{gnd_id})
        when the SAT-side interface is deleted.

        Args:
            sat_name: Satellite node name, used for logging.
            sat_pid: Kernel PID of the satellite container process.
            gnd_id: Numeric ID of the ground station node.
        """
        sat_iface = f"eth{gnd_id}"
        r = subprocess.run(
            ["nsenter", f"--net=/proc/{sat_pid}/ns/net", "ip", "link", "delete", sat_iface],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode != 0:
            logging.error(
                "HIL: failed to delete veth %s in %s: %s",
                sat_iface,
                sat_name,
                r.stderr.strip(),
            )
        else:
            logging.debug("HIL: deleted veth %s in %s", sat_iface, sat_name)

    def setup_link(self, link) -> None:
        """
        Set up a HIL link: create veth, bridge, and SAT-side qdiscs.

        Called by TopologyManager for HIL ground station links during
        initial wiring (wire_links) and on handover arrival (add_link).

        Requires node.container_pid to be set on the satellite node,
        which any launcher sets during start_containers.

        Args:
            link: A GroundStationLink whose ground station endpoint is
                listed in gnd_hardware_map.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = int(getattr(sat, "container_pid", 0) or 0)

        if not sat_pid:
            logging.error(
                "HIL: no container_pid on %s, cannot set up link to %s",
                sat.name,
                gnd.name,
            )
            return

        if not self._create_veth(sat.name, sat_pid, gnd.id):
            return
        if not self._create_bridge(gnd.name, gnd.id):
            return

        delay_ms = max(int(link.delay), 1)
        rate_kbps = self._link_capacity_kbps(link)
        self._apply_qos(sat.name, sat_pid, gnd.id, delay_ms, rate_kbps)

    def teardown_link(self, link) -> None:
        """
        Tear down a HIL link: delete bridge and SAT-side veth.

        Called by TopologyManager on handover departure (delete_link).
        The bridge is deleted first to release the hardware interface cleanly.
        The SAT-side veth deletion cascades to the host-side peer.

        Args:
            link: A GroundStationLink whose ground station endpoint is
                listed in gnd_hardware_map.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = int(getattr(sat, "container_pid", 0) or 0)

        self._teardown_bridge(gnd.name)

        if not sat_pid:
            logging.error(
                "HIL: no container_pid on %s, cannot delete veth for %s",
                sat.name,
                gnd.name,
            )
            return

        self._delete_sat_veth(sat.name, sat_pid, gnd.id)

    def update_link(self, link) -> None:
        """
        Update SAT-side QoS for a HIL link.

        Called by TopologyManager at each simulation step when orbital
        parameters change. Only the satellite container interface carries
        emulated qdiscs; the hardware side is unmodified.

        Args:
            link: A GroundStationLink with updated delay and capacity values.
        """
        gnd = self._get_hil_gnd(link)
        sat = self._get_sat(link)
        sat_pid = int(getattr(sat, "container_pid", 0) or 0)
        sat_iface = f"eth{gnd.id}"

        if not sat_pid:
            logging.warning(
                "HIL: no container_pid on %s, cannot update QoS on %s",
                sat.name,
                sat_iface,
            )
            return

        delay_ms = max(int(link.delay), 1)
        rate_kbps = self._link_capacity_kbps(link)
        rate_mbit = max(rate_kbps / 1000, 0.001)
        burst_bytes = max(rate_kbps * delay_ms // 8, 1500)

        try:
            _run_tc_batch(
                sat_pid,
                [
                    f"qdisc change dev {sat_iface} root handle 1: netem delay {delay_ms}ms",
                    f"qdisc change dev {sat_iface} parent 1:1 handle 10: tbf"
                    f" rate {rate_mbit:.3f}mbit burst {burst_bytes}b limit {burst_bytes}b",
                ],
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

    def wire_links(self, links: list) -> None:
        """
        Set up all active HIL links for initial wiring.

        Called by TopologyManager after the launcher's wire_links completes,
        for the subset of links involving HIL ground stations.

        Args:
            links: HIL GroundStationLink objects that are currently active.
        """
        for link in links:
            self.setup_link(link)

    def teardown_all(self) -> None:
        """
        Tear down all active HIL bridges.

        Called by TopologyManager during project teardown, before the launcher
        removes satellite containers. Deleting bridges releases hardware
        interfaces cleanly. Container removal cascades to veth peer deletion.
        """
        for gnd_name in list(self._hil_bridges.keys()):
            self._teardown_bridge(gnd_name)
