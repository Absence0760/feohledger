/**
 * Optional local-service gating for e2e specs.
 *
 * Several flows (SSO, email, real-adapter ERP) need a Docker container that
 * isn't part of the default `pnpm db:up` stack: Keycloak, Mailpit, LocalStack,
 * stripe-mock, fake-erp. Those specs call `skipUnlessReachable(...)` in a
 * `beforeEach`, so they:
 *
 *   - run the REAL end-to-end flow when the service is up (locally after
 *     `pnpm <svc>:up`, or in CI where the workflow starts them), and
 *   - skip with a clear, actionable message when it isn't — never a silent
 *     pass and never a failure that's really just "you forgot to start it".
 *
 * This is environment gating on an optional dependency, NOT masking a bug: when
 * the service IS present the assertions are strict.
 */

import { test } from './helpers';

/** Probe URLs — any HTTP response (even 4xx) means the service is up. */
export const SERVICES = {
	keycloak: 'http://localhost:8088/realms/feohledger/.well-known/openid-configuration',
	keycloakSaml: 'http://localhost:8088/realms/feohledger/protocol/saml/descriptor',
	mailpit: 'http://localhost:8025/api/v1/info',
	localstack: 'http://localhost:4566/_localstack/health',
	stripeMock: 'http://localhost:12111/v1',
	fakeErp: 'http://localhost:12112/health',
	fakeCommerce: 'http://localhost:12113/health'
} as const;

/** Hint shown when a service is down, keyed by probe URL. */
const HINTS: Record<string, string> = {
	[SERVICES.keycloak]: 'Keycloak not running — `pnpm idp:up && pnpm idp:seed`',
	[SERVICES.keycloakSaml]: 'Keycloak SAML not seeded — `pnpm idp:up && pnpm saml:seed`',
	[SERVICES.mailpit]: 'Mailpit not running — `pnpm mail:up` (+ backend FEOH_EMAIL_PROVIDER=smtp)',
	[SERVICES.localstack]: 'LocalStack not running — `pnpm aws:up`',
	[SERVICES.stripeMock]: 'stripe-mock not running — `pnpm stripe:up`',
	[SERVICES.fakeErp]:
		'fake-erp not running — `pnpm erp:up` (backend must have started with the .env.development FEOH_ERP_*_API_BASE overrides)',
	[SERVICES.fakeCommerce]:
		'fake-commerce not running — `pnpm commerce:up` (backend must have started with the .env.development FEOH_COMMERCE_API_BASE_OVERRIDE)'
};

const _cache = new Map<string, boolean>();

/** True if a network request to `url` gets any response within the timeout. */
export async function isReachable(url: string, timeoutMs = 2500): Promise<boolean> {
	if (_cache.has(url)) return _cache.get(url)!;
	let up = false;
	try {
		await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
		up = true;
	} catch {
		up = false;
	}
	_cache.set(url, up);
	return up;
}

/**
 * Skip the current test (call inside `beforeEach`) unless `url` is reachable.
 * The message names the `pnpm` command that starts the missing service.
 *
 * In CI's service-e2e job the container is started on purpose and
 * `FEOH_REQUIRE_INTEGRATION` is set — there an unreachable service is a hard
 * failure, never a silent skip that leaves the job green with this flow's
 * coverage quietly dropped. Locally (var unset) it still skips with an
 * actionable hint when the optional service isn't running.
 */
export async function skipUnlessReachable(url: string): Promise<void> {
	const up = await isReachable(url);
	if (up) return;
	const hint = HINTS[url] ?? `Required local service not reachable: ${url}`;
	if (process.env.FEOH_REQUIRE_INTEGRATION) {
		throw new Error(`${hint} — FEOH_REQUIRE_INTEGRATION is set, refusing to skip`);
	}
	test.skip(true, hint);
}
