import { expectNoA11yViolations } from '../a11y/axe-helper';
import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';
import { fillSignupForm, stubCaptcha } from './captchaStub';

/**
 * The signup form enforces the server's rules before the server has to, and
 * puts the server's refusals next to the field they are about (issue #635,
 * finding 17).
 *
 * The rules mirrored here are `backend/app/schemas/signup.py` (both names at
 * least two characters) and `backend/app/utils/slug.py::validate_slug_format`
 * (starts with a letter; no trailing or doubled hyphen). The captcha stub is
 * used only for its endpoint stubs; the sitekey it turns on is answered by a
 * solved widget where a test submits.
 */

test.use({ storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

test.beforeEach(async ({ page }) => {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// about:blank — ignore
		}
	});
});

const FORM = { company: 'Rules Co', slug: 'rulesco', name: 'Rules Admin', email: 'rules@example.com' };

test('a workspace address that starts with a digit or hyphen is refused at once, in words, with no request', async ({
	page
}) => {
	const checks: string[] = [];
	page.on('request', (r) => {
		if (new URL(r.url()).pathname === '/api/signup/slug-check') checks.push(r.url());
	});
	await page.goto('/signup');
	const slug = page.getByLabel('Workspace URL');
	const submit = page.getByRole('button', { name: 'Send verification email' });

	for (const bad of ['9lives', '-acme']) {
		await slug.fill(bad);
		await expect(page.locator('#slug-hint')).toHaveText('Must start with a letter.');
		await expect(slug).toHaveAttribute('aria-invalid', 'true');
		await expect(submit).toBeDisabled();
	}

	// Fixing it clears the refusal and goes on to the availability check.
	await slug.fill('lives9');
	await expect(slug).toHaveAttribute('aria-invalid', 'false');
	await expect(page.locator('small.hint.ok, small.hint.bad')).toBeVisible();
	expect(checks.every((u) => !/slug=(9lives|-acme)/.test(u))).toBe(true);
});

for (const [value, message] of [
	['acme-', "Can't end with a hyphen."],
	['ac--me', "Can't have two hyphens in a row."]
] as const) {
	test(`"${value}" is refused by the form's own rule, not a round trip`, async ({ page }) => {
		const checks: string[] = [];
		page.on('request', (r) => {
			if (new URL(r.url()).pathname === '/api/signup/slug-check') checks.push(r.url());
		});
		await page.goto('/signup');
		await page.getByLabel('Workspace URL').fill(value);
		await expect(page.locator('#slug-hint')).toHaveText(message);
		await expect(page.getByRole('button', { name: 'Send verification email' })).toBeDisabled();
		expect(checks).toEqual([]);
	});
}

test('a one-character company or person name is stopped by the browser before it is sent', async ({
	page
}) => {
	const { submissions } = await stubCaptcha(page);
	await page.goto('/signup');
	await fillSignupForm(page, { ...FORM, company: 'A', name: 'B' });
	await expect(page.locator('small.hint.ok')).toBeVisible();
	await page.getByRole('button', { name: 'Send verification email' }).click();

	for (const label of ['Company name', 'Your name']) {
		expect(
			await page.getByLabel(label).evaluate((el) => (el as HTMLInputElement).validity.tooShort)
		).toBe(true);
	}
	await expect(page.getByRole('heading', { name: 'Check your email' })).toHaveCount(0);
	expect(submissions).toHaveLength(0);
});

test('a taken address (409) is shown on the address field, which takes focus', async ({ page }) => {
	await stubCaptcha(page, {
		start: () => ({ status: 409, json: { detail: "'rulesco' is already taken." } })
	});
	await page.goto('/signup');
	await fillSignupForm(page, FORM);
	await expect(page.locator('small.hint.ok')).toBeVisible();
	await page.evaluate(() => (window as unknown as { __solveCaptcha: (t: string) => void }).__solveCaptcha('t'));
	await page.getByRole('button', { name: 'Send verification email' }).click();

	const slug = page.getByLabel('Workspace URL');
	await expect(page.locator('#slug-hint')).toHaveText("'rulesco' is already taken.");
	await expect(slug).toHaveAttribute('aria-invalid', 'true');
	// The host suffix is part of the description, so a screen reader hears
	// which address the slug becomes.
	await expect(slug).toHaveAttribute('aria-describedby', 'slug-suffix slug-hint');
	await expect(slug).toBeFocused();
	// Not repeated in the banner at the top.
	await expect(page.locator('.error')).toHaveCount(0);
});

test('a validation refusal (422) lands under the field it names; an unplaceable one stays in the banner', async ({
	page
}) => {
	await stubCaptcha(page, {
		start: (n) =>
			n === 1
				? {
						status: 422,
						json: {
							detail: [
								{
									loc: ['body', 'admin_name'],
									type: 'string_too_short',
									msg: 'String should have at least 2 characters',
									ctx: { min_length: 2 }
								}
							]
						}
					}
				: { status: 422, json: { detail: 'Enter a valid email address.' } }
	});
	await page.goto('/signup');
	await fillSignupForm(page, FORM);
	await expect(page.locator('small.hint.ok')).toBeVisible();
	const solve = () =>
		page.evaluate(() => (window as unknown as { __solveCaptcha: (t: string) => void }).__solveCaptcha('t'));
	await solve();
	const submit = page.getByRole('button', { name: 'Send verification email' });
	await submit.click();

	const name = page.getByLabel('Your name');
	const nameError = page.locator('#signup-name-error');
	// In the reader's language, not Pydantic's "String should have…".
	await expect(nameError).toHaveText('Must be at least 2 characters.');
	await expect(name).toHaveAttribute('aria-invalid', 'true');
	await expect(name).toHaveAttribute('aria-describedby', 'signup-name-error');
	await expect(name).toBeFocused();
	await expect(page.locator('.error')).toHaveCount(0);
	await expectNoA11yViolations(page);
	// The label is still just the label: the error is the field's description.
	await expect(page.getByRole('textbox', { name: 'Your name', exact: true })).toBeVisible();

	// Editing the field clears its error.
	await name.fill('Rules Admin');
	await expect(nameError).toHaveCount(0);
	await expect(name).not.toHaveAttribute('aria-invalid', 'true');

	// A plain-string refusal names no field: it goes in the banner.
	await solve();
	await submit.click();
	await expect(page.locator('.error')).toHaveText('Enter a valid email address.');
});

test('submitting inside the slug check\'s pause still applies the address rule', async ({ page }) => {
	const { submissions } = await stubCaptcha(page);
	await page.goto('/signup');
	await fillSignupForm(page, { ...FORM, slug: 'rules-' });
	// Straight from typing to Enter: the debounced check has not run yet.
	await page.getByLabel('Email').press('Enter');
	await expect(page.locator('#slug-hint')).toHaveText("Can't end with a hyphen.");
	await expect(page.getByLabel('Workspace URL')).toBeFocused();
	expect(submissions).toHaveLength(0);
});

test('a 422 naming the slug lands on the address field; items naming no field stay in the banner beside it', async ({
	page
}) => {
	await stubCaptcha(page, {
		start: () => ({
			status: 422,
			json: {
				detail: [
					{ loc: ['body', 'slug'], type: 'value_error', msg: "'admin' is reserved and cannot be used." },
					{ loc: ['body', 'locale'], type: 'string_too_long', msg: 'too long', ctx: { max_length: 16 } }
				]
			}
		})
	});
	await page.goto('/signup');
	await fillSignupForm(page, FORM);
	await expect(page.locator('small.hint.ok')).toBeVisible();
	await page.evaluate(() => (window as unknown as { __solveCaptcha: (t: string) => void }).__solveCaptcha('t'));
	await page.getByRole('button', { name: 'Send verification email' }).click();

	const slug = page.getByLabel('Workspace URL');
	await expect(page.locator('#slug-hint')).toHaveText("'admin' is reserved and cannot be used.");
	await expect(slug).toHaveAttribute('aria-invalid', 'true');
	await expect(slug).toBeFocused();
	// The left-over keeps its field name, since it is shown away from any field.
	await expect(page.locator('.error')).toHaveText('locale: Must be at most 16 characters.');
});
