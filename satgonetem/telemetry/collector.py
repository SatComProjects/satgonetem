"""Topology sampling for experiment metrics.

The collector deliberately depends on a small topology surface so it can be
used with a live :class:`TopologyManager` or a test double:

* ``links``, ``satellites``, ``ground_stations``
* ``current_time_step`` and ``update_time``
* optional ``monitor_links()`` and ``get_last_simulation_error()``
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any


class MetricsCollector:
    """Build JSON-serializable samples from a topology."""

    def __init__(self, topology: Any, usage_provider=None) -> None:
        self.topology = topology
        self._usage_provider = usage_provider

    def _link_usage(self) -> list[dict[str, Any]]:
        provider = self._usage_provider
        if provider is None:
            provider = getattr(self.topology, "monitor_links", None)
        if not callable(provider):
            return []
        try:
            return list(provider() or [])
        except Exception as exc:  # noqa: BLE001 - telemetry must not abort a run
            logging.warning("Link usage sampling failed: %s", exc)
            return []

    def _usage_map(self) -> dict[frozenset[str], int]:
        result: dict[frozenset[str], int] = {}
        for item in self._link_usage():
            if not isinstance(item, dict):
                continue
            src = item.get("src")
            dst = item.get("dst")
            if not src or not dst:
                continue
            try:
                value = int(item.get("value") or 0)
            except (TypeError, ValueError):
                value = 0
            result[frozenset((str(src), str(dst)))] = value
        return result

    def sample(self, flows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Return one metrics sample.

        Args:
            flows: Optional serialized flow results to include in the sample.
        """
        topology = self.topology
        step = int(getattr(topology, "current_time_step", 0) or 0)
        tick = getattr(topology, "update_time", 0) or 0
        try:
            sim_time_s = round(step * float(tick), 6)
        except (TypeError, ValueError):
            sim_time_s = 0.0

        usage = self._usage_map()
        links: list[dict[str, Any]] = []
        for link in getattr(topology, "links", {}).values():
            source = getattr(link, "source", None)
            target = getattr(link, "target", None)
            src = getattr(source, "name", "")
            dst = getattr(target, "name", "")
            key = frozenset((str(src), str(dst)))
            usage_value = usage.get(key)
            if usage_value is None:
                try:
                    usage_value = int(getattr(link, "tx", 0) or 0)
                except (TypeError, ValueError):
                    usage_value = 0
            try:
                rx_value = int(getattr(link, "rx", 0) or 0)
            except (TypeError, ValueError):
                rx_value = 0
            links.append(
                {
                    "src": src,
                    "dst": dst,
                    "type": getattr(link, "type", ""),
                    "active": bool(
                        getattr(link, "is_active", True)
                        and getattr(link, "admin_up", True)
                    ),
                    "admin_up": bool(getattr(link, "admin_up", True)),
                    "delay_ms": int(getattr(link, "delay", 0) or 0),
                    "capacity_kbps": int(
                        getattr(link, "get_capacity", lambda: 0)() or 0
                    ),
                    "loss_percent": float(getattr(link, "loss", 0.0) or 0.0),
                    "jitter_ms": int(getattr(link, "jitter", 0) or 0),
                    "tx_bps": usage_value,
                    "rx_bps": rx_value,
                }
            )

        error = None
        get_error = getattr(topology, "get_last_simulation_error", None)
        if callable(get_error):
            try:
                exc = get_error()
            except Exception:  # noqa: BLE001 - diagnostics only
                exc = None
            if exc is not None:
                error = str(exc)

        sample: dict[str, Any] = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "sim_time_s": sim_time_s,
            "topology": {
                "satellites": len(getattr(topology, "satellites", {}) or {}),
                "ground_stations": len(getattr(topology, "ground_stations", {}) or {}),
                "links": len(getattr(topology, "links", {}) or {}),
            },
            "links": links,
            "last_error": error,
        }
        if flows is not None:
            sample["flows"] = flows
        return sample
