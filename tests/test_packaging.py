"""Tests for bundled sample data and packaging metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from satgonetem.data import (
    resolve_topology,
    sample_topology_names,
    topology_file,
    topology_files_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
