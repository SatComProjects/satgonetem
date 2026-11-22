"""Optional backend-control operations for ``TopologyManager``.

This mixin is composed into ``TopologyManager`` by the integrator. It keeps
backend capabilities optional: every method feature-detects the corresponding
launcher method and raises ``RuntimeError`` when the active backend does not
implement it. ``NetworkLauncher`` therefore does not grow abstract methods for
operations only some backends can perform.

State caveat
------------
A raw restart, stop, or start changes container state only. Addresses,
routes, qdisc administration, and the interface/qdisc wiring must be
reapplied afterwards (for example by the normal routing and link setup path)
before traffic flows again.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

import grpc

from satgonetem.models.link import validate_admin_up, validate_jitter, validate_loss
from satgonetem.proto import netem_pb2
from satgonetem.services.mixins.simulation_loop import ensure_simulation_lock

if TYPE_CHECKING:
    from satgonetem.models.interface import Interface
    from satgonetem.models.link import Link


class BackendControlMixin:
    """Node, interface, link, and project controls for a capable backend."""

    launcher: Any
    satellites: dict
    ground_stations: dict

    # ------------------------------------------------------------------
    # Backend lookup and local state helpers
    # ------------------------------------------------------------------

    def _control_backend(self, operation: str) -> Any:
        """Return the launcher method named *operation*.

        Raises:
            RuntimeError: If no launcher is active or it does not implement
                the requested control operation.
        """
        launcher = getattr(self, "launcher", None)
        if launcher is None:
            raise RuntimeError(f"No network launcher is active; cannot {operation}")
        method = getattr(launcher, operation, None)
        if method is None:
            raise RuntimeError(
                f"{type(launcher).__name__} does not support {operation}"
            )
        return method

    def _find_node(self, node_name: str) -> Any:
        """Return the satellite or ground station named *node_name*."""
        for collection_name in ("satellites", "ground_stations"):
            collection = getattr(self, collection_name, {}) or {}
            for node in collection.values():
                if getattr(node, "name", None) == node_name:
                    return node
        raise ValueError(f"Unknown node {node_name!r}")

    @staticmethod
    def _resolve_interface(node: Any, interface: Any) -> Any:
        """Accept an Interface object or resolve a name/``ethN`` string."""
        if not isinstance(interface, str):
            return interface

        candidates = list(getattr(node, "interfaces", []))
        loopback = getattr(node, "loopback", None)
        if loopback is not None:
            candidates.append(loopback)
        for candidate in candidates:
            try:
                iname = candidate.get_iname()
            except (AttributeError, ValueError):
                iname = None
            if candidate.name == interface or iname == interface:
                return candidate
        raise ValueError(f"Node {node.name!r} has no interface {interface!r}")

    @staticmethod
    def _interface_index(interface: Interface) -> int:
        """Map an interface to the GoNetEm ``ifIndex`` (the ``ethN`` suffix).

        Raises:
            ValueError: If the interface cannot be expressed as
                ``eth<digits>`` (for example a loopback or a malformed name).
        """
        try:
            iname = interface.get_iname()
        except (AttributeError, ValueError) as exc:
            name = getattr(interface, "name", interface)
            raise ValueError(
                f"Cannot control interface {name!r}: expected an "
                f"'eth<digits>' name ({exc})"
            ) from exc
        if not iname.startswith("eth") or not iname[3:].isdigit():
            raise ValueError(
                f"Cannot control interface {interface.name!r}: expected an "
                f"'eth<digits>' name, got {iname!r}"
            )
        return int(iname[3:])

    def _endpoint_interfaces(self, link: Link) -> list[tuple[Any, Any]]:
        """Return ``(node, interface)`` for both ends of *link*.

        Raises:
            RuntimeError: If an endpoint interface is not attached. Sending a
                partial state change to the backend is worse than failing.
        """
        try:
            source_name, target_name = link.get_names()
        except AttributeError:
            source_name = f"{link.source.name}.{link.target.id}"
            target_name = f"{link.target.name}.{link.source.id}"

        peer_interfaces = list(getattr(link, "peer_interfaces", []))
        pairs: list[tuple[Any, Any]] = []
        for node, expected in (
            (link.source, source_name),
            (link.target, target_name),
        ):
            interface = next(
                (
                    candidate
                    for candidate in getattr(node, "interfaces", [])
                    if candidate.name == expected
                ),
                None,
            )
            if interface is None:
                interface = next(
                    (
                        candidate
                        for candidate in peer_interfaces
                        if candidate.name == expected
                    ),
                    None,
                )
            if interface is None:
                raise RuntimeError(
                    f"Link endpoint interface {expected!r} is not attached "
                    f"to node {node.name!r}"
                )
            pairs.append((node, interface))
        return pairs

    @staticmethod
    def _link_label(link: Link) -> str:
        source = getattr(getattr(link, "source", None), "name", "?")
        target = getattr(getattr(link, "target", None), "name", "?")
        return f"{source}<->{target}"

    @staticmethod
    def _atomic_write(path: Any, data: bytes) -> None:
        """Write *data* to *path* with a temp file and ``os.replace``."""
        target = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(target))
        fd, temp_path = tempfile.mkstemp(prefix=".satgonetem-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise

    # ------------------------------------------------------------------
    # Node lifecycle
    # ------------------------------------------------------------------

    def restart_node(self, node_name: str) -> None:
        """Restart one node container and clear its stale local handle.

        The container handle is cleared even when the RPC raises: after a
        restart attempt the old handle cannot be trusted. The container comes
        back without addresses, routes, or qdiscs, so the caller must reapply
        them before traffic flows again.
        """
        with ensure_simulation_lock(self):
            restart = self._control_backend("restart_node")
            node = self._find_node(node_name)
            try:
                restart(node_name)
            finally:
                node.container = None
                node.container_pid = None

    def stop_all_nodes(self) -> None:
        """Stop the simulation loop, then stop every node container.

        The simulation lock is deliberately not held across ``stop()``: the
        loop must be able to finish its in-flight tick and join.
        """
        stop_backend = self._control_backend("stop_all_nodes")
        get_running = getattr(self, "get_running", None)
        if get_running is not None and get_running():
            stop_loop = getattr(self, "stop", None)
            if stop_loop is None:
                raise RuntimeError("Simulation loop is running but cannot be stopped")
            stop_loop()
        # The loop has already joined; serialize the backend call itself
        # without holding the lock across stop()/join().
        with ensure_simulation_lock(self):
            stop_backend()

    def start_all_nodes(self) -> None:
        """Start every node container.

        Addresses, routes, and qdiscs must be reapplied afterwards.
        """
        with ensure_simulation_lock(self):
            start_backend = self._control_backend("start_all_nodes")
            start_backend()

    # ------------------------------------------------------------------
    # Interface and link state
    # ------------------------------------------------------------------

    def set_interface_state(self, node_name: str, interface: Any, up: bool) -> None:
        """Bring one interface up or down through the backend.

        Args:
            node_name: Node that owns the interface.
            interface: ``Interface`` instance, emulator name, or ``ethN``.
            up: Whether the interface should be administratively up.
        """
        if not isinstance(up, bool):
            raise TypeError(f"set_interface_state up must be a bool, got {up!r}")
        with ensure_simulation_lock(self):
            node = self._find_node(node_name)
            resolved = self._resolve_interface(node, interface)
            if_index = self._interface_index(resolved)
            set_state = self._control_backend("set_interface_state")
            set_state(node_name, if_index, up)

    def set_link_state(self, link: Link, up: bool) -> None:
        """Bring both endpoints of *link* up or down and pin the admin state.

        ``Link.admin_up`` keeps the next topology tick from overwriting the
        interface state; ``Link.update_interfaces_state()`` ANDs it with the
        satcom-derived ``is_active`` value.

        The backend calls are made before any local state is committed. If
        either endpoint RPC fails, the previous ``admin_up``/interface state is
        left untouched and the exception is re-raised.
        """
        up = validate_admin_up(up)
        with ensure_simulation_lock(self):
            pairs = self._endpoint_interfaces(link)
            set_state = self._control_backend("set_interface_state")
            previous_admin_up = link.admin_up
            previous_interface_states = [
                (interface, interface.is_active) for _node, interface in pairs
            ]
            applied: list[tuple[int, Any]] = []
            try:
                for index, (node, interface) in enumerate(pairs):
                    if_index = self._interface_index(interface)
                    set_state(node.name, if_index, up)
                    applied.append((index, node))
            except BaseException:
                link.admin_up = previous_admin_up
                for interface, previous_state in previous_interface_states:
                    interface.is_active = previous_state
                for index, node in reversed(applied):
                    interface = pairs[index][1]
                    try:
                        set_state(
                            node.name,
                            self._interface_index(interface),
                            previous_interface_states[index][1],
                        )
                    except Exception as exc:  # noqa: BLE001 - preserve primary error
                        logging.error(
                            "Failed to restore interface state for %s: %s",
                            node.name,
                            exc,
                        )
                raise

            link.admin_up = up
            active = bool(link.is_active and link.admin_up)
            for _node, interface in pairs:
                interface.is_active = active

    # ------------------------------------------------------------------
    # Capture, status, snapshot
    # ------------------------------------------------------------------

    def capture_interface(
        self,
        node_name: str,
        interface: Any,
        output: Any = None,
        timeout_s: float = 30.0,
    ) -> bytes:
        """Capture one interface and optionally write it to *output*.

        Returns:
            The captured pcap bytes, even when *output* is written.
        """
        node = self._find_node(node_name)
        resolved = self._resolve_interface(node, interface)
        if_index = self._interface_index(resolved)
        capture = self._control_backend("capture_interface")
        data = capture(node_name, if_index, timeout_s=timeout_s)
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError(
                f"capture_interface returned {type(data).__name__}, expected bytes"
            )
        payload = bytes(data)
        if output is not None:
            self._atomic_write(output, payload)
        return payload

    def get_backend_status(self) -> dict:
        """Return a JSON-serializable backend status dictionary."""
        status_backend = self._control_backend("get_backend_status")
        status = status_backend()
        if not isinstance(status, dict):
            raise RuntimeError(
                f"get_backend_status returned {type(status).__name__}, expected dict"
            )
        try:
            json.dumps(status)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"get_backend_status returned non-JSON-serializable data: {exc}"
            ) from exc
        return status

    def save_snapshot(self, path: Any) -> None:
        """Save the backend project archive to *path* atomically."""
        save_project = self._control_backend("save_project")
        data = save_project()
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise RuntimeError("save_project returned no project data")
        self._atomic_write(path, bytes(data))

    # ------------------------------------------------------------------
    # Link impairments
    # ------------------------------------------------------------------

    def set_link_impairment(
        self,
        link: Link,
        loss_percent: float | None = None,
        jitter_ms: int | None = None,
    ) -> None:
        """Set loss/jitter on *link* and push the update to the backend.

        A failed update leaves ``link.to_update`` set so the normal link sync
        retries it. ``launcher.update_link()`` is intentionally not used
        because it logs and swallows backend errors.

        Raises:
            ValueError: If no field is supplied or a value is out of range.
            TypeError: If a value has the wrong type.
            RuntimeError: If the backend has no ``update_links`` capability or
                reports an application error.
        """
        if loss_percent is None and jitter_ms is None:
            raise ValueError("set_link_impairment requires loss_percent or jitter_ms")

        with ensure_simulation_lock(self):
            new_loss = (
                validate_loss(loss_percent)
                if loss_percent is not None
                else getattr(link, "loss", 0.0)
            )
            new_jitter = (
                validate_jitter(jitter_ms)
                if jitter_ms is not None
                else getattr(link, "jitter", 0)
            )
            link.loss = new_loss
            link.jitter = new_jitter
            link.to_update = True

            label = self._link_label(link)
            update_links = self._control_backend("update_links")
            try:
                response = update_links([link])
            except grpc.RpcError as exc:
                raise RuntimeError(
                    f"Failed to apply link impairment to {label}: {exc}"
                ) from exc

            response_status = getattr(response, "status", None)
            if (
                response_status is not None
                and response_status.code != netem_pb2.StatusCode.OK
            ):
                detail = (
                    getattr(response_status, "error", "")
                    or "server returned a non-OK status"
                )
                raise RuntimeError(
                    f"Failed to apply link impairment to {label}: {detail}"
                )

            results = list(getattr(response, "results", ()))
            if len(results) != 1:
                raise RuntimeError(
                    f"update_links returned {len(results)} result(s) for 1 link"
                )
            result_status = getattr(results[0], "status", None)
            if result_status is None or result_status.code != netem_pb2.StatusCode.OK:
                detail = (
                    getattr(result_status, "error", "")
                    if result_status is not None
                    else ""
                )
                raise RuntimeError(
                    f"Failed to apply link impairment to {label}: "
                    f"{detail or 'server returned a non-OK status'}"
                )

            link.to_update = False
