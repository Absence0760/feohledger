import { describe, expect, it } from 'vitest';
import { en } from '$lib/i18n/locales/en';
import { ROLE_LABEL_KEYS, SYSTEM_ROLES, roleLabelKey } from './admin';

/**
 * Drift guard: the frontend's system-role roster must equal the backend's, and
 * the accessor must keep its hands off a tenant's own custom role names.
 *
 * The roster half is the usual shape — `api/deps.py::ALL_ROLES` owns which
 * roles are built in, and a role missing from `ROLE_LABEL_KEYS` renders as its
 * raw slug (`ap_manager`) in a table whose every other cell is translated,
 * which is the defect this map was added to fix on three surfaces at once.
 *
 * The second half is the one worth reading. `roleLabelKey` falls an unknown
 * name back to the stored string precisely so a custom role keeps the name its
 * admin typed, and that is only safe because the backend REFUSES a custom role
 * named after a built-in (`api/admin.py`, 400 "reserved system role name"). If
 * that check ever went away, this map would start translating tenant data — one
 * org's `cfo` role rendering as `CFO` in English and `CFO` in Japanese is
 * harmless, but the mechanism is not, so the check is pinned here.
 *
 * Reads the real Python through Vite's `import.meta.glob`, the way
 * `exception.test.ts` and `notification.roster.test.ts` do — the frontend
 * deliberately carries no `@types/node`.
 */

const RAW = import.meta.glob(
	['../../../../backend/app/api/deps.py', '../../../../backend/app/api/admin.py'],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

function source(suffix: string): string {
	const hit = Object.entries(RAW).find(([path]) => path.endsWith(suffix));
	expect(hit, `${suffix} not found through import.meta.glob`).toBeDefined();
	return hit![1];
}

/** `ALL_ROLES`, resolved through the `ROLE_* = "…"` constants, in its order. */
function backendRoster(): string[] {
	const py = source('api/deps.py');
	const values: Record<string, string> = {};
	for (const m of py.matchAll(/^(ROLE_[A-Z_]+) = "([a-z0-9_]+)"$/gm)) values[m[1]] = m[2];
	expect(Object.keys(values).length, 'no ROLE_* constants parsed').toBeGreaterThan(3);
	const tuple = /^ALL_ROLES = \(([^)]*)\)$/m.exec(py);
	expect(tuple, 'ALL_ROLES tuple not found — did it move or change shape?').not.toBeNull();
	return [...tuple![1].matchAll(/ROLE_[A-Z_]+/g)].map((m) => {
		const name = m[0];
		expect(values[name], `${name} is in ALL_ROLES but has no literal`).toBeDefined();
		return values[name];
	});
}

describe('system-role roster', () => {
	it('carries the backend roster, in the backend order', () => {
		expect([...SYSTEM_ROLES]).toEqual(backendRoster());
	});

	it('labels every built-in role and nothing else', () => {
		expect(Object.keys(ROLE_LABEL_KEYS).sort()).toEqual([...SYSTEM_ROLES].sort());
	});

	it('resolves every key in the English catalogue', () => {
		for (const key of Object.values(ROLE_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('keeps the English wording the previous hardcoded map held', () => {
		// `admin/users.spec.ts` checks the "AP Manager" role checkbox by its label
		// text, so these four strings are an e2e selector as well as copy.
		const enRecord = en as Record<string, string>;
		expect(enRecord[ROLE_LABEL_KEYS.admin]).toBe('Admin');
		expect(enRecord[ROLE_LABEL_KEYS.ap_manager]).toBe('AP Manager');
		expect(enRecord[ROLE_LABEL_KEYS.ap_clerk]).toBe('AP Clerk');
		expect(enRecord[ROLE_LABEL_KEYS.cfo]).toBe('CFO');
	});
});

describe('roleLabelKey', () => {
	it('keys the built-ins', () => {
		expect(roleLabelKey('admin')).toBe('admin.roles.name.admin');
		expect(roleLabelKey('ap_manager')).toBe('admin.roles.name.apManager');
	});

	it('returns null for a custom role, so the caller prints the stored name', () => {
		// A custom role's name is text an admin typed into their own tenant. It is
		// data: translating it would rename someone's `Approver` role according to
		// whichever locale each viewer happens to be in.
		expect(roleLabelKey('Approver')).toBeNull();
		expect(roleLabelKey('Invoice Reviewer')).toBeNull();
		expect(roleLabelKey('')).toBeNull();
	});

	it('cannot be reached by a custom role, because the backend reserves the names', () => {
		// The one thing that makes the fallback sound. `POST /api/admin/roles`
		// rejects any name in ALL_ROLES before it can be stored, so no tenant owns
		// a role this map speaks for.
		const py = source('api/admin.py');
		expect(
			/if name in ALL_ROLES:\s*\n\s*raise HTTPException\(\s*status_code=400/.test(py),
			'the reserved-system-role-name check is gone — ROLE_LABEL_KEYS could now translate tenant data'
		).toBe(true);
	});
});
