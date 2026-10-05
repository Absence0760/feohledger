"""Shared probe for "a by-id route honours `X-Entity-ID`" regression tests.

Every by-id route on an `EntityMixin` model resolves its row within the
caller's selected entity, and an out-of-scope id gets the SAME 404 a missing
one does (`api/purchase_orders._get_scoped_po` is the reference shape). The
tests that pin that, one per router, all need the same three moves:

- give the tenant a second entity (`two_entities`);
- fire a request at a row from the *other* entity and assert it is a 404
  byte-identical to the one an unknown id gets (`assert_out_of_scope_404`), so
  the route can't be used as an enumeration oracle;
- prove the in-scope read still works (the positive control each test makes
  itself, because what "works" means differs per route).

Kept as a helper module rather than a fixture so each test file reads top to
bottom without chasing conftest indirection.
"""

from __future__ import annotations

import uuid


async def two_entities(client, *, slug: str) -> tuple[str, str]:
    """Create a second entity; return ``(default_entity_id, other_entity_id)``."""
    r = await client.post("/api/entities", json={"name": f"Scope {slug}", "slug": slug})
    assert r.status_code == 201, r.text
    other_id = r.json()["id"]
    listing = await client.get("/api/entities")
    default_id = next(e["id"] for e in listing.json() if e["is_default"])
    return default_id, other_id


async def assert_out_of_scope_404(
    client,
    method: str,
    path: str,
    row_id: str,
    *,
    entity_id: str,
    json: dict | list | None = None,
    params: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
) -> None:
    """``path`` (containing ``{id}``) against ``row_id`` under ``entity_id``
    must 404 exactly as it does for an id that doesn't exist at all.

    ``row_id`` may also appear in ``params`` / ``json`` values as the literal
    ``"{id}"``, for routes that take the id outside the path.
    """

    def _fill(value: str):
        def sub(obj):
            if isinstance(obj, str):
                return obj.replace("{id}", value)
            if isinstance(obj, dict):
                return {k: sub(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [sub(v) for v in obj]
            return obj

        return sub

    headers = {"X-Entity-ID": entity_id}
    real, ghost = _fill(row_id), _fill(str(uuid.uuid4()))

    async def fire(fill):
        return await client.request(
            method,
            fill(path),
            headers=headers,
            json=fill(json),
            params=fill(params),
            files=files,
            data=fill(data),
        )

    blocked = await fire(real)
    assert blocked.status_code == 404, (method, path, blocked.status_code, blocked.text)
    missing = await fire(ghost)
    assert missing.status_code == 404, (method, path, missing.status_code, missing.text)
    assert blocked.json() == missing.json(), (method, path, blocked.json(), missing.json())
