# fake-commerce

A tiny, deterministic HTTP fake of the commerce / inventory platforms FeohLedger
pushes received stock to (Shopify, Lightspeed, Square, Cin7, WooCommerce).
It exists so the real adapters in `backend/app/services/commerce_adapters/` run
end to end with no merchant account (guard rail 7). Reasoning: `docs/decisions.md`
§273; the adapter side is `backend/docs/commerce-integration.md`.

## Running it

```bash
pnpm commerce:up      # docker compose --profile commerce, host port 12113
pnpm commerce:logs
pnpm commerce:down
pnpm test:commerce    # Playwright tests-e2e/commerce/ (skips if the fake is down)
```

`backend/.env.development` sets `FEOH_COMMERCE_API_BASE_OVERRIDE=http://localhost:12113`,
so a dev backend sends every provider's calls to `http://localhost:12113/<provider key>`.
A deployed environment refuses to boot with that override set.

Backend pytest does **not** need the container: `backend/tests/commerce_contract.py`
loads `app.py` in process and routes adapter HTTP to it through
`httpx.ASGITransport`.

## Ops endpoints

| Route | What |
|---|---|
| `GET /health` | Liveness (compose healthcheck, e2e skip gate). |
| `POST /__reset` | Clears every provider's in-memory state. |
| `GET /__providers` | The mounted provider keys. |
| `GET /__state/<key>` | A provider's state snapshot (items, locations, levels), for assertions. |

## Adding a provider

One file: `providers/<provider key>.py`. The file name **is** the provider key —
the same string as the backend `CommerceProviderSpec.key` — and the app mounts
it at `/<provider key>` on start-up. Nothing else in this directory changes.

The module must define:

```python
from fastapi import APIRouter
from providers._inventory import InventoryBook

router = APIRouter()            # paths relative to /<provider key>
BOOK = InventoryBook(...)       # optional — the shared idempotent stock book

def reset() -> None: ...        # restore the fixtures (called by POST /__reset)
def state() -> dict: ...        # optional — exposed at GET /__state/<key>
```

Rules for a provider module:

- **Emulate the real API's shapes** for the calls the adapter makes — paths,
  auth header, request and response bodies, error status codes — and nothing
  more. Refuse a request without the expected credential with the provider's
  real 401/403 body shape, so the adapter's auth-error mapping is exercised.
- **Fixed fixtures** — at least one SKU that resolves, one location, and a way
  to reach "no such SKU" and "no such location". Tests assert these literals.
- **Idempotency the way the provider does it**: a native idempotency key, or the
  reference/note field the adapter writes and checks. `InventoryBook.adjust`
  takes the key and replays the original adjustment.
- **Whole units unless the platform holds fractions** (`InventoryBook(whole_units_only=...)`).
- Optional failure hooks (`POST /<key>/__fail-next` with a status code) so the
  adapter's transient / rate-limit mapping can be tested.
- No new dependencies: `fastapi` + `uvicorn` only (`requirements.in`).
