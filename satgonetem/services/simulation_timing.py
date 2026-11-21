"""Pure simulation-loop timing helpers."""

from __future__ import annotations


def tick_delay(update_time: int, update_factor: float, elapsed: float) -> float:
    """Return the wait before the next simulation tick.

    The configured interval is ``update_time * update_factor``; a minimum of
    0.01 s is retained so a slow tick cannot spin the loop.
    """
    configured = max(0.01, float(update_time) * float(update_factor))
    return max(0.01, configured - float(elapsed))
