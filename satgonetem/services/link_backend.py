"""Narrow link-operation backend used by LinkOpsMixin."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LinkBackend(Protocol):
    """Operations LinkOpsMixin needs from a network launcher."""

    def update_links(self, links: list[Any]) -> Any: ...

    def add_link(self, link: Any) -> Any: ...

    def update_link(self, link: Any) -> Any: ...

    def delete_link(self, link: Any) -> Any: ...
