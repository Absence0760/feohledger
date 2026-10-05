---
description: Add or land an Alembic revision with the migration-coordinator agent in the loop. Checks the chain, id length, control-plane-vs-tenant gate and idempotent DDL; applies to the local control plane + one tenant; verifies model ↔ migration parity (fresh tenants use create_all); surfaces sync edits across models, schemas, frontend types and mobile models (no codegen); proposes tests; flags doc updates.
argument-hint: <revision slug or path>
---

Run the new-migration workflow for `$ARGUMENTS`. Either author and coordinate
the revision, or coordinate one the user has already drafted.

## When to use this command

**Right fit:**

- About to add a file under `backend/alembic/versions/`.
- Just drafted a revision and want it verified before committing.
- Changing a revision that is not yet on `main`.

**Wrong fit — refuse:**

- A revision already on `main` — history is never edited; write a new revision.
- A one-off data fix with no schema change and no need to fan out to every
  tenant — that is a script under `backend/scripts/`, not a revision.

## What this command does

It is **not** the per-change reviewer (`/safe-edit`). It is the per-migration
workflow that catches drift the reviewer can't easily see: a revision with no
scope gate, an index a fresh tenant will never get because the model doesn't
declare it, a chain fork, a revision id too long for `alembic_version`, and the
hand-synced layers (models, schemas, `frontend/src/lib/types/`,
`mobile/lib/models/`).

The work is done by the `migration-coordinator` agent
(`.claude/agents/engineering/migration-coordinator.md`). This command resolves
the revision, invokes the agent, and offers the follow-up edits.

## Procedure

### 1. Resolve the revision

If `$ARGUMENTS` is:

- A **path** under `backend/alembic/versions/` → use it.
- A **slug** → find the current head (`ls backend/alembic/versions | sort | tail -1`)
  and propose `NNNN_<slug>.py` for the next number, with `down_revision` set to
  that head and a revision id of at most 32 characters. If the file doesn't
  exist yet, draft it from the nearest sibling of the same scope (control vs
  tenant), including its `_is_control_db()`-style gate — then coordinate it.
- **Empty** → `git status` for new or modified files under
  `backend/alembic/versions/`. No candidate → "no migration to coordinate."

### 2. Spawn the migration-coordinator agent

> "Coordinate the Alembic revision at `backend/alembic/versions/<file>`. Output the format from your spec."

### 3. Relay the report

Relay it verbatim — the file paths and proposed field signatures are the
actionable part.

### 4. Offer the follow-up edits

One focused question at a time, each opt-in, in this order: model parity fixes
(blocking), schema / frontend type / mobile model sync, tests, docs. Apply only
what the agent proposed.

### 5. Hand off the commit

Path-scoped (`git commit -m "…" -- <paths>`), and only when asked or when the
surrounding workflow already commits. `feat(db):` for new tables/columns,
`fix(db):` for corrective revisions, `chore(db):` for index-only changes. No
`Co-Authored-By` / "Generated with" / emoji footers.

## What this command does NOT replace

- `/check` — the pre-commit gate; run it after `/safe-migration` for the
  review + test-gap + doc pass on the whole diff.
- `/safe-edit` — for the route / service work the new column enables. Run
  `/safe-migration` first, then `/safe-edit` on the code that uses it.

## Tone

A one-line "Coordinating migration `<file>`…", the agent's verbatim report, the
opt-in follow-ups one at a time, the commit handoff. Don't narrate the fan-out.
