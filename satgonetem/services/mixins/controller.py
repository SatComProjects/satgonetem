import logging
from typing import Optional


import subprocess

from satgonetem.utils.ip_utils import IPUtils


class ControllerMixin:
    # ------------------------------------------------------------------
    # Controller connection helpers
    # ------------------------------------------------------------------

    def _remove_controller_veth(
        self,
        veth_host: str,
        veth_peer: str,
        bridge: str,
        ovs_cid: Optional[str] = None,
    ) -> None:
        """Idempotently tear down a controller veth pair.

        Removes *veth_peer* from the OVS *bridge* inside *ovs_cid* when the
        container ID is known, then deletes *veth_host* (which also destroys
        the peer).
        """
        if ovs_cid:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    ovs_cid,
                    "ovs-vsctl",
                    "--if-exists",
                    "del-port",
                    bridge,
                    veth_peer,
                ],
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["sudo", "ip", "link", "delete", veth_host],
            capture_output=True,
            text=True,
        )

    def setup_controller_connection(self) -> None:
        """Create a veth pair linking the host to the Controller OVS switch.

        The host side is brought up and addressed; the peer side is moved
        into the OVS container namespace, attached to the Controller bridge,
        and brought up.

        Raises:
            RuntimeError: If the OVS container cannot be found or any setup
                step fails.  A best-effort rollback is attempted on failure.
        """
        veth_host = self.controller_veth_host
        veth_peer = self.controller_veth_peer
        bridge = self.controller_bridge_name
        host_ip = self.controller_host_ip
        prefix = self.controller_subnet_prefix

        result = subprocess.run(
            ["docker", "ps", "--filter", "name=.ovs", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(
                "Could not find an OVS container (docker filter 'name=.ovs' returned nothing)"
            )
        lines = result.stdout.strip().splitlines()
        ovs_cid = lines[0]
        if len(lines) > 1:
            logging.warning(
                "Multiple OVS containers matched '.ovs'; using first match (%s)",
                ovs_cid,
            )

        try:
            subprocess.run(
                [
                    "sudo",
                    "ip",
                    "link",
                    "add",
                    veth_host,
                    "type",
                    "veth",
                    "peer",
                    "name",
                    veth_peer,
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            subprocess.run(
                ["sudo", "ip", "link", "set", veth_host, "up"],
                capture_output=True,
                text=True,
                check=True,
            )

            pid_result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Pid}}", ovs_cid],
                capture_output=True,
                text=True,
                check=True,
            )
            pid = pid_result.stdout.strip()
            subprocess.run(
                ["sudo", "ip", "link", "set", veth_peer, "netns", pid],
                capture_output=True,
                text=True,
                check=True,
            )

            subprocess.run(
                ["docker", "exec", ovs_cid, "ip", "link", "set", veth_peer, "up"],
                capture_output=True,
                text=True,
                check=True,
            )

            subprocess.run(
                [
                    "docker",
                    "exec",
                    ovs_cid,
                    "ovs-vsctl",
                    "--may-exist",
                    "add-port",
                    bridge,
                    veth_peer,
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            subprocess.run(
                ["sudo", "ip", "addr", "add", f"{host_ip}/{prefix}", "dev", veth_host],
                capture_output=True,
                text=True,
                check=True,
            )

            logging.info(
                "Controller connection established: %s <-> %s "
                "(OVS container %s, bridge %s, host IP %s/%d)",
                veth_host,
                veth_peer,
                ovs_cid,
                bridge,
                host_ip,
                prefix,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.strip() if exc.stderr else ""
            logging.error(
                "Controller connection setup failed at step %s: %s",
                exc.cmd,
                stderr,
            )
            try:
                self._remove_controller_veth(veth_host, veth_peer, bridge, ovs_cid)
            except Exception as rollback_exc:
                logging.warning(
                    "Controller connection rollback also failed: %s", rollback_exc
                )
            raise RuntimeError(
                f"Failed to set up controller connection: {stderr or exc}"
            ) from exc

    def setup_controller_addressing(self) -> dict[str, str]:
        """Assign controller-plane IP addresses to all satellites and ground stations.

        Each node receives a unique address derived from its numeric ID on the
        interface defined by ``controller_node_iface_pattern``.

        Returns:
            Mapping from node name to assigned IPv4 address.

        Raises:
            RuntimeError: If one or more nodes could not be addressed.
        """
        prefix = self.controller_subnet_prefix
        iface_pattern = self.controller_node_iface_pattern

        controller_ips: dict[str, str] = {}
        failures: list[str] = []

        for node in list(self.get_satellites()) + list(self.get_ground_stations()):
            try:
                ip = self.build_controller_ip(node.id)
                iface = iface_pattern.format(node_id=node.id)
                node.execute_command(f"ip addr add {ip}/{prefix} dev {iface}")
                controller_ips[node.name] = ip
                logging.debug(
                    "Assigned controller IP %s/%d to %s on %s",
                    ip,
                    prefix,
                    node.name,
                    iface,
                )
            except Exception as exc:
                logging.error(
                    "Failed to assign controller IP to %s: %s",
                    node.name,
                    exc,
                )
                failures.append(node.name)

        if failures:
            raise RuntimeError(
                f"Controller addressing failed for {len(failures)} node(s): "
                f"{', '.join(failures)}"
            )

        logging.info(
            "Controller addressing complete for %d node(s)",
            len(controller_ips),
        )
        return controller_ips

    def build_controller_ip(self, node_id: int) -> str:
        """Build a controller-plane IPv4 address from a node ID.

        The bit layout is fixed:

          - 5 leading ``1`` bits  → first octet 248
          - 13 zero bits
          - 13 bits for *node_id*
          - 1 trailing ``1`` bit

        Args:
            node_id: Numeric node identifier. Must fit in 13 bits (0..8191).

        Returns:
            Quad-dotted IPv4 address string.

        Raises:
            ValueError: If *node_id* is negative or exceeds 13 bits.
        """
        if not isinstance(node_id, int) or node_id < 0 or node_id > 0x1FFF:
            raise ValueError(
                f"node_id must be an integer in [0, 8191], got {node_id!r}"
            )
        binary = "11111" + f"{0:0>13b}" + f"{node_id:0>13b}" + "1"
        return IPUtils.quaddot(binary)

    def build_satellite_ip(self, satellite_id: int) -> str:
        """Deprecated alias for :meth:`build_controller_ip`."""
        return self.build_controller_ip(satellite_id)

    def cleanup_controller_connection(self) -> None:
        """Remove the controller veth pair and detach it from the OVS bridge."""
        veth_host = self.controller_veth_host
        veth_peer = self.controller_veth_peer
        bridge = self.controller_bridge_name

        result = subprocess.run(
            ["docker", "ps", "--filter", "name=.ovs", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
        )
        ovs_cid = None
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            ovs_cid = lines[0]
            if len(lines) > 1:
                logging.warning(
                    "Multiple OVS containers matched '.ovs'; using first match (%s)",
                    ovs_cid,
                )

        self._remove_controller_veth(veth_host, veth_peer, bridge, ovs_cid)
        logging.info("Controller connection cleaned up (%s)", veth_host)

    def get_management_ip(self, node_id: int) -> str:
        """Get the management IP address for a given node ID."""
        return self.build_controller_ip(node_id)

    def get_management_interface(self, node_id: int) -> str:
        """Get the management interface for a given node ID."""
        return "eth" + str(50000 + node_id)
