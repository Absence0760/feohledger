import { expectNoA11yViolations } from '../a11y/axe-helper';
import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';
import { captchaEvent, fillSignupForm, stubCaptcha } from './captchaStub';

/**
 * The signup captcha's failure paths (issue #635, findings 1 and 2).
 *
 * The happy path, "try again" and the theme-follow behaviour live in
 * `flow.spec.ts`. These cover what a real visitor hits when it goes wrong:
 * coming back to the page after reading the Terms, a content blocker that
 * stops hCaptcha's script, and a submission the server refuses after it has
 * already spent the captcha token.
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

const FORM = {
	company: 'Captcha Co',
	slug: 'captchaco',
	name: 'Captcha Admin',
	email: 'captcha@example.com'
};

test('landing → signup → Terms → Back draws the widget again', async ({ page }) => {
	await stubCaptcha(page);
	await page.goto('/');
	await page.getByRole('link', { name: 'Create workspace' }).first().click();
	await expect(page).toHaveURL(/\/signup$/);
	const widget = page.locator('.h-captcha');
	await expect(widget).toHaveAttribute('data-widget', '1');

	// The consent line's link, read before agreeing.
	await page.getByRole('link', { name: 'Terms of Service' }).click();
	await expect(page).toHaveURL(/\/legal\/terms$/);
	await expect(page.locator('.h-captcha')).toHaveCount(0);

	await page.goBack();
	await expect(page).toHaveURL(/\/signup$/);
	// A fresh container on a fresh mount, long after the script loaded: it
	// must get a widget of its own, or the page asks for a captcha that is
	// not there.
	await expect(widget).toHaveAttribute('data-widget', '2');
	await expect(page.getByRole('alert').filter({ hasText: "security check couldn't load" })).toHaveCount(0);
});

test('a blocked captcha script says so where the widget would be, and the form says why it cannot send', async ({
	page
}) => {
	const { submissions } = await stubCaptcha(page, { script: 'abort' });
	await page.goto('/signup');

	const notice = page.getByRole('alert').filter({ hasText: "The security check couldn't load." });
	await expect(notice).toBeVisible();
	await expect(notice).toContainText('content blocker, ad blocker or browser privacy shield');
	await expect(page.locator('.h-captcha')).toHaveCount(0);
	// Where the widget would be: just above the submit button.
	const submit = page.getByRole('button', { name: 'Send verification email' });
	const noticeBox = (await notice.boundingBox())!;
	const submitBox = (await submit.boundingBox())!;
	expect(noticeBox.y).toBeLessThan(submitBox.y);

	await fillSignupForm(page, FORM);
	await expect(page.locator('small.hint.ok')).toBeVisible();
	// The button is described by the notice, so focusing it says why.
	await expect(submit).toHaveAttribute('aria-describedby', 'captcha-blocked');
	await submit.click();
	// Not "please complete the captcha" — there is nothing to complete.
	await expect(page.getByText("The form can't be sent until the security check loads.")).toBeVisible();
	await expect(page.getByText('Please complete the captcha.')).toHaveCount(0);
	expect(submissions).toHaveLength(0);

	await expectNoA11yViolations(page);
});

test('a script that loads but brings no captcha API is the same failure', async ({ page }) => {
	await stubCaptcha(page, { script: 'empty' });
	await page.goto('/signup');
	await expect(
		page.getByRole('alert').filter({ hasText: "The security check couldn't load." })
	).toBeVisible();
	await expect(page.locator('.h-captcha')).toHaveCount(0);
});

test('a block lifted between visits loads the captcha on the next visit', async ({ page }) => {
	const stub = await stubCaptcha(page, { script: (n) => (n === 1 ? 'abort' : 'serve') });
	await page.goto('/signup');
	await expect(
		page.getByRole('alert').filter({ hasText: "The security check couldn't load." })
	).toBeVisible();

	await page.getByRole('link', { name: 'Terms of Service' }).click();
	await expect(page).toHaveURL(/\/legal\/terms$/);
	await page.goBack();

	// The failed <script> was not left behind to be mistaken for one still
	// loading: the page asked again, and this time drew the widget.
	await expect(page.locator('.h-captcha')).toHaveAttribute('data-widget', '1');
	expect(stub.scriptRequests()).toBe(2);
	await expect(
		page.getByRole('alert').filter({ hasText: "The security check couldn't load." })
	).toHaveCount(0);
});

test('a refused submission spends the token: the widget is reset and the retry needs a fresh solve', async ({
	page
}) => {
	const { submissions } = await stubCaptcha(page, {
		start: (n) =>
			n === 1
				? { status: 502, json: { detail: "Couldn't send the verification email. Please try again." } }
				: undefined
	});
	await page.goto('/signup');
	const widget = page.locator('.h-captcha');
	await expect(widget).toHaveAttribute('data-widget', '1');

	await fillSignupForm(page, FORM);
	await expect(page.locator('small.hint.ok')).toBeVisible();
	await captchaEvent(page, 'solve', 'token-1');
	await expect(widget).toHaveAttribute('data-solved', 'true');
	const submit = page.getByRole('button', { name: 'Send verification email' });
	await submit.click();

	// The server verified (and so spent) the token before failing to send.
	await expect(page.locator('.error')).toHaveText(
		"Couldn't send the verification email. Please try again."
	);
	expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1']);
	// The widget no longer looks solved, and was reset rather than left
	// holding a token the server will refuse.
	await expect(widget).toHaveAttribute('data-solved', 'false');
	expect(
		await page.evaluate(() => (window as unknown as { __resetWidgets: string[] }).__resetWidgets)
	).toEqual(['w1']);

	// The spent token does not ride along on the retry.
	await submit.click();
	await expect(page.locator('.error')).toHaveText('Please complete the captcha.');
	expect(submissions).toHaveLength(1);

	await captchaEvent(page, 'solve', 'token-2');
	await submit.click();
	await expect(page.getByRole('heading', { name: 'Check your email' })).toBeFocused();
	expect(submissions.map((b) => b.captcha_token)).toEqual(['token-1', 'token-2']);
});
