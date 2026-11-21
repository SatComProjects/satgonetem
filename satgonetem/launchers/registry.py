"""Launcher backend registry.

The default GoNetEm backend is resolved dynamically so existing tests and
embedders can replace the class at its module path. Additional backends
register a factory under a name used by ``NetworkConfig.network_launcher``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

LauncherFactory = Callable[..., Any]

_FACTORIES: dict[str, LauncherFactory] = {}


def register_launcher(name: str, factory: LauncherFactory) -> None:
    """Register a launcher factory under *name* (case-insensitive)."""
    _FACTORIES[name.upper()] = factory


def create_launcher(name: str = "GONETEM", **kwargs: Any) -> Any:
    """Create the launcher selected by *name*.

    Raises:
        ValueError: If no backend is registered for *name*.
    """
    key = (name or "GONETEM").upper()
    if key == "GONETEM":
        from satgonetem.launchers import gonetem_launcher

        return gonetem_launcher.GoNetEmLauncher(**kwargs)
    factory = _FACTORIES.get(key)
    if factory is None:
        raise ValueError(f"Unknown network launcher backend: {name!r}")
    return factory(**kwargs)


def available_launchers() -> list[str]:
    """Return registered backend names, including the built-in GoNetEm."""
    names = ["GONETEM"]
    names.extend(name for name in _FACTORIES if name not in names)
    return names
