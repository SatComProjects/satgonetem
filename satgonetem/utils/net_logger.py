"""Helpers for the per-satellite link-usage sampler.

Each satellite container runs a small Bash sampler written by
:func:`build_net_logger_script`. The sampler appends one JSON object per
sample to ``/tmp/interfaces/<project>.<SatN>``. Every object maps interface
names to a single bits-per-second value, ``max(rx_bps, tx_bps)``, because the
topology link key is direction-independent.

``/tmp`` is bind-mounted into every node container by the launchers, so the
host-side monitor can read the same files without an RPC.
"""

from __future__ import annotations

import base64
import hashlib
import math
import shlex
from typing import Any

NET_LOGGER_DIR = "/tmp/interfaces"
NET_LOGGER_INTERVAL_S = 1.0

_TOKEN_PREFIX = "sgnl-"


def _validate_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_destination(destination: Any) -> str:
    return _validate_text(destination, "destination")


def _validate_token(token: Any) -> str:
    return _validate_text(token, "token")


def _validate_interval(interval_s: Any) -> float:
    if isinstance(interval_s, bool) or not isinstance(interval_s, (int, float)):
        raise TypeError("interval_s must be a number")
    interval = float(interval_s)
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval_s must be a finite positive number")
    return interval


def _directory(path: str) -> str:
    return path.rstrip("/") or "/"


def net_logger_path(
    project_name: str,
    node_name: str,
    path: str = NET_LOGGER_DIR,
) -> str:
    """Return the monitor file for one node.

    Args:
        project_name: GoNetEm project name.
        node_name: Node name such as ``"Sat3"``.
        path: Directory holding the monitor files.

    Returns:
        ``<path>/<project_name>.<node_name>``.
    """
    project = _validate_text(project_name, "project_name")
    node = _validate_text(node_name, "node_name")
    directory = _directory(_validate_text(path, "path"))
    if directory == "/":
        return f"/{project}.{node}"
    return f"{directory}/{project}.{node}"


def net_logger_token(destination: str) -> str:
    """Return a stable, shell-safe identity token for one monitor file."""
    _validate_destination(destination)
    digest = hashlib.sha1(destination.encode("utf-8")).hexdigest()[:16]
    return f"{_TOKEN_PREFIX}{digest}"


def _token_selector(token: str) -> str:
    """Return shell code that selects this sampler without matching pkill.

    The token is decoded from base64 so the literal token never appears in the
    detached ``sh -c`` command line. Replacing the leading ``s`` with a
    character class stops ``pkill -f`` from matching its own command line.
    """
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    return (
        f"token=$(printf %s {shlex.quote(encoded)} | base64 -d); "
        f'pattern="[s]${{token#s}}"'
    )


def build_net_logger_script() -> str:
    """Return the Bash sampler launched inside every satellite container.

    The script reads ``$1`` as the output file, ``$2`` as the sampling
    interval, and ``$3`` as its identity token. It appends one JSON object per
    sample; the value for each ``eth*`` interface is the larger of the
    receive and transmit rates observed since the previous sample.
    """
    return r"""#!/usr/bin/env bash
# SatGoNetem link-usage sampler. Usage: bash -s -- <out> <interval> <token>
set -u

out="$1"
interval="$2"
sysfs_net="${SGNL_SYSFS_NET:-/sys/class/net}"

now_ms() {
    local up rest whole frac
    read -r up rest < /proc/uptime || return 1
    whole="${up%.*}"
    frac="${up#*.}"
    if [ "$frac" = "$up" ]; then
        frac=0
    fi
    while [ "${#frac}" -lt 3 ]; do
        frac="${frac}0"
    done
    frac="${frac:0:3}"
    printf '%s' "$((10#$whole * 1000 + 10#$frac))"
}

declare -A prev_rx=()
declare -A prev_tx=()

emit_sample() {
    local now="$1"
    local first="$2"
    local line="{" sep="" path name rx tx drx dtx elapsed bps
    for path in "$sysfs_net"/eth*; do
        [ -e "$path" ] || continue
        name="${path##*/}"
        rx=$(cat "$path/statistics/rx_bytes" 2>/dev/null) || continue
        tx=$(cat "$path/statistics/tx_bytes" 2>/dev/null) || continue
        case "$rx" in ''|*[!0-9]*) continue ;; esac
        case "$tx" in ''|*[!0-9]*) continue ;; esac
        if [ "$first" = "1" ]; then
            bps=0
        else
            elapsed=$((now - prev_ms))
            if [ "$elapsed" -le 0 ]; then
                elapsed=1
            fi
            drx=$((rx - ${prev_rx[$name]:-$rx}))
            dtx=$((tx - ${prev_tx[$name]:-$tx}))
            if [ "$drx" -lt 0 ]; then drx=0; fi
            if [ "$dtx" -lt 0 ]; then dtx=0; fi
            if [ "$drx" -ge "$dtx" ]; then
                bps=$((drx * 8000 / elapsed))
            else
                bps=$((dtx * 8000 / elapsed))
            fi
        fi
        prev_rx[$name]="$rx"
        prev_tx[$name]="$tx"
        line="${line}${sep}\"${name}\":${bps}"
        sep=","
    done
    printf '%s\n' "${line}}" >> "$out"
}

mkdir -p "$(dirname "$out")"
: > "$out"

prev_ms=$(now_ms)
emit_sample "$prev_ms" 1
while sleep "$interval"; do
    current_ms=$(now_ms)
    emit_sample "$current_ms" 0
    prev_ms="$current_ms"
done
"""


def build_start_command(destination: str, interval_s: float, token: str) -> str:
    """Build the detached command that starts one sampler.

    The command stops any older sampler for the same destination, removes the
    old samples, and pipes the encoded script into a detached Bash process.
    """
    destination = _validate_destination(destination)
    interval = _validate_interval(interval_s)
    token = _validate_token(token)
    encoded = base64.b64encode(build_net_logger_script().encode("utf-8")).decode(
        "ascii"
    )
    selector = _token_selector(token)
    return (
        f"{selector}; "
        f'pkill -f -- "$pattern" >/dev/null 2>&1 || true; '
        f"rm -f {shlex.quote(destination)}; "
        f"printf %s {shlex.quote(encoded)} | base64 -d | "
        f"bash -s -- {shlex.quote(destination)} {shlex.quote(str(interval))} "
        f'"$token"'
    )


def build_stop_command(destination: str, token: str) -> str:
    """Build the command that stops one sampler and removes its samples."""
    destination = _validate_destination(destination)
    token = _validate_token(token)
    selector = _token_selector(token)
    return (
        f"{selector}; "
        f'pkill -f -- "$pattern" >/dev/null 2>&1 || true; '
        f"rm -f {shlex.quote(destination)}"
    )
