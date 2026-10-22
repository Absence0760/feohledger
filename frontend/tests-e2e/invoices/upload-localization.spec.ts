import { expect, test } from '../fixtures/helpers';

/**
 * /invoices — upload feedback is in the reader's language (#635).
 *
 * `handleUpload` built its progress line and every toast as English template
 * literals — "Uploading 2 of 2...", "Failed to upload b.pdf: …", "Uploaded 1 of
 * 2 invoices (1 failed)" — with a hand-rolled `invoice${total > 1 ? 's' : ''}`
 * plural, while the catalogue already carried `invoices.action.uploadingProgress`
 * unused. A German operator got German buttons and English feedback.
 *
 * Both uploads are stubbed: the code under test is the client's reporting of a
 * mixed result, and stubbing keeps a real invoice (and a real extraction) out of
 * the tenant. The requests are held until the progress line has been read,
 * because it only exists while they are in flight. The list refetch is real.
 * `untranslatedCopy.test.ts` (`TOASTS_TRANSLATED`) is the static half.
 */

const FILES = [
	{ name: 'a.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 a') },
	{ name: 'b.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 b') }
];

test.describe('/invoices upload feedback is localized', () => {
	test('a de reader sees German progress, a German failure and a German plural summary', async ({
		page
	}) => {
		let release!: () => void;
		const held = new Promise<void>((resolve) => (release = resolve));
		let seen = 0;
		await page.route('**/api/invoices/upload', async (route) => {
			const n = ++seen;
			await held;
			if (n === 1) {
				// Not `pending`, so the page does not start polling an id that
				// does not exist.
				return route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({ id: '00000000-0000-0000-0000-000000000635', status: 'ready_for_review' })
				});
			}
			return route.fulfill({
				status: 500,
				contentType: 'application/json',
				body: JSON.stringify({ detail: 'kaputt' })
			});
		});

		await page.addInitScript(() => {
			localStorage.setItem('feoh_locale', 'de');
		});
		await page.goto('/invoices');
		// By class, not by name: the button's name IS the text under test, and it
		// changes mid-spec.
		const uploadBtn = page.locator('button.btn-upload');
		await expect(uploadBtn).toHaveText('+ Rechnungen hochladen');
		await expect(uploadBtn).toBeEnabled();

		await page.locator('input[type="file"]').setInputFiles(FILES);

		// In flight: the keyed progress line, not "Uploading 2 of 2...".
		await expect(uploadBtn).toHaveText('2 von 2 werden hochgeladen …');
		await expect.poll(() => seen).toBe(2);
		release();

		const toasts = page.locator('.toast-text');
		await expect(toasts.filter({ hasText: 'b.pdf konnte nicht hochgeladen werden: kaputt' })).toBeVisible();
		// `total` is 2, so the plural arm is "Rechnungen" — the old literal
		// pluralised by hand and only ever in English.
		await expect(
			toasts.filter({ hasText: '1 von 2 Rechnungen hochgeladen (1 fehlgeschlagen)' })
		).toBeVisible();
		await expect(toasts.filter({ hasText: /Uploaded|Failed to upload/ })).toHaveCount(0);
		await expect(uploadBtn).toHaveText('+ Rechnungen hochladen');
	});
});
