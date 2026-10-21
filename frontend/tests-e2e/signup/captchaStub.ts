import type { Page } from '@playwright/test';

import { tenantBase } from '../fixtures/helpers';

/** What one `/api/signup/start` attempt carried. */
export interface StartBody {
	company_name: string;
	slug: string;
	admin_name: string;
	admin_email: string;
	captcha_token: string | null;
}

export interface CaptchaStubOptions {
	/**
	 * How hCaptcha's script answers:
	 * - `serve` — the fake explicit-render API below (the default);
	 * - `abort` — the request fails, as a content blocker makes it;
	 * - `empty` — it loads but defines nothing, as a blocker's stand-in does.
	 *
	 * A function picks per request (1-based), for a block that is lifted
	 * between visits.
	 */
	script?: 'serve' | 'abort' | 'empty' | ((attempt: number) => 'serve' | 'abort' | 'empty');
	/** The answer to the Nth (1-based) `/start`; a 200 when omitted. */
	start?: (attempt: number) => { status: number; json: unknown } | undefined;
}

const FAKE_HCAPTCHA = `(() => {
	let widgets = 0;
	const containers = {};
	window.__removedWidgets = [];
	window.__resetWidgets = [];
	window.hcaptcha = {
		render(el, opts) {
			const id = 'w' + String(++widgets);
			containers[id] = el;
			el.dataset.widget = String(widgets);
			el.dataset.renderedTheme = opts.theme || 'light';
			el.dataset.solved = 'false';
			window.__solveCaptcha = (t) => { el.dataset.solved = 'true'; opts.callback(t); };
			window.__expireCaptcha = () => { el.dataset.solved = 'false'; opts['expired-callback'](); };
			return id;
		},
		reset(id) {
			window.__resetWidgets.push(id);
			if (containers[id]) containers[id].dataset.solved = 'false';
		},
		remove(id) {
			window.__removedWidgets.push(id);
		}
	};
	const onload = new URL(document.currentScript.src).searchParams.get('onload');
	if (onload) window[onload]();
})();`;

/**
 * Turn the captcha on and stub everything external about it. Local dev and CI
 * run with no hCaptcha sitekey (the local-first default), so this path is
 * otherwise never exercised. Stubbed: the sitekey, the slug check (always
 * available), the submit endpoint (so a test can read what each attempt sent
 * and choose the answer), and hCaptcha's script — faked with the
 * explicit-render API the page uses: it renders nothing by itself, calls the
 * `onload` named in its URL, and `render` / `reset` / `remove` record what
 * they were asked. Each widget's container carries its number, the theme it
 * was drawn in and whether it shows as solved.
 *
 * Returns the bodies `/start` received, and a count of script requests.
 */
export async function stubCaptcha(
	page: Page,
	opts: CaptchaStubOptions = {}
): Promise<{ submissions: StartBody[]; scriptRequests: () => number }> {
	const submissions: StartBody[] = [];
	let scripts = 0;
	await page.route('**/api/public-config', (route) =>
		route.fulfill({
			json: { hcaptcha_sitekey: 'e2e-sitekey', tenant_url_template: tenantBase('{slug}') }
		})
	);
	await page.route(
		(url) => url.pathname === '/api/signup/slug-check',
		(route) => route.fulfill({ json: { slug: 'x', available: true, reason: null } })
	);
	await page.route(
		(url) => url.pathname === '/api/signup/start',
		async (route) => {
			submissions.push(route.request().postDataJSON());
			const answer = opts.start?.(submissions.length);
			await route.fulfill(
				answer ?? { json: { status: 'pending', message: 'We sent a verification link.' } }
			);
		}
	);
	await page.route(
		(url) => url.hostname === 'hcaptcha.com' && url.pathname === '/1/api.js',
		(route) => {
			scripts += 1;
			const mode =
				typeof opts.script === 'function' ? opts.script(scripts) : (opts.script ?? 'serve');
			if (mode === 'abort') return route.abort('blockedbyclient');
			return route.fulfill({
				contentType: 'application/javascript',
				body: mode === 'empty' ? '/* blocked */' : FAKE_HCAPTCHA
			});
		}
	);
	return { submissions, scriptRequests: () => scripts };
}

/** Call one of the fake hCaptcha's recorded callbacks, as a solve / expiry would. */
export async function captchaEvent(
	page: Page,
	event: 'solve' | 'expire',
	token = ''
): Promise<void> {
	await page.evaluate(
		([e, t]) => {
			const w = window as unknown as {
				__solveCaptcha: (t: string) => void;
				__expireCaptcha: () => void;
			};
			if (e === 'solve') w.__solveCaptcha(t);
			else w.__expireCaptcha();
		},
		[event, token] as const
	);
}

/** Fill the four signup fields. */
export async function fillSignupForm(
	page: Page,
	{ company, slug, name, email }: { company: string; slug: string; name: string; email: string }
): Promise<void> {
	await page.getByLabel('Company name').fill(company);
	await page.getByLabel('Workspace URL').fill(slug);
	await page.getByLabel('Your name').fill(name);
	await page.getByLabel('Email').fill(email);
}
