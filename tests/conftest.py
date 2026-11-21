"""Shared pytest fixtures and configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support import DummyTopology, FakeCommandExecutor


@pytest.fixture
def command_executor() -> FakeCommandExecutor:
    """A recording command executor."""
    return FakeCommandExecutor()


@pytest.fixture
def topology() -> DummyTopology:
    """An empty dict-backed topology harness."""
    return DummyTopology()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root independent of the current working directory."""
    return Path(__file__).resolve().parents[1]
