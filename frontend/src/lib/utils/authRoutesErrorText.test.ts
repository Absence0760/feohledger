import { describe, expect, it } from 'vitest';
import { apiErrorMessage } from './apiError.ts';

/**
 * Source-scan ratchet: the signed-out auth surface never shows a caught
 * error's own `.message`.
 *
 * Sign-in, MFA, password reset, signup, verify, find-workspace and the
 * supplier portal's equivalents are the first pages anyone sees, in their own
 * language. `err instanceof Error ? err.message : m(…)` put a network
 * `TypeError` ("Failed to fetch"), a passkey prompt's `DOMException`, a
 * bodyless proxy 502 ("API error 502") or a 5xx "Internal Server Error" there
 * verbatim, in English. `utils/apiError.ts::apiErrorMessage` is the one way to
 * read a caught error for display on these pages: the server's sentence when it
 * sent one for the user (localized when coded), else the page's own localized
 * fallback. The permissive `errorMessage` is banned here too, since it passes
 * any `Error`'s message through.
 *
 * Read through Vite's `import.meta.glob`, like `errorDetailDisplay.test.ts`.
 */

const RAW = import.meta.glob(
	[
		'/src/routes/login/**/*.svelte',
		'/src/routes/signup/**/*.svelte',
		'/src/routes/verify/**/*.svelte',
		'/src/routes/find-workspace/**/*.svelte',
		'/src/routes/change-password/**/*.svelte',
		'/src/routes/portal/login/**/*.svelte',
		'/src/routes/portal/forgot-password/**/*.svelte',
		'/src/routes/portal/reset-password/**/*.svelte',
		'/src/routes/portal/change-password/**/*.svelte',
		'/src/lib/components/auth/**/*.svelte'
	],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

const PATTERNS: { name: string; re: RegExp }[] = [
	{ name: 'a caught error’s `.message`', re: /(?<![.'"`\w])\w+\??\.message\b/g },
	{
		name: 'the permissive `errorMessage` helper',
		re: /import\s*\{[^}]*\berrorMessage\b[^}]*\}\s*from\s*'#lib\/utils\/apiError\.ts'/g
	}
];

/** Drop comments so prose that names the anti-pattern is not a hit. */
function stripComments(src: string): string {
	return src
		.replace(/<!--[\s\S]*?-->/g, (c) => c.replace(/[^\n]/g, ' '))
		.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
		.replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');
}

function findViolations(files: Record<string, string>): string[] {
	const hits: string[] = [];
	for (const [path, raw] of Object.entries(files)) {
		const src = stripComments(raw);
		for (const { name, re } of PATTERNS) {
			for (const match of src.matchAll(re)) {
				const line = src.slice(0, match.index).split('\n').length;
				hits.push(`${path}:${line} — ${name}: ${match[0].trim()}`);
			}
		}
	}
	return hits;
}

describe('signed-out auth pages show no raw error message', () => {
	it('scans the whole auth surface', () => {
		const paths = Object.keys(RAW);
		for (const expected of [
			'/src/routes/login/+page.svelte',
			'/src/routes/login/mfa/+page.svelte',
			'/src/routes/login/forgot-password/+page.svelte',
			'/src/routes/signup/+page.svelte',
			'/src/routes/verify/+page.svelte',
			'/src/routes/find-workspace/+page.svelte',
			'/src/routes/portal/login/+page.svelte',
			'/src/lib/components/auth/SetPasswordLink.svelte',
			'/src/lib/components/auth/AuthCallback.svelte'
		]) {
			expect(paths).toContain(expected);
		}
	});

	it('no auth page reads `.message` off a caught error — use apiErrorMessage(err, fallback)', () => {
		expect(findViolations(RAW)).toEqual([]);
	});

	it('catches each shape it claims to (so a green run means something)', () => {
		const bad = {
			'a.svelte': "error = err instanceof Error ? err.message : m('auth.login.failed');",
			'b.svelte': "if (e instanceof Error && e?.message) serverMessage = e.message;",
			'c.svelte': "import { errorMessage as t } from '#lib/utils/apiError.ts';"
		};
		const hits = findViolations(bad);
		expect(hits.filter((h) => h.startsWith('a.svelte'))).toHaveLength(1);
		expect(hits.filter((h) => h.startsWith('b.svelte'))).toHaveLength(2);
		expect(hits.filter((h) => h.startsWith('c.svelte'))).toHaveLength(1);
		// Prose about the rule is not a hit.
		expect(findViolations({ 'd.svelte': '// never err.message here\n<!-- nor e.message -->' })).toEqual([]);
		// Nor is a message-catalogue key that ends in `.message`.
		expect(findViolations({ 'e.svelte': "m('auth.callback.message')" })).toEqual([]);
	});
});

describe('apiErrorMessage', () => {
	const apiError = (message: string, status: number, detail?: unknown, code: string | null = null) =>
		Object.assign(new Error(message), { name: 'ApiError', status, code, params: {}, detail });

	it('shows the server’s sentence for a refusal meant for the user', () => {
		expect(apiErrorMessage(apiError('Invalid credentials', 401, 'Invalid credentials'), 'fb')).toBe(
			'Invalid credentials'
		);
		// A coded refusal: `message` is already the localized sentence.
		const coded = { code: 'rate_limited', message: 'Too many attempts', params: {} };
		expect(apiErrorMessage(apiError('Zu viele Versuche', 429, coded), 'fb')).toBe('Zu viele Versuche');
	});

	it('falls back for a network failure, a passkey prompt and a hand-thrown Error', () => {
		expect(apiErrorMessage(new TypeError('Failed to fetch'), 'fb')).toBe('fb');
		expect(
			apiErrorMessage(Object.assign(new Error('The operation was aborted.'), { code: 20 }), 'fb')
		).toBe('fb');
		expect(apiErrorMessage(new Error('Passkey authentication was cancelled'), 'fb')).toBe('fb');
		expect(apiErrorMessage('boom', 'fb')).toBe('fb');
	});

	it('shows a CODED 5xx refusal: it was written for the user', () => {
		const coded = { code: 'signup_email_send_failed', message: "Couldn't send", params: {} };
		expect(
			apiErrorMessage(apiError('Konnte nicht senden', 502, coded, 'signup_email_send_failed'), 'fb')
		).toBe('Konnte nicht senden');
	});

	it('falls back for a bodyless response and any uncoded 5xx', () => {
		expect(apiErrorMessage(apiError('API error 502', 502), 'fb')).toBe('fb');
		expect(apiErrorMessage(apiError('Unauthorized', 401), 'fb')).toBe('fb');
		expect(apiErrorMessage(apiError('Internal Server Error', 500, 'Internal Server Error'), 'fb')).toBe(
			'fb'
		);
	});
});
