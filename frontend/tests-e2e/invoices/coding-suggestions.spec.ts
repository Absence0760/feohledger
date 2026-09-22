import {
	API_BASE,
	authedTenantHeaders,
	chartGlCode,
	deleteVendorsWhere,
	expect,
	tenantPsql,
	test
} from '../fixtures/helpers';

/**
 * Invoice modal — advisory coding suggestions from vendor history.
 *
 * `GET /api/enrichment/invoices/{id}/suggestions` derives the dominant
 * historical GL account / cost center / payment terms from this vendor's
 * APPROVED invoices. It is advisory in the strict sense: the endpoint writes
 * nothing, and the UI must not write anything either — the user applies a
 * suggestion into the form and the existing Save (with its audit row and
 * optimistic-concurrency token) stays the only writer.
 *
 * The two things worth failing a build over are therefore:
 *   1. the suggestion carries its provenance (how many prior invoices, out of
 *      how many, at what confidence) — an unexplained value asking to be
 *      trusted is the failure mode this surface exists to avoid; and
 *   2. it is NOT auto-applied: the field stays empty until the user acts, and
 *      even then nothing is persisted until Save.
 *
 * The backend needs `autofill_min_sample` (3) prior invoices at
 * `autofill_min_confidence` (60%), so the fixture creates exactly three
 * unanimous ones plus the draft under test.
 */

const MARKER = 'SUGG';

interface Created {
	vendorId: string;
	vendorName: string;
	draftId: string;
	draftNumber: string;
	glCode: string;
}

/** Three approved invoices coded identically, plus an uncoded draft — all for
 *  one fresh vendor, so the sample the panel reports is exactly 3. */
async function seedHistory(page: import('@playwright/test').Page): Promise<Created> {
	const stamp = Date.now();
	const vendorName = `${MARKER}-VENDOR-${stamp}`;
	const headers = await authedTenantHeaders(page);

	const vResp = await page.request.post(`${API_BASE}/api/vendors`, {
		headers,
		data: { name: vendorName }
	});
	expect(vResp.status()).toBe(201);
	const vendorId = ((await vResp.json()) as { id: string }).id;

	// Use a GL code the tenant's own chart carries: the modal renders a <select>
	// when GL accounts exist, and since docs/decisions.md §199 the create below
	// refuses a code that is not an active account of the invoice's chart.
	const glCode = chartGlCode();

	for (let i = 0; i < 3; i++) {
		const resp = await page.request.post(`${API_BASE}/api/invoices`, {
			headers,
			data: {
				vendor: vendorName,
				invoice_number: `${MARKER}-HIST-${stamp}-${i}`,
				amount: '250.00',
				gl_account: glCode,
				cost_center: `${MARKER}-CC`
			}
		});
		expect(resp.status()).toBe(201);
		const id = ((await resp.json()) as { id: string }).id;
		// History only counts approved-or-beyond invoices (an unreviewed draft is
		// not a human-accepted coding baseline).
		tenantPsql(
			`UPDATE invoices SET status='approved', vendor_id='${vendorId}' WHERE id='${id}'`
		);
	}

	const draftNumber = `${MARKER}-DRAFT-${stamp}`;
	const dResp = await page.request.post(`${API_BASE}/api/invoices`, {
		headers,
		data: { vendor: vendorName, invoice_number: draftNumber, amount: '250.00' }
	});
	expect(dResp.status()).toBe(201);
	const draftId = ((await dResp.json()) as { id: string }).id;
	tenantPsql(`UPDATE invoices SET vendor_id='${vendorId}' WHERE id='${draftId}'`);

	return { vendorId, vendorName, draftId, draftNumber, glCode };
}

/**
 * Remove a test vendor and everything that points at it.
 *
 * `deleteVendorsWhere` owns the vendor's whole child graph — including the
 * invoices this spec creates — so the ordering and the child list are not
 * this file's to maintain.
 */
function purgeVendor(vendorId: string): void {
	try {
		deleteVendorsWhere(`id='${vendorId}'`);
	} catch {
		/* best-effort */
	}
}

/** Open the draft invoice's modal via search → Edit. */
async function openDraft(page: import('@playwright/test').Page, number: string) {
	await page.goto('/invoices');
	const search = page.getByPlaceholder('Search invoices...');
	await search.fill(number);
	await page.waitForResponse((r) => r.url().includes('/api/invoices') && r.url().includes('search='));
	const row = page.locator('table tbody tr', { hasText: number });
	await expect(row).toBeVisible({ timeout: 10_000 });
	await row.getByRole('button', { name: 'Edit' }).click();
	const modal = page.locator('div.modal[role="dialog"][aria-label*="Edit invoice"]');
	await expect(modal).toBeVisible();
	return modal;
}

test.describe('/invoices coding suggestions', () => {
	test('the suggestion shows its provenance and is applied by the user, not automatically', async ({
		page
	}) => {
		await page.goto('/invoices');
		const seeded = await seedHistory(page);
		try {
			const modal = await openDraft(page, seeded.draftNumber);

			const panel = modal.locator('[data-testid="coding-suggestions"]');
			await expect(panel).toBeVisible({ timeout: 10_000 });
			await expect(panel).toContainText('Suggested coding');
			// Advisory framing is explicit, not implied by the layout.
			await expect(panel).toContainText('Nothing is filled in automatically');

			const glSuggestion = panel.locator('[data-testid="coding-suggestion-gl_account"]');
			await expect(glSuggestion).toBeVisible();
			await expect(glSuggestion).toContainText(seeded.glCode);
			// The provenance: the counts the score was actually derived from.
			await expect(glSuggestion).toContainText('3 of 3 prior invoices used this value');
			await expect(glSuggestion).toContainText('100.0%');

			// NOT auto-applied — the coding field is still empty while the
			// suggestion sits beside it.
			const glSelect = modal.locator('label', { hasText: 'GL Account' }).locator('select');
			const glInput = modal.locator('label', { hasText: 'GL Account' }).locator('input');
			const usesSelect = (await glSelect.count()) > 0;
			const glField = usesSelect ? glSelect : glInput;
			await expect(glField).toHaveValue('');

			// The user applies it. Only then does the field carry the value.
			await glSuggestion.getByRole('button', { name: 'Apply' }).click();
			await expect(glField).toHaveValue(seeded.glCode);

			// ...and it is explicitly still unsaved — applying is not a write.
			await expect(
				modal.locator('[data-testid="coding-suggestion-applied"]')
			).toContainText('not saved yet');

			// The server still holds no GL account: nothing was persisted.
			const stored = tenantPsql(
				`SELECT COALESCE(gl_account,'') FROM invoices WHERE id='${seeded.draftId}'`
			).trim();
			expect(stored).toBe('');

			// The applied row leaves the list (its value now sits in the field).
			await expect(glSuggestion).toBeHidden();
		} finally {
			purgeVendor(seeded.vendorId);
		}
	});

	test('saving after applying persists the suggested coding', async ({ page }) => {
		await page.goto('/invoices');
		const seeded = await seedHistory(page);
		try {
			const modal = await openDraft(page, seeded.draftNumber);
			const panel = modal.locator('[data-testid="coding-suggestions"]');
			await expect(panel).toBeVisible({ timeout: 10_000 });

			await panel
				.locator('[data-testid="coding-suggestion-cost_center"]')
				.getByRole('button', { name: 'Apply' })
				.click();

			const saved = page.waitForResponse(
				(r) =>
					r.url().includes(`/api/invoices/${seeded.draftId}`) &&
					r.request().method() === 'PATCH'
			);
			await modal.locator('button.btn-save').click();
			expect((await saved).status()).toBe(200);

			const stored = tenantPsql(
				`SELECT COALESCE(cost_center,'') FROM invoices WHERE id='${seeded.draftId}'`
			).trim();
			expect(stored).toBe(`${MARKER}-CC`);
		} finally {
			purgeVendor(seeded.vendorId);
		}
	});
});
