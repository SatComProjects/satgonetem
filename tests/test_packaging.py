"""Tests for bundled sample data and packaging metadata."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from satgonetem.data import (
    resolve_topology,
    sample_topology_names,
    topology_file,
    topology_files_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT_TEXT = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
_PB2_TEXT = (REPO_ROOT / "satgonetem" / "proto" / "netem_pb2.py").read_text(
    encoding="utf-8"
)
_PB2_GRPC_TEXT = (REPO_ROOT / "satgonetem" / "proto" / "netem_pb2_grpc.py").read_text(
    encoding="utf-8"
)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _declared_lower_bound(package: str) -> tuple[int, ...]:
    """Return the ``>=`` bound declared for *package* in pyproject.toml."""
    match = re.search(
        rf'^\s*"{re.escape(package)}(?P<specifier>[^"]*)"\s*,\s*$',
        _PYPROJECT_TEXT,
        re.MULTILINE,
    )
    assert match is not None, f"{package} is not a declared dependency"
    bound = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", match.group("specifier"))
    assert bound is not None, f"{package} has no lower-bound specifier"
    return _version_tuple(bound.group(1))


def test_bundled_topologies_are_present():
    names = sample_topology_names()
    assert "test_topology.json" in names
    assert "starlink_topology.json" in names
    assert topology_files_dir().joinpath("test_topology.json").is_file()


def test_resolve_topology_accepts_explicit_and_bundled_paths(tmp_path):
    bundled = resolve_topology("test_topology.json")
    assert Path(bundled).is_file()

    explicit = tmp_path / "custom.json"
    explicit.write_text("{}")
    assert resolve_topology(str(explicit)) == str(explicit)


@pytest.mark.parametrize("name", ["../secret.json", "nested/file.json", "", "."])
def test_topology_file_rejects_path_components(name):
    with pytest.raises((ValueError, FileNotFoundError)):
        topology_file(name)


def test_py_typed_marker_is_packaged():
    assert (REPO_ROOT / "satgonetem" / "py.typed").is_file()


def test_pyproject_declares_console_script_and_package_discovery():
    text = (REPO_ROOT / "pyproject.toml").read_text()
    assert 'satgonetem = "satgonetem.cli.main:main"' in text
    assert 'include = ["satgonetem*"]' in text
    assert (
        '"satgonetem.data" = ["topology_files/*.json", "topology_files/*.md"]' in text
    )


def test_declared_grpc_and_protobuf_bounds_cover_generated_stubs():
    """Metadata lower bounds must not admit a runtime the stubs reject.

    ``netem_pb2.py`` validates ``Protobuf Python Version`` and
    ``netem_pb2_grpc.py`` refuses to import when ``grpcio`` is older than the
    generator version, so an install satisfying only the declared ``>=``
    bounds would fail at import time.
    """
    protobuf_stub = re.search(r"Protobuf Python Version:\s*([0-9.]+)", _PB2_TEXT)
    grpc_stub = re.search(r"GRPC_GENERATED_VERSION = '([0-9.]+)'", _PB2_GRPC_TEXT)
    assert protobuf_stub is not None
    assert grpc_stub is not None

    assert _declared_lower_bound("protobuf") >= _version_tuple(protobuf_stub.group(1))
    assert _declared_lower_bound("grpcio") >= _version_tuple(grpc_stub.group(1))


def test_generated_proto_stubs_import_against_the_locked_runtime():
    from google.protobuf import empty_pb2  # noqa: F401  (populate descriptor pool)

    from satgonetem.proto import netem_pb2, netem_pb2_grpc

    assert netem_pb2.DESCRIPTOR is not None
    assert netem_pb2_grpc.NetemStub is not None
