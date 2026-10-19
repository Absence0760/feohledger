import { API_BASE, expect, test } from '../fixtures/helpers';
import { SERVICES, skipUnlessReachable } from '../fixtures/services';
import type { Page } from '@playwright/test';

/**
 * Outbound email end-to-end through the real Mailpit sink.
 *
 * Drives the email-backed signup flow: POST /api/signup/start sends the
 * verification email via the `smtp` adapter → it lands in Mailpit → we pull the
 * verify token out of the captured message → POST /api/signup/complete
 * provisions the tenant (and sends the welcome email, which also lands).
 *
 * Gated on Mailpit being reachable. It ALSO requires the backend to be running
 * with FEOH_EMAIL_PROVIDER=smtp (pointing at Mailpit) — locally:
 *   pnpm mail:up && FEOH_EMAIL_PROVIDER=smtp FEOH_SMTP_PORT=1025 pnpm dev:backend
 * The CI e2e job sets that env. If the backend is on the `console` adapter the
 * verification email never arrives and the test fails (a setup error, surfaced
 * loudly) rather than silently passing.
 */

const MAILPIT = 'http://localhost:8025';

interface MailpitMessage {
	ID: string;
	Subject: string;
	To: Array<{ Address: string }>;
}

/** Find a captured message addressed to `email`, or null. */
async function findMessageTo(page: Page, email: string): Promise<MailpitMessage | null> {
	const resp = await page.request.get(`${MAILPIT}/api/v1/search`, {
		params: { query: `to:${email}` }
	});
	if (!resp.ok()) return null;
	const body = (await resp.json()) as { messages: MailpitMessage[] };
	return body.messages[0] ?? null;
}

/** Full plaintext body of a captured message. */
async function messageText(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${MAILPIT}/api/v1/message/${id}`);
	const body = (await resp.json()) as { Text: string };
	return body.Text;
}

/** HTML part of a captured message. */
async function messageHtml(page: Page, id: string): Promise<string> {
	const resp = await page.request.get(`${MAILPIT}/api/v1/message/${id}`);
	const body = (await resp.json()) as { HTML: string };
	return body.HTML;
}

test.describe('Outbound email via Mailpit (signup)', () => {
	test.beforeEach(async () => {
		await skipUnlessReachable(SERVICES.mailpit);
	});

	test('signup sends a verification email that lands in Mailpit, and the link completes signup', async ({
		page
	}) => {
		const slug = `e2email${Date.now()}`;
		const email = `admin@${slug}.example`;

		// 1. Kick off signup — backend sends the verification email over SMTP.
		const start = await page.request.post(`${API_BASE}/api/signup/start`, {
			data: {
				company_name: 'Mailpit E2E Co',
				slug,
				admin_name: 'Mailpit Admin',
				admin_email: email
			}
		});
		expect(start.status(), await start.text()).toBe(200);

		// 2. The verification email lands in Mailpit (poll a real signal, no sleep).
		let msg: MailpitMessage | null = null;
		await expect
			.poll(async () => {
				msg = await findMessageTo(page, email);
				return msg?.Subject ?? null;
			}, { timeout: 15_000 })
			.toBe('Verify your FeohLedger workspace');
		expect(msg!.To[0].Address).toBe(email);

		// 3. Pull the verify token out of the captured email body.
		const text = await messageText(page, msg!.ID);
		const tokenMatch = text.match(/\/verify\?token=([A-Za-z0-9_-]+)/);
		expect(tokenMatch, 'verify link present in email body').toBeTruthy();
		const token = tokenMatch![1];

		// 4. Completing with that token provisions the tenant.
		const complete = await page.request.post(`${API_BASE}/api/signup/complete`, {
			data: { token }
		});
		expect(complete.status(), await complete.text()).toBe(200);
		const body = (await complete.json()) as {
			status: string;
			slug: string;
			set_password_url: string | null;
		};
		expect(body.status).toBe('provisioned');
		expect(body.slug).toBe(slug);
		expect(body.set_password_url).toMatch(/\/login\/reset-password\?token=[^&]+&mode=set$/);

		// 5. The welcome email lands too, carrying that same set-password link and
		//    no password (decisions §282).
		let welcome: MailpitMessage | null = null;
		await expect
			.poll(async () => {
				const resp = await page.request.get(`${MAILPIT}/api/v1/search`, {
					params: { query: `to:${email}` }
				});
				const j = (await resp.json()) as { messages: MailpitMessage[] };
				welcome = j.messages.find((m) => m.Subject.startsWith('Your FeohLedger workspace')) ?? null;
				return welcome?.ID ?? null;
			}, { timeout: 15_000 })
			.not.toBeNull();
		const welcomeText = await messageText(page, welcome!.ID);
		expect(welcomeText).toContain(body.set_password_url!);
		expect(welcomeText).not.toMatch(/Password:/);
		expect(welcomeText).toMatch(/expires in \d+ hours/);
		// The HTML part's button is the same server-built link.
		const html = await messageHtml(page, welcome!.ID);
		expect(html).toContain(`href="${body.set_password_url!.replace(/&/g, '&amp;')}"`);
		expect(html).toContain('>Set your password</a>');
	});
});
