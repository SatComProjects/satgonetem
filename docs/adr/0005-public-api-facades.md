# 0005 — Preserve public API paths with compatibility facades

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

Users import models, services, routing daemons, traffic results, and utilities
from documented module paths. The refactor splits large modules and may rename
misleading utilities. Breaking imports would be a silent, high-impact change for
external scripts.

## Decision

When a module is split or renamed:

1. Keep the old import path as a thin re-export facade for at least one release
   cycle.
2. Preserve public class and function names and result shapes.
3. Document deprecations and the replacement path.
4. Do not change configuration or YAML schemas without a separate specification.
5. Generated protobuf modules remain generated and are never hand-edited.

## Consequences

- Internal structure can improve without forcing user migrations.
- Temporary duplicate module names must be tracked and removed later.
- Tests should assert the public facade imports, not only the new location.

## Alternatives considered

- **Immediate breaking rename:** rejected because it provides no migration
  window for users.
- **No module splits:** rejected because it preserves the current god modules.
