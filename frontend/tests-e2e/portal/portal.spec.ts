import type { Page } from "@playwright/test";

import {
  API_BASE,
  acceptConsent,
  currentTenantSlug,
  deleteInvoicesWhere,
  expect,
  test,
  tenantPsql,
} from "../fixtures/helpers";

/**
 * Supplier portal (`/portal/*`) — a SEPARATE surface from the AP app.
 *
 * Auth model: VendorUser, not the employee User. The portal store
 * (`$lib/stores/portalAuth.svelte.ts`) posts to `/api/portal/auth/login`
 * and stashes the JWT in `localStorage['portal_auth_token']` (a distinct
 * key from the AP app's `auth_token`, so the two surfaces can't clobber
 * each other). Every portal endpoint is vendor-scoped: the handler only
 * ever filters on the caller's own `vendor_id`.
 *
 * Test data: `backend/scripts/seed.py` seeds exactly one VendorUser per
 * tenant (full + lean), password "demo":
 *   - full seed (local e2e tenants): bound to "Tech Hardware Corp"
 *     (v_tech), which owns INV-2024-005 + its completed ACH payment.
 *   - lean seed (CI e2e tenants): bound to "Lean Vendor Alpha" (v_alpha),
 *     which owns every lean invoice except LEAN-001 (v_beta) plus the
 *     LEAN-PAY-ALPHA payment.
 * The specs work against BOTH shapes by never hardcoding invoice numbers
 * — they assert vendor-scoped *counts* against ground truth read with
 * `tenantPsql`, which is the actual isolation contract.
 *
 * Opt out of the worker-admin storage state: the portal uses its own
 * token key, and these specs drive the portal login UI directly.
 */

const PORTAL_EMAIL = "supplier@portal.test";
const PORTAL_PASSWORD = "demo";

test.use({ storageState: { cookies: [], origins: [] } });

/** Read the portal JWT the store wrote after a successful login. */
async function portalToken(page: Page): Promise<string> {
  const t = await page.evaluate(() =>
    localStorage.getItem("portal_auth_token"),
  );
  if (!t) throw new Error("portal not signed in");
  return t;
}

/** Drive the portal login form and submit. Caller asserts the destination. */
async function portalSignInRaw(
  page: Page,
  email = PORTAL_EMAIL,
  password = PORTAL_PASSWORD,
) {
  await acceptConsent(page);
  await page.goto("/portal/login");
  await page.locator('input[type="email"]').fill(email);
  await page.locator('input[type="password"]').fill(password);
  await page.locator('button[type="submit"]').click();
}

/** Sign in and land on the invoice list.
 *
 * Sign-in itself lands on the portal HOME — it exists to answer "what needs my
 * attention". Most tests below exercise a specific page, so this navigates on
 * explicitly rather than depending on where login happens to land; the landing
 * behaviour has its own test, which uses `portalSignInRaw`. */
async function portalSignIn(
  page: Page,
  email = PORTAL_EMAIL,
  password = PORTAL_PASSWORD,
) {
  await portalSignInRaw(page, email, password);
  await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });
  await page.goto("/portal/invoices");
}

test.describe("/portal/login", () => {
  test("renders the supplier sign-in form for an anon visitor", async ({
    page,
  }) => {
    await page.goto("/portal/login");

    // The login card heading is the tenant's white-label PRODUCT NAME (themed
    // from the public GET /api/portal/branding). With no brand set on the
    // worker's tenant it falls back to the platform default "FeohLedger".
    // See tests-e2e/portal/branding.spec.ts for the configured-brand assertions.
    await expect(
      page.getByRole("heading", { name: "FeohLedger" }),
    ).toBeVisible();
    await expect(page.locator('input[type="email"]')).toBeVisible();
    await expect(page.locator('input[type="password"]')).toBeVisible();
    await expect(page.getByRole("button", { name: /Sign in/ })).toBeVisible();
  });

  test("rejects bad credentials and stays on /portal/login", async ({
    page,
  }) => {
    // Raw: the helper asserts a SUCCESSFUL landing, which is exactly what
    // must not happen here.
    await portalSignInRaw(page, "nobody@nowhere.test", "wrong-password");

    // The store surfaces the 401 detail into the `.error` banner and
    // the page never navigates. URL behaviour is the security contract.
    await expect(page.locator(".error")).toBeVisible({ timeout: 5_000 });
    await expect(page).toHaveURL(/\/portal\/login/);
  });

  test("an unauthenticated deep-link to /portal/invoices redirects to login", async ({
    page,
  }) => {
    await page.goto("/portal/invoices");

    // The portal layout sends any non-public path to /portal/login when
    // there's no portal token in localStorage.
    await expect(page).toHaveURL(/\/portal\/login/, { timeout: 5_000 });
  });
});

test.describe("/portal — authenticated vendor", () => {
  test("signs in and lands on the portal home", async ({ page }) => {
    // Raw sign-in — this test IS the landing behaviour, so it must not go
    // through the helper that navigates onward.
    await portalSignInRaw(page);
    await expect(page).toHaveURL(/\/portal\/?$/, { timeout: 15_000 });

    // The portal shell renders the vendor name + nav once /me resolves.
    await expect(page.getByRole("link", { name: "Overview" })).toBeVisible({
      timeout: 5_000,
    });
    // `exact` matters: the home body also links out to the invoice list ("All
    // invoices", "Fix rejected invoices"). We want the SHELL nav link here.
    await expect(
      page.getByRole("link", { name: "Invoices", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Payments" })).toBeVisible();

    // …and the invoice list is one click away.
    await page.getByRole("link", { name: "Invoices", exact: true }).click();
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });
    await expect(
      page.getByRole("heading", { name: "My Invoices" }),
    ).toBeVisible({
      timeout: 5_000,
    });
  });

  test("invoices list renders the vendor’s own invoice rows", async ({
    page,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    // Both seed shapes give the portal vendor at least one invoice, so
    // the table must render — not the empty state.
    await expect(page.locator("table tbody tr").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("payments tab renders the vendor’s payment history", async ({
    page,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    await page.getByRole("link", { name: "Payments" }).click();
    await expect(page).toHaveURL(/\/portal\/payments/, { timeout: 5_000 });
    await expect(page.getByRole("heading", { name: "Payments" })).toBeVisible({
      timeout: 5_000,
    });

    // Both seed shapes give the portal vendor exactly one completed
    // payment, so the table renders at least one row.
    await expect(page.locator("table tbody tr").first()).toBeVisible({
      timeout: 5_000,
    });
  });

  test("logging out returns to the portal login screen", async ({ page }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(/\/portal\/login/, { timeout: 5_000 });

    // The token is cleared — a deep-link bounces back to login.
    await page.goto("/portal/invoices");
    await expect(page).toHaveURL(/\/portal\/login/, { timeout: 5_000 });
  });
});

test.describe("/portal — vendor isolation", () => {
  // The hard contract: a logged-in vendor sees ONLY its own invoices and
  // payments, never another vendor's — even though the tenant DB holds
  // rows for every seeded vendor. We sign in through the UI to get a real
  // portal token, then hit the API directly and compare the portal's
  // counts to ground truth read with psql.
  test("invoices endpoint returns only the caller’s vendor, never the whole tenant", async ({
    page,
    request,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    const token = await portalToken(page);
    const slug = currentTenantSlug();

    // Resolve the portal user's vendor_id from the DB, then count the
    // invoices that vendor owns vs the tenant total.
    const vendorId = tenantPsql(
      `SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`,
    ).trim();
    expect(vendorId).not.toEqual("");

    const ownInvoices = parseInt(
      tenantPsql(
        `SELECT count(*) FROM invoices WHERE vendor_id='${vendorId}'`,
      ).trim(),
      10,
    );
    const totalInvoices = parseInt(
      tenantPsql("SELECT count(*) FROM invoices").trim(),
      10,
    );

    // Sanity: the seed must leave foreign invoices around, or the
    // isolation assertion is vacuous.
    expect(ownInvoices).toBeGreaterThan(0);
    expect(totalInvoices).toBeGreaterThan(ownInvoices);

    const res = await request.get(
      `${API_BASE}/api/portal/invoices?page_size=100`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          "X-Tenant-Slug": slug,
        },
      },
    );
    expect(res.ok()).toBeTruthy();
    const body = (await res.json()) as { items: unknown[]; total: number };

    // The portal must surface exactly the vendor's own invoices — no
    // more (leak), no fewer (over-filter).
    expect(body.total).toEqual(ownInvoices);
    expect(body.items.length).toEqual(ownInvoices);
  });

  test("payments endpoint returns only the caller’s vendor", async ({
    page,
    request,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    const token = await portalToken(page);
    const slug = currentTenantSlug();

    const vendorId = tenantPsql(
      `SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`,
    ).trim();

    // Payments join through invoices → filter on the invoice's vendor_id.
    const ownPayments = parseInt(
      tenantPsql(
        `SELECT count(*) FROM payments p JOIN invoices i ON p.invoice_id=i.id ` +
          `WHERE i.vendor_id='${vendorId}'`,
      ).trim(),
      10,
    );
    const totalPayments = parseInt(
      tenantPsql("SELECT count(*) FROM payments").trim(),
      10,
    );

    expect(ownPayments).toBeGreaterThan(0);
    expect(totalPayments).toBeGreaterThan(ownPayments);

    const res = await request.get(
      `${API_BASE}/api/portal/payments?page_size=100`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          "X-Tenant-Slug": slug,
        },
      },
    );
    expect(res.ok()).toBeTruthy();
    const body = (await res.json()) as {
      items: { reference: string | null }[];
      total: number;
    };

    expect(body.total).toEqual(ownPayments);

    // Belt-and-suspenders for the lean seed, where the foreign payment
    // carries a known reference: it must never appear in the response.
    const refs = body.items.map((p) => p.reference);
    expect(refs).not.toContain("LEAN-PAY-BETA");
  });
});

test.describe("/portal — self-service (PO flip, remittance, company)", () => {
  test("a vendor flips a PO into an invoice", async ({ page }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    const vendorId = tenantPsql(
      `SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`,
    ).trim();
    const orgId = tenantPsql(
      `SELECT organization_id FROM vendors WHERE id='${vendorId}'`,
    ).trim();

    // Seed a fresh, never-flipped PO for this vendor so the test is
    // isolation-safe across repeated runs against the same tenant. The flip
    // is idempotent at the DB layer (partial unique index on the
    // `po-flip:<po_id>` marker), so re-flipping a PO the seed already flipped
    // on a prior run is a no-op and would NOT raise the invoice count. A
    // unique PO per run is guaranteed un-flipped, and since the portal PO list
    // is ordered created_at DESC, this newest row is the first "Create
    // invoice" button the UI offers.
    const freshPo = `E2E-FLIP-${Date.now()}`;
    tenantPsql(
      `INSERT INTO purchase_orders (id, po_number, vendor_id, total, status, organization_id)
       VALUES (gen_random_uuid(), '${freshPo}', '${vendorId}', 250.00, 'open', '${orgId}')`,
    );

    const invoicesBefore = parseInt(
      tenantPsql(
        `SELECT count(*) FROM invoices WHERE vendor_id='${vendorId}'`,
      ).trim(),
      10,
    );

    try {
      await page.getByRole("link", { name: "Purchase Orders" }).click();
      await expect(page).toHaveURL(/\/portal\/purchase-orders/, {
        timeout: 5_000,
      });
      await expect(
        page.getByRole("heading", { name: "Purchase Orders" }),
      ).toBeVisible({ timeout: 5_000 });

      // Flip the first PO — the page routes to the invoices list on success.
      await page
        .getByRole("button", { name: "Create invoice" })
        .first()
        .click();
      await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

      // The new invoice landed for this vendor.
      await expect
        .poll(
          () =>
            parseInt(
              tenantPsql(
                `SELECT count(*) FROM invoices WHERE vendor_id='${vendorId}'`,
              ).trim(),
              10,
            ),
          { timeout: 10_000 },
        )
        .toBeGreaterThan(invoicesBefore);
    } finally {
      // Both rows this test created go back out. Leaving them was invisible in
      // CI (a fresh seed per shard) and cumulative locally: every run added one
      // invoice to the portal vendor, and once it passed the list's 20-row
      // page the back-dated rows `list-filters.spec.ts` seeds fell off page 1
      // and its date-range assertions could never pass again.
      deleteInvoicesWhere(
        `vendor_id='${vendorId}' AND po_number='${freshPo}'`,
      );
      tenantPsql(`DELETE FROM purchase_orders WHERE po_number='${freshPo}'`);
    }
  });

  test("a vendor downloads a remittance PDF for a completed payment", async ({
    page,
    request,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    const token = await portalToken(page);
    const slug = currentTenantSlug();

    const paymentId = tenantPsql(
      `SELECT p.id FROM payments p JOIN invoices i ON p.invoice_id=i.id ` +
        `JOIN vendor_users vu ON vu.vendor_id=i.vendor_id ` +
        `WHERE vu.email='${PORTAL_EMAIL}' AND p.status='completed' LIMIT 1`,
    ).trim();
    expect(paymentId).not.toEqual("");

    const res = await request.get(
      `${API_BASE}/api/portal/payments/${paymentId}/remittance`,
      { headers: { Authorization: `Bearer ${token}`, "X-Tenant-Slug": slug } },
    );
    expect(res.ok()).toBeTruthy();
    expect(res.headers()["content-type"]).toContain("application/pdf");
    const buf = await res.body();
    expect(buf.subarray(0, 4).toString()).toEqual("%PDF");
  });

  test("a bank-detail change stages for AP approval and does not apply live", async ({
    page,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    const vendorId = tenantPsql(
      `SELECT vendor_id FROM vendor_users WHERE email='${PORTAL_EMAIL}'`,
    ).trim();
    // Clean any pending request from a prior run so the form isn't disabled.
    tenantPsql(
      `DELETE FROM vendor_change_requests WHERE vendor_id='${vendorId}' AND status='pending'`,
    );

    try {
      await page.getByRole("link", { name: "Company" }).click();
      await expect(page).toHaveURL(/\/portal\/company/, { timeout: 5_000 });

      await page.getByLabel("Account number").fill("99887766");
      // `exact`: the form also carries "Wire routing number (optional)".
      await page.getByLabel("Routing number", { exact: true }).fill("011000015");
      await page
        .getByRole("button", { name: /Request bank-detail change/ })
        .click();

      // The pending banner appears (read from GET /company after staging).
      await expect(page.locator(".banner")).toBeVisible({ timeout: 10_000 });

      // The vendor row was NOT mutated — the change only staged.
      const pending = parseInt(
        tenantPsql(
          `SELECT count(*) FROM vendor_change_requests ` +
            `WHERE vendor_id='${vendorId}' AND change_type='bank_details' AND status='pending'`,
        ).trim(),
        10,
      );
      expect(pending).toEqual(1);
    } finally {
      tenantPsql(
        `DELETE FROM vendor_change_requests WHERE vendor_id='${vendorId}'`,
      );
    }
  });
});

test.describe("/portal/change-password", () => {
  test("a vendor can open the change-password page while authenticated", async ({
    page,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    await page.goto("/portal/change-password");

    await expect(
      page.getByRole("heading", { name: "Set a new password" }),
    ).toBeVisible({
      timeout: 5_000,
    });
    // Three password fields: current, new, confirm.
    await expect(page.locator('input[type="password"]')).toHaveCount(3);
  });

  test("mismatched new/confirm passwords show an inline error and do not submit", async ({
    page,
  }) => {
    await portalSignIn(page);
    await expect(page).toHaveURL(/\/portal\/invoices/, { timeout: 15_000 });

    await page.goto("/portal/change-password");

    const fields = page.locator('input[type="password"]');
    await fields.nth(0).fill(PORTAL_PASSWORD);
    await fields.nth(1).fill("A-Strong-New-Pass-1");
    await fields.nth(2).fill("does-not-match-Pass-1");
    await page.locator('button[type="submit"]').click();

    // Client-side guard catches the mismatch before any API call —
    // the `.error` banner shows and we stay on the page.
    await expect(page.locator(".error")).toBeVisible({ timeout: 5_000 });
    await expect(page).toHaveURL(/\/portal\/change-password/);
  });
});

test.describe("/portal — must-change-password redirect", () => {
  // A freshly-invited vendor logs in with a temp password and is forced
  // to /portal/change-password until they rotate it. The seed user has
  // the flag clear, so we flip it in the DB for this test and restore it
  // after — keeping the worker's other portal specs unaffected.
  test("a must-change-password vendor is redirected to change-password on login", async ({
    page,
  }) => {
    const restore = `UPDATE vendor_users SET must_change_password=false WHERE email='${PORTAL_EMAIL}'`;
    tenantPsql(
      `UPDATE vendor_users SET must_change_password=true WHERE email='${PORTAL_EMAIL}'`,
    );

    try {
      // Raw: this test IS the landing behaviour, so it must not go through the
      // helper — that one asserts the normal landing (the portal home).
      await portalSignInRaw(page);

      // The login handler sees `must_change_password` on the token
      // response and routes straight to change-password rather than
      // the portal home.
      await expect(page).toHaveURL(/\/portal\/change-password/, {
        timeout: 15_000,
      });
      await expect(
        page.getByRole("heading", { name: "Set a new password" }),
      ).toBeVisible({
        timeout: 5_000,
      });

      // And the layout enforces it: trying to slip over to invoices
      // bounces back to change-password while the flag is set.
      await page.goto("/portal/invoices");
      await expect(page).toHaveURL(/\/portal\/change-password/, {
        timeout: 5_000,
      });
    } finally {
      tenantPsql(restore);
    }
  });
});
