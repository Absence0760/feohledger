# Commerce / inventory platform integration

Roadmap Priority 14 item 3; reasoning in `docs/decisions.md` §260 (scope) and
§273 (this design).

FeohLedger records what was **ordered, received, billed and paid**. The
business's commerce platform (Shopify, Lightspeed, Square, Cin7, WooCommerce)
records what is **on the shelf**. They meet at the goods receipt: each received
line adds its quantity to the platform's *available* stock at a mapped
location, and cancelling the receipt takes it back off. Nothing is read back —
no stock levels, valuation, POS or sales enter the AP side.

## Moving parts

| Piece | Where |
|---|---|
| Item identity on lines | `po_line_items.catalog_item_id` + `sku`, copied to `gr_line_items` (migration 0111). `sku` is a snapshot; requisition, contract and ERP PO paths stamp it |
| Adapter interface | `app/services/commerce_adapters/base.py` |
| Registry (pkgutil discovery) | `app/services/commerce_adapters/registry.py` |
| Catalogue + save rule | `app/services/commerce_adapters/catalog.py` — configuration merge, `secrets_to_drop` on a platform or destination change, `public_commerce_config` (the ERP rule of decisions §271, mirrored) |
| Credentials at rest | the sealed store, block `commerce` (`services/provider_credentials`, decisions §266): `SECRET_FIELDS["commerce"]` lists every provider secret; adapters get the resolved block; written only by `PUT /api/organization/credentials/commerce` |
| Local default | `app/services/commerce_adapters/mock_adapter.py` |
| Plan / push / retry / sweep | `app/services/commerce_sync.py` |
| HTTP | `app/api/commerce.py` (`/api/commerce/*`) |
| Tables | `commerce_inventory_syncs`, `commerce_sku_mappings` (`app/models/commerce.py`) |
| Local fake | `tools/fake-commerce/` (`pnpm commerce:up`, :12113) |
| Contract test helper | `backend/tests/commerce_contract.py` |
| Web | Organization → Commerce (`CommerceConnectionPanel.svelte`); the receipt modal's sync section (`routes/goods-receipts/CommerceSyncSection.svelte`) |

## The adapter interface

```python
class CommerceAdapter(ABC):
    spec: ClassVar[CommerceProviderSpec]
    def __init__(self, config: dict[str, Any]) -> None          # secrets already decrypted
    async def test_connection(self) -> None                       # raise CommerceError when not OK
    async def list_locations(self) -> list[CommerceLocation]
    async def resolve_sku(self, sku: str) -> CommerceItemRef      # CommerceUnmappedSkuError if none / ambiguous
    async def adjust_available(self, adjustment: InventoryAdjustment) -> InventoryAdjustmentResult
```

`InventoryAdjustment(item_id, location_id, delta: Decimal, idempotency_key, sku=None, reason="received"|"receipt_cancelled")`
— `delta` is signed and never zero. `InventoryAdjustmentResult(provider_reference, available_after=None, replayed=False)`.

Errors (all `CommerceError`, each with a stable `code` and `retryable`):
`CommerceConfigError` (`config_invalid`), `CommerceAuthError` (`auth_failed`),
`CommerceUnmappedSkuError` (`unmapped_sku`), `CommerceUnknownLocationError`
(`unknown_location`), `CommerceRejectedError` (`rejected`, or
`fractional_quantity` via `require_integral`), `CommerceTransientError`
(`transient`, retryable), `CommerceRateLimitedError` (`rate_limited`, retryable).
Helpers: `http_failure(step, response)` maps by status and never copies the
body; `transport_failure(step, exc)` names the exception class only;
`self.require(name)`, `self.http_client()`, `await self.api_base(default)`.

The message of an error is stored on the sync row and shown to an admin:
**never a credential, a URL with its query, or a provider response body.**

## Sync semantics

1. **Plan in the receipt's transaction.** `goods_receipts.create_goods_receipt`
   calls `commerce_sync.plan_receipt`; one `receive` row per line. A line with
   no SKU → `skipped/no_sku`; quantity 0 → `skipped/zero_quantity`. An org
   with no `settings.commerce.provider` plans nothing.
2. **Push after commit** (`services/post_commit`). Claim = compare-and-swap to
   `in_flight` (committed), provider call with no transaction open, outcome
   written only if still `in_flight`. Location: the receipt entity's
   `entity_locations` entry, else `location_id`, else `skipped/no_location`.
   Item: `commerce_sku_mappings` override, else `adapter.resolve_sku`, else
   `skipped/unmapped_sku`. A row planned for another provider fails
   `provider_changed`; a fraction on a whole-unit platform fails
   `fractional_quantity`.
3. **Cancel** (`plan_reversal`): a `synced`/`in_flight` receive gets a
   `reverse` row (−qty, same item and location as the receive); anything that
   never landed becomes `cancelled` and its reverse row `skipped/nothing_to_reverse`.
4. **Idempotency**: unique `(gr_line_item_id, direction)` here; the row's
   `idempotency_key` (`feoh:<line id>:<direction>`) at the provider.
5. **Retry**: `POST /api/commerce/syncs/{id}/retry` (receipt-entry roles) for a
   `failed` row or a `skipped` one with `unmapped_sku`/`no_location`. The sweep
   (`FEOH_COMMERCE_SYNC_ENABLED`, off by default) retries retryable failures
   with backoff (1, 2, 4 … min, max 6 h) up to `FEOH_COMMERCE_SYNC_MAX_ATTEMPTS`,
   pushes rows left `pending`, and reclaims `in_flight` claims older than 15 min.
6. **Audit**: `commerce_inventory.{synced,skipped,failed}` (a retryable failure
   only once it stops being retried) and `commerce_inventory.retry_requested`,
   on the goods receipt; `organization.commerce_{updated,locations_updated,disconnected}`
   and `commerce_sku_mapping.{saved,deleted}` for configuration.

## API

| Route | Who |
|---|---|
| `GET /api/commerce/providers` | admin |
| `GET/PUT/DELETE /api/commerce/settings` | admin; configuration only — the read adds `secrets_set` (sealed secret NAMES); `PUT` refuses a non-blank secret (`commerce_secret_in_settings`) and drops the sealed secrets on a platform or destination change; `DELETE` clears both halves |
| `PUT /api/organization/credentials/commerce` | admin; `{set, clear}` — the only writer of commerce secrets (`api/organization_credentials.py`), audited `organization.credentials_updated` |
| `POST /api/commerce/test` | admin; the posted form plus the secrets it carries — stored ones join only for the saved platform at the saved destination (`config_for_connection_test`); returns `{success, code, message, locations}` |
| `GET /api/commerce/oauth/{provider}/authorize` | admin; the Connect hook — 501 `commerce_oauth_not_available` in this build |
| `GET/PUT /api/commerce/locations` | admin; `{location_id, entity_locations}` |
| `GET/PUT /api/commerce/sku-mappings`, `DELETE …/{id}` | admin |
| `GET /api/commerce/syncs?goods_receipt_id=` | any signed-in user who can see the receipt (entity-scoped) |
| `POST /api/commerce/syncs/{id}/retry` | `RECEIPT_ENTRY_ROLES` |

`PATCH /api/organization` refuses the `commerce` key.

## How to add a provider

Follow exactly. Everything except steps 2 and 7 touches only new files.

1. **Adapter** — `backend/app/services/commerce_adapters/<key>.py`. The file
   name is the provider key (lowercase, `[a-z][a-z0-9_]*`). Declare a
   `CommerceProviderSpec` with `catalog.field(...)` fields, mark secrets
   `secret=True`, list every field that says where credentials go in
   `destination_keys` (a shop domain, store URL, account id that becomes a host,
   an environment that picks the API host), set
   `supports_fractional_quantities`, and decorate the class with
   `@register_commerce_adapter("<key>", SPEC)` at column 0 — the key as a string literal, because that is the shape `pnpm check:subprocessors` reads. No import list to edit — the registry
   discovers the module.
   - The adapter reads its secrets from `self.config` like any other field:
     it is built from the RESOLVED block (`provider_credentials.resolve_block`),
     never from `settings.commerce`, which holds no secret.
   - Build every URL from `await self.api_base(<real base>)`: it honours
     `FEOH_COMMERCE_API_BASE_OVERRIDE` (→ `<override>/<key>`) and enforces
     https + public host on tenant-supplied hosts.
   - Use `self.http_client()`; map non-2xx with `http_failure`, transport
     errors with `transport_failure`; never log or raise a body, token or URL query.
   - Make `adjust_available` idempotent on `adjustment.idempotency_key` (the
     platform's own idempotency key, or a reference/note field you check first).
   - `resolve_sku` raises `CommerceUnmappedSkuError` for none **and** for more
     than one match.
   - Whole-unit platforms call `require_integral(delta)`.
   - Prefer reusing an existing label key (`catalog.KNOWN_FIELD_LABELS`); a new
     field name needs `org.commerce.field.<name>` in all six frontend locales.
2. **Sealed-store secret names** — add every field your spec marks
   `secret=True` to `provider_credentials.SECRET_FIELDS["commerce"]`
   (`backend/app/services/provider_credentials.py`), one line with a comment
   naming the provider. Without it the credentials endpoint refuses the path and
   `tests/test_commerce_adapters.py::test_every_provider_secret_is_a_sealed_store_field`
   fails. The e2e and realdb tests seal secrets through the credentials endpoint
   (`tests-e2e/commerce/helpers.ts::connectCommerce(page, key, config, secrets)`,
   `realdb.store_provider_secrets(…, "commerce", …)`), never through
   `/api/commerce/settings`, which refuses them.
3. **Fake** — `tools/fake-commerce/providers/<key>.py` exporting `router`,
   `reset()` and optionally `state()`; reuse `providers/_inventory.InventoryBook`.
   See `tools/fake-commerce/README.md`.
4. **Backend tests** — `backend/tests/test_commerce_<key>.py` calling
   `assert_commerce_contract(ContractCase(...))` inside `use_fake_commerce`,
   plus provider-specific error-mapping cases (`assert_maps_http_errors`).
5. **E2E** — `frontend/tests-e2e/commerce/<key>.spec.ts` gated on
   `skipUnlessReachable(SERVICES.fakeCommerce)`, using `./helpers.ts`, ending
   with `disconnectCommerce` in `afterAll`.
6. **Docs** — a row in the provider table below, and a line in
   `backend/docs/adapter-patterns.md` § Commerce.
7. **Sub-processor registers** — a real provider receives tenant data, so add
   it to `docs/sub-processors.md` and the published `/legal/sub-processors`
   page and run `pnpm check:subprocessors` (it compares the adapter registries
   with both registers). This is the one shared edit.

## Providers

| Key | Platform | Auth | Status |
|---|---|---|---|
| `mock` | Local mock (in-process) | none | default; SKUs `UNMAPPED*` are unmapped, `TRANSIENT*` fail retryably; locations `mock-main`, `mock-warehouse` |

OAuth consent connections are not built: the catalogue's `auth: "oauth"` renders
a Connect button wired to the authorize hook, and `services/erp_oauth` is bound
to `settings.erp` (`docs/followups.md`). Every planned provider has a key-based
connection.
