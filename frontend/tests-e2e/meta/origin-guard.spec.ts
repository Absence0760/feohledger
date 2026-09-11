import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

import {
	TENANT_ROOT_URL,
	tenantOrigin,
	VANITY_ORIGIN,
	WEB_HOSTNAME,
	WEB_PORT_SUFFIX,
	WEB_PROTOCOL
} from '../fixtures/env';
import { escapeRegExp } from '../fixtures/helpers';

/**
 * Source guard: no spec writes down where the app is served.
 *
 * The web origin and the API base used to be literals — `:7777` in the
 * per-worker `baseURL` fixture, `:8000` in a scatter of
 * `process.env.PUBLIC_API_URL ?? …` repeats — spread across the spec tree.
 * `fixtures/env.ts` made one origin the source of truth and derived the rest
 * (tenant subdomains, the vanity IP literal, the landing-URL pattern, the
 * dev server's port) from it.
 *
 * That matters because a git **worktree** isolates *files, not ports*, and
 * `playwright.config.ts` sets `reuseExistingServer` locally: a second session
 * keeping the default port finds the primary checkout's dev server already
 * listening and tests *that* build, silently, with every spec green against
 * code it did not change. Two round-24 agents hit exactly that; one stopped and
 * restarted a server it did not own. A single re-introduced literal brings the
 * whole failure back, because it only has to pin one spec to the wrong port for
 * that spec to cross servers mid-suite.
 *
 * Detection is source-level rather than type-level for the same reason
 * `meta/teardown-guard.spec.ts` is: the offending value is a string literal, so
 * no compiler can see it. Comments are stripped first, so prose *about* the
 * default port — of which there is a fair amount, deliberately — does not trip
 * it.
 *
 * The behavioural half below (anchoring, escaping) is here rather than in a
 * unit test because `tests-e2e/` has no unit runner: `vitest.config.ts` scopes
 * itself to `src/`, and widening it would double-collect these files, since
 * Playwright's default `testMatch` covers `*.test.ts` as well as `*.spec.ts`.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const E2E_ROOT = join(HERE, '..');

/** The one file allowed to spell the defaults — it is the implementation. */
const OWNER = join('fixtures', 'env.ts');

/** Files the scan skips, and why. Kept to exactly two: widening this set is
 *  how the literals come back. */
const EXEMPT = new Set([
	OWNER,
	// This file: the known-bad examples it asserts against are string literals,
	// not comments, so the detector correctly sees them.
	join('meta', 'origin-guard.spec.ts')
]);

/**
 * Strip block and line comments so prose describing the default port — of which
 * there is a fair amount, deliberately — is not itself flagged.
 *
 * The line-comment half requires the character before `//` to not be a colon,
 * which `meta/teardown-guard.spec.ts`'s otherwise-identical helper does not.
 * That guard hunts for SQL, and SQL contains no `//`; every pattern HERE is a
 * substring of a URL, and a naive strip would cut `'http://localhost:7777'`
 * down to `'http:` — silently blinding all four scans to the exact literal they
 * exist to find. That is a false NEGATIVE, so it cannot be left to "crude by
 * design"; the self-test below is what holds it.
 */
export function stripComments(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

/** Every `.ts` under `tests-e2e/` the guard is responsible for. */
function scannedSources(): Array<{ path: string; source: string }> {
	const out: Array<{ path: string; source: string }> = [];
	const walk = (dir: string) => {
		for (const entry of readdirSync(dir, { withFileTypes: true })) {
			const full = join(dir, entry.name);
			if (entry.isDirectory()) {
				if (entry.name === 'node_modules' || entry.name === '.auth') continue;
				walk(full);
				continue;
			}
			if (!entry.name.endsWith('.ts')) continue;
			const rel = full.slice(E2E_ROOT.length + 1);
			if (EXEMPT.has(rel)) continue;
			out.push({ path: rel, source: readFileSync(full, 'utf8') });
		}
	};
	walk(E2E_ROOT);
	return out;
}

/**
 * One banned shape: what to hunt for, what to use instead, and a line that MUST
 * trip it. The fixture lives on the shape rather than in a lookup beside it so
 * that adding a pattern without a fixture is a type error, not a guard that
 * quietly never proved it can fire.
 */
type BannedShape = {
	readonly what: string;
	readonly pattern: RegExp;
	readonly instead: string;
	readonly knownBad: string;
};

const BANNED: readonly BannedShape[] = [
	{
		what: 'hardcodes the web port',
		pattern: /:7777\b/,
		instead: 'import WEB_ORIGIN / tenantOrigin() from fixtures/env.ts',
		knownBad: "await page.goto('http://acme.localhost:7777/invoices');"
	},
	{
		what: 'hardcodes the API port',
		pattern: /:8000\b/,
		instead: 'import API_BASE from fixtures/env.ts',
		knownBad: "const api = process.env.API ?? 'http://localhost:8000';"
	},
	{
		// The port is only half of it — a spec spelling out its own
		// `http://<slug>.localhost` pins the scheme and host shape too, and it is
		// that shape which carries the port back in when someone re-adds one.
		//
		// Anchored on `http://` on purpose: the seeded logins are
		// `demo+<role>@<slug>.localhost`, which share the suffix and are not
		// origins. Flagging those would push the guard towards being switched off
		// rather than obeyed.
		what: 'hand-builds a tenant origin',
		pattern: /https?:\/\/\$\{[^}]+\}\.[a-z]/i,
		instead: 'use tenantOrigin(slug) from fixtures/env.ts',
		knownBad: 'const base = `http://${slug}.localhost`;'
	},
	{
		// This is how the scatter started: a spec reading the variable re-declares
		// the default beside it, and the two drift apart the moment one is edited.
		what: 'reads the origin env vars directly',
		pattern: /process\.env\.(E2E_WEB_ORIGIN|PUBLIC_API_URL)\b/,
		instead: 'import WEB_ORIGIN / API_BASE from fixtures/env.ts',
		knownBad: "const base = process.env.PUBLIC_API_URL ?? 'http://127.0.0.1:9999';"
	}
];

/** Does this source re-introduce `shape`, ignoring anything in a comment? */
export function detects(source: string, shape: BannedShape): boolean {
	return shape.pattern.test(stripComments(source));
}

test.describe('e2e origin discipline', () => {
	for (const shape of BANNED) {
		const { what, instead } = shape;
		test(`no spec ${what}`, () => {
			const offenders = scannedSources()
				.filter(({ source }) => detects(source, shape))
				.map(({ path }) => path);
			expect(offenders, `${instead} (fixtures/env.ts owns the value)`).toEqual([]);
		});
	}

	test('the detector flags a known-bad file and clears a known-good one', () => {
		// A clean scan over clean files proves nothing about the detector. This
		// is not a formality: the first version of `stripComments` was copied
		// from the sibling teardown guard, and because every pattern here lives
		// inside a URL, its unconditional `//` strip cut each known-bad line
		// down to `'http:` — three of the four scans passed against a file that
		// had just re-introduced the literal.
		const good = [
			"import { API_BASE, tenantOrigin } from '../fixtures/env';",
			'// Never write http://acme.localhost:7777 or :8000 here — see fixtures/env.ts.',
			'/* The old shape was `http://${slug}.localhost:7777`, read off process.env.PUBLIC_API_URL. */',
			"await page.goto(`${tenantOrigin('acme')}/invoices`);",
			'await request.get(`${API_BASE}/api/invoices`);'
		].join('\n');

		for (const shape of BANNED) {
			expect(detects(shape.knownBad, shape), `${shape.what}: known-bad not flagged`).toBe(true);
			expect(detects(good, shape), `${shape.what}: known-good flagged`).toBe(false);
		}
	});

	test('the guard is actually looking at the spec tree', () => {
		// A walk that silently found nothing — a renamed directory, a changed
		// layout, an EXEMPT entry that grew — would make every assertion above
		// vacuously true.
		const scanned = scannedSources();
		expect(scanned.length).toBeGreaterThan(200);
		expect(scanned.map(({ path }) => path)).toContain(join('auth', 'login.spec.ts'));
	});

	test('every origin shape derives from the configured web origin', () => {
		// One knob for the whole web half: a worktree that moved only some of
		// these would have its specs talking to two servers at once.
		expect(tenantOrigin('e2e1')).toBe(`${WEB_PROTOCOL}//e2e1.${WEB_HOSTNAME}${WEB_PORT_SUFFIX}`);
		expect(VANITY_ORIGIN).toBe(`${WEB_PROTOCOL}//127.0.0.1${WEB_PORT_SUFFIX}`);
		// `URL.port` is empty when the origin uses the scheme's default, which is
		// why the suffix exists at all — a naive `:${port}` builds `…example:/`.
		expect(WEB_PORT_SUFFIX === '' || /^:\d+$/.test(WEB_PORT_SUFFIX)).toBe(true);
	});

	test('TENANT_ROOT_URL means the tenant ROOT, not any page under it', () => {
		// The specs using it wait for a sign-in redirect to *complete*. Without
		// the trailing anchor the pattern also matches `/login/mfa`, so the wait
		// returns before the navigation it is waiting for.
		const root = tenantOrigin('e2e1');
		expect(TENANT_ROOT_URL.test(`${root}/`)).toBe(true);
		expect(TENANT_ROOT_URL.test(root)).toBe(true);
		expect(TENANT_ROOT_URL.test(`${root}/login`)).toBe(false);
		expect(TENANT_ROOT_URL.test(`${root}/invoices`)).toBe(false);
	});

	test('escapeRegExp does not let one tenant satisfy another tenant pattern', () => {
		// This is what the cross-tenant isolation specs assert identity with, so
		// "the slugs are alphanumeric in practice" is the wrong thing to lean on.
		const acme = new RegExp(`^${escapeRegExp(tenantOrigin('acme'))}/?$`);
		expect(acme.test(`${tenantOrigin('acme')}/`)).toBe(true);
		expect(acme.test(`${tenantOrigin('techflow')}/`)).toBe(false);
		// Unescaped, the `.` in `acme.localhost` would also match `acmeXlocalhost`.
		expect(acme.test(`${WEB_PROTOCOL}//acmeX${WEB_HOSTNAME}${WEB_PORT_SUFFIX}/`)).toBe(false);

		// The backslash is the one an escape-the-dots shortcut leaves out: `\` is
		// itself a metacharacter, so a dots-only escape passes it through and
		// builds a DIFFERENT pattern. `\d` is the sharp case — left alone it
		// matches any digit, so `a1.localhost` would satisfy a pattern meant to
		// pin exactly `a\d.localhost`.
		expect(escapeRegExp(String.raw`a\db.c`)).toBe(String.raw`a\\db\.c`);
		const backslash = new RegExp(`^${escapeRegExp(String.raw`http://a\d.localhost`)}/?$`);
		expect(backslash.test('http://a1.localhost/')).toBe(false);
		expect(backslash.test(String.raw`http://a\d.localhost/`)).toBe(true);
	});
});
