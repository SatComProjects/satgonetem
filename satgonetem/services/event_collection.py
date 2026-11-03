"""Event collection and link-delta coalescing for the V5 event path.

The V5 EventBus delivers events from the simulation thread (or from an asyncio
task when a loop is running). The emulator never mutates its topology from an
event handler: the handler only appends to :class:`EventCollector`, and the
simulation tick drains the queue and applies one coalesced delta.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Literal, Optional

LinkKey = frozenset[str]
LinkAction = Literal["created", "removed"]

logger = logging.getLogger(__name__)


class EventCollector:
    """Thread-safe, append-only queue of topology events.

    Handlers are intentionally minimal: they must not block, log per event, or
    touch emulator state. A broken event is counted and dropped rather than
    failing the simulation tick.
    """

    def __init__(self) -> None:
        self._events: Deque[Any] = deque()
        self._lock = threading.Lock()
        self._dropped = 0

    async def handle(self, event: Any) -> None:
        """Append one event; never raises into the V5 EventBus."""
        try:
            with self._lock:
                self._events.append(event)
        except Exception:
            with self._lock:
                self._dropped += 1
            logger.exception("Failed to collect topology event %r", event)

    def drain(self) -> list[Any]:
        """Return the collected events in arrival order and clear the queue."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    @property
    def dropped(self) -> int:
        """Number of events dropped because collection failed."""
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


@dataclass
class LinkDeltaResult:
    """Coalesced link changes for one tick."""

    created: Dict[LinkKey, Any] = field(default_factory=dict)
    updated: Dict[LinkKey, Any] = field(default_factory=dict)
    removed: Dict[LinkKey, Any] = field(default_factory=dict)
    ignored: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.created or self.updated or self.removed)


class LinkDelta:
    """Coalesce multiple events for the same link.

    Only the last event per link matters. The final state is then interpreted
    against the links that existed at the start of the tick:

    - final ``created`` for a new link -> add it;
    - final ``created`` for an existing link -> treat it as an update, so a
      remove+recreate in one tick does not churn the container interfaces;
    - final ``removed`` for an existing link -> remove it;
    - final ``removed`` for a link that never existed -> ignore it.
    """

    def __init__(self) -> None:
        self._state: Dict[LinkKey, tuple[LinkAction, Any]] = {}

    def record(self, key: LinkKey, action: LinkAction, event: Any) -> None:
        """Record one event; a later event for the same key wins."""
        self._state[key] = (action, event)

    def resolve(self, existing_keys: set[LinkKey]) -> LinkDeltaResult:
        """Resolve the coalesced state against the links present before the tick."""
        result = LinkDeltaResult()
        for key, (action, event) in self._state.items():
            if action == "created":
                if key in existing_keys:
                    result.updated[key] = event
                else:
                    result.created[key] = event
            else:
                if key in existing_keys:
                    result.removed[key] = event
                else:
                    result.ignored += 1
        return result

    def __len__(self) -> int:
        return len(self._state)
