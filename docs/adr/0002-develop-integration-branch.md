# 0002 — Use develop as the integration branch

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

The repository has `main` and `develop`. Release tags are cut from release
commits, while ongoing work needs an integration branch that can absorb
multi-step refactors without blocking releases. Contributors need a predictable
base and target for pull requests.

## Decision

- `develop` is the integration branch for all feature, fix, refactor, test, and
  documentation work.
- `main` is release-only.
- Short-lived branches are created from `develop` and target `develop` via pull
  request.
- Required CI checks and pull requests are enforced on both branches.

## Consequences

- Releases are deliberate promotions from a tested integration branch.
- Direct pushes to `develop` are not part of the workflow once rulesets are
  enabled.
- Emergency fixes still go through the same gates; the ruleset can be adjusted
  temporarily only with an explicit, recorded decision.

## Alternatives considered

- **Trunk-based development on `main`:** viable, but would require changing the
  existing release process and branch protection immediately.
- **Long-lived refactor branch:** rejected because it delays integration and
  increases merge risk.
