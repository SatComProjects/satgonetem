"""Preload ``satgonetem.utils`` before pytest-cov resolves dotted sources.

coverage.py resolves importable ``--cov`` source names by importing the parent
package inside ``sys_modules_saved()``. On Python 3.14 that can evict numpy's
single-phase C extension from ``sys.modules``; the next import then fails with
``ImportError: cannot load module more than once per process``.

Importing the single-phase C-extension dependencies before coverage starts keeps
them inside the pre-snapshot module set. Only dotted ``satgonetem.utils``
sources trigger this: those are the ones coverage resolves by importing the
eager utils package initialiser.
"""

from __future__ import annotations

import importlib
import os
import shlex
import sys

_DOTTED_UTILS_SOURCE = "--cov=satgonetem.utils."
_PRELOAD_MODULES = ("numpy", "scipy", "sklearn")


def _requests_dotted_utils_coverage(argv: list[str], addopts: str = "") -> bool:
    """Return True when the invocation targets a dotted utils module."""
    args = [*argv, *shlex.split(addopts)]
    return any(arg.startswith(_DOTTED_UTILS_SOURCE) for arg in args)


if _requests_dotted_utils_coverage(sys.argv, os.environ.get("PYTEST_ADDOPTS", "")):
    for _module in _PRELOAD_MODULES:
        importlib.import_module(_module)
