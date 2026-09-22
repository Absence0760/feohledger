# Procurement / Requisitions

Procure-to-pay for the AP platform: buyers raise requisitions or non-PO intake
requests, route them for approval, convert approved requests into purchase
orders, buy from preferred catalogs/vendors (guided buying), and track spend
against department / project / cost-center / GL budgets.

This is the umbrella doc. Each vertical has its own focused doc:

| Vertical | Doc | API | Frontend |
|----------|-----|-----|----------|
| Purchase requisitions + req→PO conversion | [procurement-requisitions.md](procurement-requisitions.md) | `/api/requisitions` | `/requisitions` |
| Catalog management + guided buying | [procurement-catalogs.md](procurement-catalogs.md) | `/api/catalogs` | `/catalogs` |
| Budget tracking | [procurement-budgets.md](procurement-budgets.md) | `/api/budgets` | `/budgets` |
| Intake forms for non-PO spend | [procurement-intake.md](procurement-intake.md) | `/api/intake` | `/intake` |

## Data model (migration `0041_procurement`)

Six tenant-scoped tables in `app/models/procurement.py` (alongside the existing
`PurchaseOrder` / `GoodsReceipt` tables). All carry an inline `organization_id`
+ `EntityMixin` (subsidiary scope) + `TimestampMixin`. Money is always
`Numeric(15, 2)` (never float). Status fields are `StrEnum` mapped to
`Enum(..., native_enum=False)` String columns. No circular FKs — create order is
`catalogs → catalog_items → budgets → purchase_requisitions →
requisition_line_items → intake_requests` (`purchase_orders` / `contracts` /
`vendors` / `gl_accounts` / `entities` already exist). The migration is
tenant-gated (no-ops on the control plane), idempotent, and fans out to every
tenant via `scripts/migrate_all_tenants.py`; a fresh tenant gets the tables from
`tenant_provisioning` `create_all` because the models are registered on
`Base.metadata`.

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `catalogs` | Supplier / internal catalog | `catalog_type` (internal/punchout), `vendor_id`, `punchout_url`, `is_active`, `is_preferred` |
| `catalog_items` | Purchasable line in an internal catalog | `catalog_id`, `sku`, `unit_price`, `uom`, `vendor_id`, `gl_account_id`, `category` |
| `budgets` | Spend allocation for a dimension/period | `dimension` (department/project/cost_center/gl_account), `dimension_value`, `period`, `amount` |
| `purchase_requisitions` | Buyer purchase request | `requisition_number`, `requester_user_id`, `status`, `total`, `vendor_id`, `contract_id`, `budget_id`, `converted_po_id` |
| `requisition_line_items` | Requisition line | `requisition_id`, `catalog_item_id`, `quantity`, `unit_price`, `total`, `gl_account_id` |
| `intake_requests` | Non-PO spend intake form | `request_number`, `request_type`, `status`, `estimated_amount`, `form_data` (JSONB), `converted_requisition_id`, `converted_po_id` |

## How the pieces connect

```
intake_request --(convert)--> purchase_requisition --(convert)--> purchase_order
   (non-PO ask)                  (approved)                          (committed spend)
                                      |  budget_id                        |
                                      v                                   v
                                   budget  <----- compute-on-read spend rollup -----
                                      ^
                                catalog / guided buying steer the buyer to
                                preferred vendors + in-contract sources
```

- **A PO records the currency its source knew.** `purchase_orders.currency`
  (migration 0099, nullable, no default) is stamped by every creation path —
  the requisition's code on conversion, the contract's on a contract-based PO,
  the ERP payload's on sync — and NULL means no source said, rendered bare.
  Existing POs were back-filled from their originating requisition only; the
  rest stay NULL rather than borrow the org's currency (`docs/decisions.md`
  §197). `tests/test_purchase_order_currency_stamping.py` fails on a
  `PurchaseOrder(...)` that omits `currency=`.
- **Intake → Requisition → PO** is the spend escalation path. Each conversion is
  **idempotent** (a replay returns the existing downstream artifact, never a
  second one) and **row-locked** (`SELECT … FOR UPDATE` on the source row) so two
  concurrent requests can't both create — honoring the money-path idempotency
  invariant.
- **Budgets** never store a running total; committed spend (open requisitions +
  their converted POs) and actual spend (matched invoices) are summed live from
  the procurement/AP tables so totals can't drift. See the budgets doc for the
  exact committed-vs-actual definitions and the known department/project actual
  gap.
- **Guided buying** is deterministic (no LLM): preferred catalogs flag preferred
  vendors, the contracts table surfaces in-contract vendors, and active catalog
  items are matched by category/vendor/text.

## RBAC summary

| Surface | Read | Mutate |
|---------|------|--------|
| Requisitions | admin / ap_manager / ap_clerk / cfo | create/edit admin / ap_manager / ap_clerk; approve/reject admin / ap_manager / cfo; convert admin / ap_manager |
| Catalogs | admin / ap_manager / ap_clerk / cfo | admin / ap_manager |
| Budgets | admin / ap_manager / cfo | admin / cfo |
| Intake | admin / ap_manager / ap_clerk / cfo | raise: all four roles; approve/reject/convert admin / ap_manager |

Every route is behind the auth dependency + `require_roles`; the four routers are
in `tests/test_rbac.py::ROUTERS` so the no-auth-endpoint coverage gate scans
them. Every mutation writes a `dispatch_audit` row.

## Shipped extensions (formerly deferred)

- Live cXML / OCI punch-out round-trips are implemented — the `punchout_adapters`
  family (mock in-process default + real `cxml`, fail-closed without a supplier
  secret), `PunchoutSession` (migration `0045`), a secret-gated supplier
  cart-return endpoint, and convert-to-requisition. See
  [procurement-catalogs.md](procurement-catalogs.md).
- Budget `actual` now matches all four dimensions: invoices carry
  `department` / `project` columns (migration `0044`), so realised-invoice
  actuals cover department/project, not just `cost_center` / `gl_account`. See
  [procurement-budgets.md](procurement-budgets.md).

## Deferred / future extensions

- Per-entity requisition approval chains reuse the lightweight status machine
  (not the full invoice `WorkflowInstance` engine).
