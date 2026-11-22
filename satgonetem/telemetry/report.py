"""Build a human- and machine-readable report from a run directory."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from typing import Any

import yaml

from satgonetem.telemetry.writers import write_json


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    samples: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            samples.append(value)
    return samples


def _link_values(samples: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for sample in samples:
        for link in sample.get("links", []):
            try:
                values.append(float(link.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
    return values


def _topology_counts(sample: dict[str, Any] | None) -> dict[str, Any]:
    if not sample:
        return {}
    topology = sample.get("topology")
    return dict(topology) if isinstance(topology, dict) else {}


def _read_run_errors(root: Path) -> list[str]:
    path = root / "errors.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"errors.json could not be read: {exc}"]
    if isinstance(payload, dict):
        payload = payload.get("errors", [])
    if not isinstance(payload, list):
        return ["errors.json must contain a list of errors"]
    return [str(item) for item in payload if item]


def build_report(run_dir: str | Path) -> dict[str, Any]:
    """Summarize a run directory and write ``summary.json`` and ``report.md``.

    Args:
        run_dir: Directory containing ``manifest.yaml`` and, optionally,
            ``metrics.jsonl`` and ``flows.json``.

    Returns:
        The summary dictionary (also written to ``summary.json``).

    Raises:
        FileNotFoundError: If the run directory or manifest is missing.
        ValueError: If the manifest is not a YAML mapping.
    """
    root = Path(run_dir)
    manifest_path = root / "manifest.yaml"
    if not root.is_dir():
        raise FileNotFoundError(f"Run directory does not exist: {root}")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Run manifest not found: {manifest_path}")

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"Run manifest must be a mapping: {manifest_path}")

    samples = _read_jsonl(root / "metrics.jsonl")
    flows_path = root / "flows.json"
    flows: list[dict[str, Any]] = []
    flow_read_error: str | None = None
    if flows_path.is_file():
        try:
            payload = json.loads(flows_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            flow_read_error = f"flows.json could not be read: {exc}"
        else:
            if isinstance(payload, list):
                flows = [item for item in payload if isinstance(item, dict)]
            else:
                flow_read_error = "flows.json must contain a list"

    tx = _link_values(samples, "tx_bps")
    rx = _link_values(samples, "rx_bps")
    simulator_errors = sorted(
        {
            str(sample.get("last_error"))
            for sample in samples
            if sample.get("last_error")
        }
    )
    run_errors = _read_run_errors(root)
    if flow_read_error:
        run_errors.append(flow_read_error)
    flow_errors = [flow for flow in flows if flow.get("error")]
    first_topology = _topology_counts(samples[0] if samples else None)
    last_topology = _topology_counts(samples[-1] if samples else None)

    summary: dict[str, Any] = {
        "name": manifest.get("name", root.name),
        "run_dir": str(root),
        "scenario": manifest,
        "topology": {
            "first": first_topology,
            "last": last_topology,
        },
        "metrics": {
            "samples": len(samples),
            "first_step": samples[0].get("step") if samples else None,
            "last_step": samples[-1].get("step") if samples else None,
            "sim_time_s": samples[-1].get("sim_time_s") if samples else 0,
            "max_tx_bps": max(tx) if tx else 0,
            "avg_tx_bps": round(mean(tx), 3) if tx else 0,
            "max_rx_bps": max(rx) if rx else 0,
            "avg_rx_bps": round(mean(rx), 3) if rx else 0,
            "last_errors": simulator_errors,
        },
        "flows": {
            "count": len(flows),
            "errors": len(flow_errors),
            "results": flows,
            "read_error": flow_read_error,
        },
        "errors": run_errors,
    }

    write_json(root / "summary.json", summary)
    (root / "report.md").write_text(_render_markdown(summary), encoding="utf-8")
    return summary


def _render_markdown(summary: dict[str, Any]) -> str:
    scenario = summary.get("scenario", {})
    metrics = summary.get("metrics", {})
    flows = summary.get("flows", {})
    lines = [
        f"# Run report: {summary.get('name')}",
        "",
        "## Scenario",
        "",
    ]
    for key in ("topology", "duration_s", "tick_s", "routing", "ipv6"):
        if key in scenario:
            lines.append(f"- **{key}:** `{scenario[key]}`")
    topology = summary.get("topology") or {}
    first = topology.get("first") or {}
    last = topology.get("last") or {}
    lines.extend(
        [
            "",
            "## Topology",
            "",
            f"- Satellites: {first.get('satellites', 0)} to "
            f"{last.get('satellites', 0)}",
            f"- Ground stations: {first.get('ground_stations', 0)} to "
            f"{last.get('ground_stations', 0)}",
            f"- Links: {first.get('links', 0)} to {last.get('links', 0)}",
        ]
    )
    lines.extend(
        [
            "",
            "## Metrics",
            "",
            f"- Samples: {metrics.get('samples', 0)}",
            f"- Simulation time: {metrics.get('sim_time_s', 0)} s",
            f"- Steps: {metrics.get('first_step')} to {metrics.get('last_step')}",
            f"- Max / average TX: {metrics.get('max_tx_bps', 0)} / "
            f"{metrics.get('avg_tx_bps', 0)} bps",
            f"- Max / average RX: {metrics.get('max_rx_bps', 0)} / "
            f"{metrics.get('avg_rx_bps', 0)} bps",
            "",
            "## Flows",
            "",
            f"- Result objects: {flows.get('count', 0)}",
            f"- Errors: {flows.get('errors', 0)}",
            "",
        ]
    )
    if flows.get("read_error"):
        lines.extend(
            [
                f"- **Artifact warning:** {flows['read_error']}",
                "",
            ]
        )
    if flows.get("results"):
        lines.extend(["| Source | Destination | Tool | Error |", "|---|---|---|---|"])
        for flow in flows["results"]:
            lines.append(
                "| {src} | {dst} | {tool} | {error} |".format(
                    src=flow.get("source", ""),
                    dst=flow.get("destination", ""),
                    tool=flow.get("tool", ""),
                    error=flow.get("error") or "",
                )
            )
        lines.append("")
    run_errors = summary.get("errors") or []
    if run_errors:
        lines.extend(["## Run errors", ""])
        lines.extend(f"- `{error}`" for error in run_errors)
        lines.append("")
    errors = metrics.get("last_errors") or []
    if errors:
        lines.extend(["## Simulator errors", ""])
        lines.extend(f"- `{error}`" for error in errors)
        lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    lines.append("- `manifest.yaml` — scenario definition")
    lines.append("- `metrics.jsonl` — per-sample link metrics")
    lines.append("- `flows.json` — serialized flow results")
    lines.append("- `summary.json` — machine-readable summary")
    lines.append("")
    return "\n".join(lines)
