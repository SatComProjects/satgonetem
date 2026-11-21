"""Built-in routing daemon registry.

The registry is the single source of truth for built-in routing method names
and factories. Callers may supply an additional custom mapping; custom
factories take precedence so tests and embedders can override a method without
editing the manager.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Type

from satgonetem.routing.base_daemon import RoutingDaemon

DaemonFactory = Callable[[Any], RoutingDaemon]

_BUILTIN_FACTORIES: dict[str, DaemonFactory] = {}


def register_builtin(name: str, daemon_class: Type[RoutingDaemon]) -> None:
    """Register a built-in daemon class under *name*."""
    _BUILTIN_FACTORIES[name] = daemon_class


def builtin_names() -> list[str]:
    """Return built-in method names in registration order."""
    return list(_BUILTIN_FACTORIES)


def builtin_factory(name: str) -> DaemonFactory | None:
    """Return the built-in factory for *name*, or None."""
    return _BUILTIN_FACTORIES.get(name)


def create_daemon(
    name: str,
    topology: Any,
    custom: Mapping[str, DaemonFactory] | None = None,
) -> RoutingDaemon | None:
    """Instantiate a routing daemon for *name*.

    Custom factories are checked first, then built-ins. Returns None when the
    method is unknown.
    """
    if custom and name in custom:
        factory = custom[name]
    else:
        factory = _BUILTIN_FACTORIES.get(name)
    return factory(topology) if factory is not None else None


def available_methods(custom: Mapping[str, Any] | None = None) -> list[str]:
    """Return built-in names followed by custom names."""
    names = builtin_names()
    if custom:
        names.extend(name for name in custom if name not in names)
    return names


def is_known_method(
    name: str, custom: Mapping[str, Any] | None = None
) -> bool:
    """Return True when *name* resolves to a built-in or custom factory."""
    return name in _BUILTIN_FACTORIES or bool(custom and name in custom)


__all__ = [
    "DaemonFactory",
    "available_methods",
    "builtin_factory",
    "builtin_names",
    "create_daemon",
    "is_known_method",
    "register_builtin",
]
