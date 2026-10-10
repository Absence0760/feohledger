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

`InventoryAdjustment(item_id, location_id, delta: Decimal, idempotency_key, sku=None, reason="received"|"receipt_cancelled", attempt=1)`
— `delta` is signed and never zero. `attempt` is which push of this key it is
(the sync row's `attempts` after its claim): above 1, an earlier push may have
landed without our hearing back, so an adapter whose provider may forget an
idempotency key looks for the earlier write first (§ Square). `InventoryAdjustmentResult(provider_reference, available_after=None, replayed=False)`.

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
| `shopify` | Shopify (Admin GraphQL, pinned `2026-10`) | custom-app Admin API access token per shop | real; whole units only; fake `providers/shopify.py` |
| `woocommerce` | WooCommerce (merchant's own WordPress, REST `/wp-json/wc/v3`) | consumer key + secret, HTTPS Basic | real; one synthetic location `store`; whole units; § WooCommerce |
| `square` | Square (REST API v2, `Square-Version` pinned) | access token (Bearer) + `environment` | real; fractional quantities; § Square |

### Shopify (`commerce_adapters/shopify.py`)

- **Connection** — `shop_domain` (`<handle>.myshopify.com`; a bare handle or a
  pasted `https://…/` is normalised, anything naming another host is refused —
  it is a destination key, so changing it drops the stored token),
  `access_token` (secret; a custom app's Admin API token with `read_locations`,
  `read_products`, `read_inventory`, `write_inventory`), and optional
  `api_version` (default `2026-10`; a `YYYY-MM` release from `2026-01`, where
  `@idempotent` exists).
- **Calls** — `POST https://<shop>/admin/api/<version>/graphql.json` with
  `X-Shopify-Access-Token`: `shop { name }` (test), `locations` (active only,
  paginated), `productVariants(query: "sku:\"…\"")` filtered to an exact SKU
  (Shopify's search is token-based; none or several → `unmapped_sku`), then per
  adjustment a precheck of `location` + the item's `inventoryLevel` there, then
  `inventoryAdjustQuantities` (`name: "available"`, `reason: "received"` or
  `"correction"` for a cancelled receipt, `referenceDocumentUri:
  feohledger://inventory-sync/<key>`, `@idempotent(key:)` plus the
  `Idempotency-Key` header).
- **Idempotency** — the same key and parameters replay the original
  adjustment group. The precheck exists so a stale location or an item not
  stocked there is refused *before* a key is spent. A key whose attempt failed
  (`IDEMPOTENCY_PREVIOUS_ATTEMPT_FAILED`) cannot be reused, so the adapter walks
  `<key>`, `<key>#2` … `<key>#5`; a later replay walks the same sequence to the
  attempt that succeeded. `IDEMPOTENCY_KEY_PARAMETER_MISMATCH` is refused, never
  re-keyed — an earlier call may have landed elsewhere. Shopify documents no
  key-retention window. The case that depends on it is a call that landed but
  whose response was lost (timeout, reclaimed `in_flight` claim): its retry is
  safe only while Shopify still holds the key, and the sweep's retries fall
  well inside a day; a manual retry days later would not be protected.
- **Errors** — HTTP via `http_failure`; GraphQL `THROTTLED` → `rate_limited`,
  `ACCESS_DENIED` (missing scope) → `auth_failed`, `INTERNAL_SERVER_ERROR` →
  `transient`, any other code → `rejected` naming the code only. userErrors:
  `INVALID_LOCATION` → `unknown_location`; `ADJUST_QUANTITIES_FAILED`,
  `SERVICE_UNAVAILABLE`, `IDEMPOTENCY_CONCURRENT_REQUEST` → `transient`; the rest
  → `rejected` with our own wording. Shopify's message text is never copied.
- **Fake** — `shpat_fake_shopify_token` (and `shpat_fake_readonly_token` for
  `ACCESS_DENIED`); `gid://shopify/Location/1`/`2` (and inactive `/3`); SKUs
  `SHOP-TEE-M` → `InventoryItem/101`, `SHOP-DUP` (ambiguous), `SHOP-MUG` (not
  stocked at Location/2). `POST /shopify/__fail-next` queues `throttled`, an
  `http` status, or a mutation userError code.

OAuth consent connections are not built: the catalogue's `auth: "oauth"` renders
a Connect button wired to the authorize hook, and `services/erp_oauth` is bound
to `settings.erp` (`docs/followups.md`). Every planned provider has a key-based
connection.

## WooCommerce

`app/services/commerce_adapters/woocommerce.py`; fake at
`tools/fake-commerce/providers/woocommerce.py`; tests
`backend/tests/test_commerce_woocommerce.py` and
`frontend/tests-e2e/commerce/woocommerce.spec.ts`.

**Connection.** Fields: `store_url`, `consumer_key`, `consumer_secret` (both
secret; a REST key from WooCommerce → Settings → Advanced → REST API with
Read/Write permission). `store_url` is the merchant's own WordPress site, so it
is a **destination key** (changing it drops the stored secrets, decisions
§266). It must be `https`, carry a host, and carry no user-info, query or
fragment; a sub-directory install (`https://example.com/shop`) is fine. Every
call goes through `api_base`, which runs the SSRF check (the host must resolve
to a public address). **Redirects are never followed** — a 3xx is a non-retryable
`rejected`, so the Basic credentials only ever reach the host the admin typed.
The key and secret ride the `Authorization` header only; WooCommerce's
query-string auth is never used, because a query string lands in access logs.
`test_connection` calls `GET /system_status`, which needs a valid key (the
public `/wp-json/wc/v3` index would answer an anonymous caller).

Not supported: sites without pretty permalinks (`?rest_route=` URLs), and hosts
that strip the `Authorization` header before PHP sees it (some CGI setups) —
both answer with an error at Test, and the merchant's fix is on their server.

**Locations.** Core WooCommerce keeps one stock pool per product, so
`list_locations` returns one synthetic location, `store` ("Store"). Map it as
the default location. Multi-location inventory plugins (ATUM, Multi Locations
Inventory and the like) each have their own API and are **out of scope**.

**Items.** `item_id` is `<product id>` for a simple product and
`<parent id>/<variation id>` for a variation — the same shape an admin types
into an SKU override. `resolve_sku` calls `GET /products?sku=<sku>` (on current
WooCommerce this matches variations as well, returning `type: variation` and
`parent_id`); when that finds nothing it falls back to the variations endpoint,
which is per parent, by walking the variable products
(`GET /products?type=variable`, then `GET /products/<id>/variations?sku=`),
bounded to `MAX_FALLBACK_PARENTS` (100) parents. Past the bound the answer is
"unmapped" — a skip an admin fixes with an SKU override, never a guess. Matching
is case-insensitive, like WooCommerce's own SKU uniqueness. Zero matches **and**
more than one (an import can duplicate an SKU) are `unmapped_sku`. An SKU
containing a comma is unmapped without a call, because WooCommerce reads `sku=`
as a comma-separated list.

**Adjusting.** WooCommerce has no relative stock endpoint: `stock_quantity` on
`PUT /products/<id>` (or `…/variations/<vid>`) is absolute. So the adapter:

1. refuses a fraction (`require_integral`) and any location but `store`;
2. reads the product — and, for a variation whose `manage_stock` is `"parent"`,
   its parent, which holds that variation's stock;
3. refuses with code `stock_not_managed` (non-retryable; the receipt shows the
   adapter's message: turn on *Manage stock?*) when `manage_stock` is not on;
4. looks for `adjustment.idempotency_key` in the product's
   `_feoh_inventory_syncs` meta — found means a replay: return
   `replayed=True`, write nothing;
5. writes `stock_quantity = current + delta` **and** the key into that meta in
   one `PUT`, so a response lost after the write leaves the key recorded and
   the retry is recognised.

The meta value is a JSON list of `{k, d, a}` (key, delta, quantity after),
bounded to the newest `MAX_RECORDED_SYNCS` (100) entries per product; a replay
of a key older than that on the same product would apply twice, which the
retry sweep's attempt cap and backoff keep far out of reach. Unreadable meta is
treated as empty. `provider_reference` is `wc:<product id>:<idempotency key>`.

**Decision: the read → write race with storefront sales is accepted, and
tracked.** Between the read in step 2 and the write in step 5, a sale on the
storefront can decrement the stock; our absolute write then overwrites it, and
the shelf count is wrong by that sale (a lost update). There is no
compare-and-set in the WooCommerce REST API — no `If-Match`, no conditional
`stock_quantity` — so the adapter cannot detect it. The window is two HTTP
round trips, once per receipt line, and the alternatives are worse:
*re-reading after the write* cannot tell a sale from our own change;
*refusing to sync* leaves the receipt's stock off the shelf entirely; and
*query-string or plugin-free tricks* do not exist. The durable fix is a
relative write: WooCommerce's own `wc_update_product_stock($product, $n,
'increase'|'decrease')` runs an atomic SQL `UPDATE … SET stock = stock + n`, but
it is reachable only from PHP — so it needs a small companion WordPress plugin
exposing it as a REST route (with the idempotency check beside it). That is
tracked in `docs/followups.md` with its trigger.

**Sub-processor status.** The store runs on the merchant's own WordPress host,
chosen and contracted by them; Automattic (WooCommerce's maker) receives
nothing through this integration. So WooCommerce is a **customer-controlled
counterparty, not a sub-processor of ours**: its `docs/sub-processors.md` § 19.1
row names the processor `(customer's own WordPress store)`, the same shape as
the customer's QMS, and it is listed with § 22's customer-controlled
counterparties and on `/legal/sub-processors` § 4.

**Fake.** Fixtures `WOO-SIMPLE-1` (101, 10 in stock), `WOO-UNMANAGED` (102,
`manage_stock: false`), `WOO-VAR-RED` (variation 201 of 200), `WOO-VAR-BLUE`
(variation 202, `manage_stock: "parent"`), `WOO-DUP` (two products). Key
`ck_fake_woocommerce` / `cs_fake_woocommerce`. `LEGACY_SKU_SEARCH` makes
`/products?sku=` skip variations (the fallback's case); `POST
/woocommerce/__fail-next {"status": N}` injects one failure (a 3xx carries a
`Location`); `GET /__state/woocommerce` exposes stock, meta, the request log and
a count of requests that carried credentials in the query (always 0).

## Square

`app/services/commerce_adapters/square.py`; fake `tools/fake-commerce/providers/square.py`;
tests `backend/tests/test_commerce_square.py`, `frontend/tests-e2e/commerce/square.spec.ts`.

- **Connection.** `environment` (`sandbox` first, so the form never defaults to
  the live host) picks `connect.squareupsandbox.com` or `connect.squareup.com`
  and is a destination key — switching it drops the stored token.
  `access_token` is a Developer Console access token sent as `Bearer`; it needs
  `MERCHANT_PROFILE_READ`, `ITEMS_READ`, `INVENTORY_READ` and `INVENTORY_WRITE`.
  Every request pins `Square-Version` (`SQUARE_VERSION`, `2026-09-16`); bump it
  deliberately, with the fake.
- **Calls.** `test_connection` → `GET /v2/merchants/me`; `list_locations` →
  `GET /v2/locations` (`INACTIVE` → `active=False`); `resolve_sku` →
  `POST /v2/catalog/search`, `ITEM_VARIATION` by an exact `sku` query, item id =
  the variation id (Square counts stock per variation); none or more than one →
  `unmapped_sku`.
- **Adjustment.** `POST /v2/inventory/changes/batch-create`, one `ADJUSTMENT`
  at one location (`from_location_id` = `to_location_id`): receive is
  `NONE → IN_STOCK`, a cancelled receipt `IN_STOCK → NONE` — Square's
  documented "removed from inventory tracking" transition, not `WASTE`, which
  would report the stock as damaged or lost. The quantity is the exact decimal
  string (Square holds up to five places; more is refused, never rounded);
  `occurred_at` is the push time, since Square refuses one older than 24 hours.
  `available_after` is the `IN_STOCK` count in the response's `counts`.
- **Idempotency.** Square's native `idempotency_key` is our key (hashed only if
  over Square's 128 characters), and `reference_id` carries it too. Square does
  not document how long it remembers a key, so on any push after the first
  (`attempt > 1`) the adapter first looks for an adjustment carrying our
  `reference_id` (`POST /v2/inventory/changes/batch-retrieve`, the item at the
  location, up to 10 pages of 1000) and returns it as `replayed` instead of
  writing again; a first push pays for no lookup. A re-send Square refuses as
  `IDEMPOTENCY_KEY_REUSED` (the body changed — a retry always has a new
  `occurred_at`) gets the same lookup, and not finding it there is a
  non-retryable refusal, never an assumed success.
- **Errors.** `errors[].category` first — `AUTHENTICATION_ERROR` →
  `auth_failed`, `RATE_LIMIT_ERROR` → `rate_limited`, `API_ERROR` →
  `transient` — then the status as in `http_failure`; an
  `INVALID_REQUEST_ERROR` whose `field` names a location → `unknown_location`,
  anything else → `rejected`. The message carries the step, the status and the
  category / code only when they are upper-snake constants; `detail` is never
  copied.
- **Fake fixtures.** Token `fake-square-token`; locations `L-MAIN`,
  `L-WAREHOUSE`, `L-CLOSED` (inactive); SKU `SQ-TEE-M` → `VAR-TEE-M`; `SQ-DUP`
  on two variations; `POST /square/__fail-next {"status": …}`;
  `POST /square/__forget-keys` (Square's key retention lapsing).
- **Not built:** Square OAuth (app install) — a personal or application access
  token only; tracked with the other OAuth connections (§ Providers).
