"""NetworkLifecycleMixin for TopologyManager."""

from __future__ import annotations

import logging
import time
import warnings
from typing import TYPE_CHECKING, Any

from satgonetem.services.mixins.simulation_loop import ensure_simulation_lock
from satgonetem.utils.constants import MAX_WORKERS

if TYPE_CHECKING:
    from satgonetem.routing.base_daemon import RoutingDaemon


# Maximum time to wait for one traffic flow thread during shutdown.
_FLOW_STOP_TIMEOUT_SEC = 5.0


class NetworkLifecycleMixin:
    """NetworkLifecycle functionality."""

    routing_daemon: RoutingDaemon | None
    project_id: str | None
    client: Any
    request: Any
    # A previous stop retained the launcher after a failed forced close; the
    # next stop must retry the forced path because the graceful client is gone.
    _pending_force_close: bool

    def start_gonetem(self, controller: bool = False) -> tuple[float, float] | None:
        """Start GoNetEm by launching containers and wiring links.

        Returns:
            tuple[float, float]: Container and link setup times, in seconds.
            None: If the emulator is already running (no-op).
        """
        if self.get_gonetem_status():
            return None

        retained_launcher = getattr(self, "launcher", None)
        if retained_launcher is not None:
            raise RuntimeError(
                "A GoNetEm project is still retained from a failed stop; "
                "call stop_gonetem() to retry before starting again."
            )
        # A fresh start has no retained project to force-close.
        self._pending_force_close = False

        from satgonetem.launchers.registry import create_launcher

        launcher = create_launcher(
            getattr(self, "network_launcher", "GONETEM"),
            topology_manager=self,
            server_address=self.gonetem_server,
            project_name=self.project_name,
            isl_capacity_kbps=self.isl_link_capacity,
            gnd_capacity_kbps=self.gnd_link_capacity,
            ground_object_capacity_kbps=getattr(
                self, "ground_object_link_capacity", self.gnd_link_capacity
            ),
            netem_limit=getattr(self, "link_netem_limit", 1000),
            txqueuelen=getattr(self, "link_txqueuelen", 1000),
        )
        # Assign before startup so a failed launch can still be torn down.
        self.launcher = launcher

        all_nodes = list(self.satellites.values()) + list(self.ground_stations.values())
        active_links = [lnk for lnk in self.links.values() if lnk.is_active]

        hil = self.hil_manager
        if hil is not None:
            launch_nodes = [n for n in all_nodes if not hil.is_hil_node(n.name)]
            launch_links = [lnk for lnk in active_links if not hil.is_hil_link(lnk)]
            hil_links = [lnk for lnk in active_links if hil.is_hil_link(lnk)]
        else:
            launch_nodes = all_nodes
            launch_links = active_links
            hil_links = []

        try:
            container_time, link_time = launcher.start_containers(
                launch_nodes,
                MAX_WORKERS,
                controller=controller,
                links=launch_links,
            )
            launcher.wire_links(launch_links, MAX_WORKERS)

            if hil is not None:
                hil.wire_links(hil_links)
        except Exception:
            # Startup failed after the project may have been opened. Close it
            # best-effort and reset state so a retry does not leak a project.
            cleanup_failed = False
            try:
                cleanup_confirmed = launcher.force_close_project() is True
            except Exception as cleanup_exc:
                logging.error(
                    "Failed to force-close GoNetEm project after startup failure: %s",
                    cleanup_exc,
                )
            else:
                cleanup_failed = not cleanup_confirmed
            if cleanup_failed:
                # Keep the handle so stop_gonetem() can retry the forced close
                # instead of leaking the project silently.
                self._pending_force_close = True
                logging.error(
                    "Force-close after startup failure did not confirm cleanup; "
                    "retaining the launcher for stop_gonetem() to retry"
                )
            else:
                self.launcher = None
                self._pending_force_close = False
            self.set_status(False)
            self.set_gonetem_status(False)
            self.set_routing_initiated(False)
            raise

        self.set_status(True)
        self.set_gonetem_status(True)
        self.start_time_ = time.time()

        return (container_time, link_time)

    def _stop_simulation_loop(self) -> None:
        """Stop the background simulation loop if it is running."""
        if self.get_running() or (
            hasattr(self, "_sim_thread")
            and self._sim_thread is not None
            and self._sim_thread.is_alive()
        ):
            self.stop()

    def _stop_active_flows(self) -> None:
        """Stop active traffic flows with a bounded wait.

        A single failing flow must not prevent container/project teardown.
        Flows whose thread is still alive after the bounded wait are retained
        in ``_active_flows`` so a later stop attempt can retry them.
        """
        flows = list(getattr(self, "_active_flows", []))
        stuck: list = []
        for flow in flows:
            try:
                status = getattr(flow, "status", lambda: None)()
                active = status is not None and getattr(
                    status, "name", str(status)
                ) in ("RUNNING", "PENDING")
            except Exception as exc:
                logging.warning(
                    "Failed to inspect traffic flow during shutdown: %s", exc
                )
                # The flow may still be running; attempt to stop it below.
                active = True
            if not active:
                continue

            stop = getattr(flow, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:
                    logging.warning(
                        "Failed to stop traffic flow during shutdown: %s", exc
                    )

            thread = getattr(flow, "_thread", None)
            try:
                if thread is not None and thread.is_alive():
                    thread.join(timeout=_FLOW_STOP_TIMEOUT_SEC)
                    if thread.is_alive():
                        logging.warning(
                            "Traffic flow thread did not stop within %.1fs; "
                            "retaining it for a later stop attempt",
                            _FLOW_STOP_TIMEOUT_SEC,
                        )
                        stuck.append(flow)
            except Exception as exc:
                logging.warning(
                    "Failed to inspect traffic flow thread during shutdown: %s", exc
                )
                # Without a liveness answer, keep the flow tracked so it can be
                # retried instead of silently leaking a running command.
                stuck.append(flow)

        if hasattr(self, "_active_flows"):
            self._active_flows[:] = stuck

    def _stop_routing_daemon(self) -> None:
        """Best-effort routing teardown before the project is closed.

        Routes live inside the project containers, so the daemon is removed
        while the containers still exist and then dropped regardless of the
        removal outcome. The simulation lock serializes the removal with
        ``init_routing()``/``delete_routing()`` and in-flight ticks.
        """
        with ensure_simulation_lock(self):
            daemon = getattr(self, "routing_daemon", None)
            if daemon is not None:
                try:
                    daemon.remove(max_workers=MAX_WORKERS)
                except Exception as exc:
                    logging.error(
                        "Failed to remove routing daemon during stop: %s", exc
                    )
                finally:
                    self.routing_daemon = None
            self.set_routing_initiated(False)

    def stop_gonetem(self) -> float:
        """Stop GoNetEm and clean up resources.

        Gracefully stops any active background activity (simulation loop,
        routing, tcpdump, traffic flows) before tearing down the network.

        Returns:
            float: Time taken to stop GoNetEm
        """
        tic = time.perf_counter()

        self._stop_simulation_loop()
        self._stop_active_flows()

        self.set_status(False)

        launcher = getattr(self, "launcher", None)
        pending_force_close = bool(getattr(self, "_pending_force_close", False))
        teardown_error: Exception | None = None
        close_error: Exception | None = None
        launcher_retained = False

        self._stop_routing_daemon()

        try:
            if self.hil_manager is not None:
                self.hil_manager.teardown_all()
        except Exception as exc:
            teardown_error = exc
            logging.error("HIL teardown failed during stop: %s", exc)

        if pending_force_close and launcher is not None:
            # The previous stop already attempted the graceful close, which
            # may have dropped the client. Retry the forced path directly.
            forced = False
            try:
                forced = launcher.force_close_project() is True
            except Exception as force_exc:
                logging.error("Retried force-close failed during stop: %s", force_exc)
            if not forced:
                logging.error(
                    "Retried force-close did not confirm cleanup during stop; "
                    "retaining project handles for another retry"
                )
                launcher_retained = True
                close_error = RuntimeError(
                    "GoNetEm project force-close did not confirm cleanup; "
                    "project handles were retained for another stop_gonetem() retry"
                )
        else:
            try:
                if launcher is not None:
                    launcher.close_project()
            except Exception as exc:
                close_error = exc
                logging.error("Failed to close GoNetEm project during stop: %s", exc)
                force_close = getattr(launcher, "force_close_project", None)
                fallback_succeeded = False
                if callable(force_close):
                    try:
                        fallback_succeeded = force_close() is True
                    except Exception as force_exc:
                        logging.error(
                            "Force-close fallback failed during stop: %s", force_exc
                        )
                if not fallback_succeeded:
                    logging.error(
                        "Force-close fallback did not confirm cleanup during stop; "
                        "retaining project handles for a retry"
                    )
                    launcher_retained = True

        # A failed stop must not leave the manager in a state where
        # start_gonetem() silently refuses to run (CORE-07). Keep the launcher
        # itself only when neither graceful nor forced cleanup succeeded so a
        # retry can target the same project.
        self.set_gonetem_status(False)
        self.set_routing_initiated(False)
        self.routing_method = None
        self._pending_force_close = launcher_retained
        if not launcher_retained:
            self.launcher = None
            self.project_id = None
            self.client = None
            self.request = None

        if teardown_error is not None:
            raise teardown_error
        if launcher_retained and close_error is not None:
            raise close_error

        return time.perf_counter() - tic

    def force_stop_gonetem(self) -> float:
        """Force stop GoNetEm without graceful cleanup.

        This method can be used in scenarios where the normal stop_gonetem process fails
        or when a quick reset is needed. It will attempt to kill all containers and clean
        up resources without waiting for graceful shutdown.

        Returns:
            float: Time taken to force stop GoNetEm
        """
        warnings.warn(
            "force_stop_gonetem does not clean up properly; "
            "prefer stop_gonetem() for a clean shutdown.",
            ResourceWarning,
            stacklevel=2,
        )
        tic = time.perf_counter()

        teardown_error: Exception | None = None
        try:
            # Stop the simulation loop and traffic flows before closing the
            # project so no background thread mutates a closed topology.
            try:
                self._stop_simulation_loop()
            except Exception as exc:
                logging.error(
                    "Failed to stop simulation loop during force stop: %s", exc
                )

            self.set_status(False)

            try:
                self._stop_active_flows()
            except Exception as exc:
                logging.error("Failed to stop traffic flows during force stop: %s", exc)

            self._stop_routing_daemon()

            if self.hil_manager is not None:
                try:
                    self.hil_manager.teardown_all()
                except Exception as exc:
                    teardown_error = exc
                    logging.error("HIL teardown failed during force stop: %s", exc)

            launcher = getattr(self, "launcher", None)
            if launcher is not None:
                try:
                    launcher.force_close_project()
                except Exception as exc:
                    logging.error(
                        "Failed to force-close GoNetEm project during force stop: %s",
                        exc,
                    )
        finally:
            # A failed force stop must not leave stale state behind.
            self.launcher = None
            self.routing_daemon = None
            self.project_id = None
            self.client = None
            self.request = None
            self._pending_force_close = False
            self.set_status(False)
            self.set_gonetem_status(False)
            self.set_routing_initiated(False)
            self.routing_method = None

        if teardown_error is not None:
            raise teardown_error

        return time.perf_counter() - tic

    def set_ip_addresses(
        self, satellites: bool = True, ground_stations: bool = True
    ) -> float:
        """Set the planned addresses for all interfaces in the topology.

        IPv4 is always pushed.  When IPv6 is enabled, IPv6 is pushed too; both
        families are attempted even if one of them fails, and the failures are
        re-raised after both attempts.

        Args:
            satellites:  Optinal flag to assign addresses to satellites (defaults to True)
            ground_stations: Optional flag to assign addresses to ground stations (defaults to True)

        """
        if getattr(self, "address_plan", None) is None:
            raise RuntimeError(
                "IPv4 address plan is not initialized; construct the "
                "TopologyManager from a topology before assigning addresses"
            )

        tic: float = time.perf_counter()

        ipv4_error: Exception | None = None
        try:
            self.set_ipv4s_for_all_nodes(
                set_lo=True,
                max_workers=MAX_WORKERS,
                satellites=satellites,
                ground_stations=ground_stations,
            )
        except Exception as exc:  # noqa: BLE001 - attempted after IPv6 push
            ipv4_error = exc
            logging.exception("IPv4 address assignment failed")

        ipv6_error: Exception | None = None
        if getattr(self, "ipv6_enabled", False):
            if getattr(self, "address_plan_v6", None) is None:
                ipv6_error = RuntimeError(
                    "IPv6 address plan is not initialized; construct the "
                    "TopologyManager from a topology before assigning addresses"
                )
            else:
                try:
                    self.set_ipv6s_for_all_nodes(
                        set_lo=True,
                        max_workers=MAX_WORKERS,
                        satellites=satellites,
                        ground_stations=ground_stations,
                    )
                except Exception as exc:  # noqa: BLE001 - aggregate both families
                    ipv6_error = exc
                    logging.exception("IPv6 address assignment failed")

        if ipv4_error is not None and ipv6_error is not None:
            raise RuntimeError(
                "Address assignment failed for both families: "
                f"IPv4: {ipv4_error}; IPv6: {ipv6_error}"
            ) from ipv4_error
        if ipv4_error is not None:
            raise ipv4_error
        if ipv6_error is not None:
            raise ipv6_error

        return time.perf_counter() - tic

    def fast_start(self, routing_method: str = "static") -> None:
        """Start GoNetEm with a fast startup sequence for rapid testing iterations.

        This method performs a streamlined startup process that skips some of the
        more time-consuming steps like waiting for containers to be fully ready or
        launching tcpdump. It is intended for use in development and testing scenarios
        where quick feedback is more valuable than a fully initialized environment.

        Args:
            routing_method: Optional routing method to initialize (default: 'static').
                            Must be one of the allowed routing methods.
        """
        self.start_gonetem()
        self.set_ip_addresses()
        self.init_routing(routing_method=routing_method)
