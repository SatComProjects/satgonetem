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
    if flows_path.is_file():
        payload = json.loads(flows_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            flows = [item for item in payload if isinstance(item, dict)]

    tx = _link_values(samples, "tx_bps")
    rx = _link_values(samples, "rx_bps")
    errors = sorted(
        {
            str(sample.get("last_error"))
            for sample in samples
            if sample.get("last_error")
        }
    )
    flow_errors = [flow for flow in flows if flow.get("error")]

    summary: dict[str, Any] = {
        "name": manifest.get("name", root.name),
        "run_dir": str(root),
        "scenario": manifest,
        "metrics": {
            "samples": len(samples),
            "first_step": samples[0].get("step") if samples else None,
            "last_step": samples[-1].get("step") if samples else None,
            "sim_time_s": samples[-1].get("sim_time_s") if samples else 0,
            "max_tx_bps": max(tx) if tx else 0,
            "avg_tx_bps": round(mean(tx), 3) if tx else 0,
            "max_rx_bps": max(rx) if rx else 0,
            "avg_rx_bps": round(mean(rx), 3) if rx else 0,
            "last_errors": errors,
        },
        "flows": {
            "count": len(flows),
            "errors": len(flow_errors),
            "results": flows,
        },
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
