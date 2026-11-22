"""Bundled sample topology files and lookup helpers.

The sample constellations are shipped inside the package so an installed wheel
can load them without a repository checkout.  ``resolve_topology`` accepts an
explicit filesystem path first and falls back to a bundled file name.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

__all__ = [
    "resolve_topology",
    "sample_topology_names",
    "topology_file",
    "topology_files_dir",
]

_TOPOLOGY_SUFFIX = ".json"


def topology_files_dir() -> Any:
    """Return the directory containing the bundled topology files."""
    return files(__name__).joinpath("topology_files")


def sample_topology_names() -> tuple[str, ...]:
    """Return the bundled topology file names, sorted."""
    return tuple(
        sorted(
            entry.name
            for entry in topology_files_dir().iterdir()
            if entry.name.endswith(_TOPOLOGY_SUFFIX)
        )
    )


def topology_file(name: str) -> Any:
    """Return one bundled topology file.

    Args:
        name: Plain file name such as ``"starlink_topology.json"``.  Path
            separators are rejected so the helper cannot escape the bundled
            directory.

    Raises:
        ValueError: If *name* is not a plain file name.
        FileNotFoundError: If the file is not bundled.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"Topology name must be a non-empty string, got {name!r}")
    if Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"Topology name must be a plain file name, got {name!r}")

    candidate = topology_files_dir().joinpath(name)
    if not candidate.is_file():
        available = ", ".join(sample_topology_names()) or "none"
        raise FileNotFoundError(
            f"Bundled topology {name!r} was not found; available: {available}"
        )
    return candidate


def resolve_topology(path: str) -> str:
    """Return a loadable topology path.

    An existing filesystem path is returned unchanged.  Otherwise *path* is
    looked up in the bundled topology directory, so installed users can pass
    ``"starlink_topology.json"``.

    Raises:
        FileNotFoundError: If neither an existing path nor a bundled file
            matches.
    """
    direct = Path(path)
    if direct.is_file():
        return str(direct)
    return str(topology_file(path))
