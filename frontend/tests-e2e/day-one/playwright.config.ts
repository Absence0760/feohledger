import { defineConfig, devices } from '@playwright/test';

import { API_BASE, PLATFORM_DOMAINS, WEB_ORIGIN, WEB_PORT, tenantOrigin } from '../fixtures/env';
import { readTenant } from './fixtures';

/**
 * The day-one suite (#575): one customer's first day on an install that a
 * deploy has just built from an EMPTY database. It runs against the stack
 * `scripts/day-one/stack.sh` brings up — deploy's own migration step,
 * add-tenant's own provisioning, the production image booted with
 * production-shaped settings — never against `seed.py` data, and never as part
 * of the 14-shard suite (`../playwright.config.ts` ignores this directory).
 *
 * Separate from the main config because that one's `globalSetup` checks the
 * SEEDED tenants' workflows, which on day one do not exist, and because this
 * suite is one ordered journey: one worker, no retries — a retry of a step
 * that already moved money would only be testing a different starting state.
 *
 * Run it: `scripts/day-one/stack.sh all`, then `pnpm test:day-one` from the
 * repo root. See `tests-e2e/README.md` § The day-one suite.
 */
const PORT_ARG = WEB_PORT ? ` --port ${WEB_PORT}` : '';
const HOST_ARG = ' --host 127.0.0.1';

export default defineConfig({
	testDir: '.',
	testMatch: '*.spec.ts',

	timeout: 30_000,
	expect: { timeout: 10_000 },
	retries: 0,
	workers: 1,
	fullyParallel: false,
	forbidOnly: !!process.env.CI,
	reporter: process.env.CI ? [['github'], ['list']] : 'list',

	webServer: {
		command:
			process.env.FEOH_E2E_USE_PREVIEW === 'true'
				? `pnpm exec vite preview${PORT_ARG}${HOST_ARG}`
				: `pnpm exec vite dev${PORT_ARG}${HOST_ARG}`,
		url: WEB_ORIGIN,
		reuseExistingServer: !process.env.CI,
		timeout: 60_000,
		stdout: 'ignore',
		stderr: 'pipe',
		env: { PUBLIC_API_URL: API_BASE, PUBLIC_PLATFORM_DOMAINS: PLATFORM_DOMAINS }
	},

	use: {
		baseURL: tenantOrigin(readTenant()?.slug ?? 'dayone'),
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'retain-on-failure',
		locale: 'en-US',
		timezoneId: 'UTC',
		// The width an operator's laptop has; the overflow checks run here.
		viewport: { width: 1280, height: 800 }
	},

	projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } } }]
});
