# 0005 — Traffic subsystem

- Status: Proposed
- Date: 2026-11-21
- Depends on: 0001, 0002, 0003
- Supersedes: —

## Context

`iperf3_utils.py` is 1,691 lines and mixes port allocation, command building,
execution, parsing, flow lifecycle, sync helpers, plotting, and OS integration.
Ping, hping3, and iperf3 duplicate the same state machine and runner.
`FlowScheduler` reaches into private `flow._thread` and mutates `flow.delay`.
Ping and hping3 cannot be interrupted during their delay and kill all
same-named processes, unlike iperf3's port-scoped cleanup. `traffic/__init__.py`
eagerly imports pandas and matplotlib.

## Requirements

- **MUST** define a `Flow` protocol and a shared threaded-flow base.
- **MUST** migrate Ping first, then hping3, then iperf3, preserving current
  observable behavior during each step.
- **MUST** split `iperf3_utils.py` into command building, execution, pure
  parsing, result/plot models, and flow lifecycle; keep
  `satgonetem.traffic.iperf3_utils` as a compatibility facade.
- **MUST** add fixture corpora for parser variants and test parsers as pure
  functions.
- **MUST** cover `FlowScheduler` constructor validation, state transitions,
  results/errors after success and failure, timeout behavior, and
  `save_results=False`.
- **MUST** avoid eager pandas/matplotlib imports in the package entry point.
- **MUST NOT** change stop/kill semantics or timeout defaults as part of the
  refactor; those require a separate behavior spec.

## Acceptance criteria

- No traffic module exceeds roughly 400 lines except a thin facade.
- Parser modules have at least 90% coverage; traffic coverage is at least 80%.
- The scheduler depends on the flow protocol, not private attributes.
- Existing imports and result shapes are unchanged.
- The default test suite has no real sleeps attributable to traffic flows.

## Non-goals

- Changing parsing semantics, kill behavior, or scheduler defaults.
- Rewriting plotting output.

## Risks and mitigations

- **Tool output variants:** tolerant parsers and corpora.
- **Behavior drift in cleanup:** preserve current semantics and add a separate
  spec for the ping/hping3 process-isolation defect.
- **Plotting dependencies:** move imports to the plotting module and test the
  facade.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/traffic
uv run --frozen --extra dev pytest -q --cov=satgonetem/traffic --cov-fail-under=80
```
