/**
 * The accounts `backend/scripts/seed.py` creates, for the login page's
 * dev-only "Dev accounts" panel (`DevAccountsPanel.svelte`).
 *
 * DEV ONLY. This module is imported solely from inside an
 * `import.meta.env.DEV` branch, so `pnpm build` drops it and the S3/CloudFront
 * bundle never carries a seeded credential. `scripts/check_dev_accounts_stripped.mjs`
 * proves that against a real build in CI by searching `frontend/build` for
 * every address below — keep the addresses written out as plain literals here
 * (one per line, no string building) so that check can read them.
 *
 * Mirrors seed.py — read it, don't guess: every employee password is `demo`,
 * acme has one user per role, techflow an admin and a clerk, and each e2e
 * worker tenant (`e2e1`…`e2eN`) has `demo+<role>@<slug>.localhost` for
 * admin / manager / clerk / cfo.
 */

import type { SystemRole } from '#lib/types/admin.ts';

export interface DevAccount {
	email: string;
	password: string;
	role: SystemRole;
}

const SEED_PASSWORD = 'demo';

const FIXED: Record<string, DevAccount[]> = {
	acme: [
		{ email: 'demo@acme.com', password: SEED_PASSWORD, role: 'admin' },
		{ email: 'demo+apmanager@acme.com', password: SEED_PASSWORD, role: 'ap_manager' },
		{ email: 'demo+apclerk@acme.com', password: SEED_PASSWORD, role: 'ap_clerk' },
		{ email: 'demo+cfo@acme.com', password: SEED_PASSWORD, role: 'cfo' }
	],
	techflow: [
		{ email: 'admin@techflow.com', password: SEED_PASSWORD, role: 'admin' },
		{ email: 'clerk@techflow.com', password: SEED_PASSWORD, role: 'ap_clerk' }
	]
};

/** seed.py's e2e role keys, and the system role each one holds. */
const E2E_ROLES: [string, SystemRole][] = [
	['admin', 'admin'],
	['manager', 'ap_manager'],
	['clerk', 'ap_clerk'],
	['cfo', 'cfo']
];

/** The seeded accounts for the tenant the page is on; `[]` for any other host. */
export function devAccountsFor(slug: string | null): DevAccount[] {
	if (!slug) return [];
	if (FIXED[slug]) return FIXED[slug];
	if (/^e2e\d+$/.test(slug)) {
		return E2E_ROLES.map(([key, role]) => ({
			email: `demo+${key}@${slug}.localhost`,
			password: SEED_PASSWORD,
			role
		}));
	}
	return [];
}
