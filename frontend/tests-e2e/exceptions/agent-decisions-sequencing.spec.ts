import { expect, test } from '../fixtures/helpers';
import { exceptionSummary } from './summary';

/**
 * `/exceptions` → AI Agents tab — the decision log's own request sequencing.
 *
 * `loadDecisions` is a second, independent list on the exceptions surface (its
 * action chips call it directly, with no debounce) and it had no
 * `createRequestSequencer`. Two quick chip clicks let the FIRST response land
 * last, filling the table with decisions the active chip excludes and setting
 * `total`/`page` from them — so the Load-more that follows asks for the wrong
 * page of the wrong filter.
 *
 * The KPI stats are a one-shot mount read of different state and stay
 * unsequenced by design; only the log is asserted here.
 *
 * Everything is stubbed so the interleaving is exact.
 */

const STATS = {
	total_decisions: 2,
	auto_resolved: 1,
	escalated: 1,
	no_action: 0,
	resolution_rate: 0.5,
	escalation_rate: 0.5,
	accuracy: null
};

function decision(id: string, action: string, agentType: string) {
	return {
		id,
		exception_id: `${id}-exc`,
		invoice_id: `${id}-inv`,
		exception_type: 'duplicate',
		agent_type: agentType,
		action_taken: action,
		confidence: 0.9,
		autonomy_level: 'suggest',
		rationale: 'stubbed',
		changes: null,
		created_at: '2026-01-01T00:00:00Z'
	};
}

test.describe('/exceptions AI Agents — decision-log sequencing', () => {
	test('a held action filter cannot repaint the log under a newer chip', async ({ page }) => {
		await page.route('**/api/exceptions/summary*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(exceptionSummary())
			})
		);
		await page.route('**/api/exceptions/agent-stats*', (route) =>
			route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify(STATS)
			})
		);

		let releaseEscalated: () => void = () => {};
		const escalatedGate = new Promise<void>((resolve) => (releaseEscalated = resolve));

		await page.route('**/api/exceptions/agent-decisions*', async (route) => {
			const action = new URL(route.request().url()).searchParams.get('action_taken');
			if (action === 'escalated') {
				// The earlier request: held until the newer one has landed.
				await escalatedGate;
				await route.fulfill({
					status: 200,
					contentType: 'application/json',
					body: JSON.stringify({
						items: [decision('d-stale', 'escalated', 'STALE-AGENT')],
						total: 1,
						page: 1,
						page_size: 20
					})
				});
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({
					items:
						action === 'auto_resolved'
							? [decision('d-live', 'auto_resolved', 'LIVE-AGENT')]
							: [decision('d-mount', 'no_action', 'MOUNT-AGENT')],
					total: 1,
					page: 1,
					page_size: 20
				})
			});
		});

		await page.route('**/api/exceptions?*', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname !== '/api/exceptions') {
				await route.fallback();
				return;
			}
			await route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ items: [], total: 0 })
			});
		});

		await page.goto('/exceptions?view=agents');
		const dash = page.getByTestId('agent-dashboard');
		await expect(dash).toBeVisible();
		await expect(dash.getByText('MOUNT-AGENT')).toBeVisible();

		// Escalated (slow, held) then Auto-resolved (fast).
		await dash.locator('.filter-chip', { hasText: 'Escalated' }).click();
		await dash.locator('.filter-chip', { hasText: 'Auto-resolved' }).click();
		await expect(dash.getByText('LIVE-AGENT')).toBeVisible();

		// Release the stale response and wait for the page to actually receive it —
		// a real signal, not a sleep.
		const staleResponse = page.waitForResponse(
			(r) =>
				new URL(r.url()).pathname === '/api/exceptions/agent-decisions' &&
				new URL(r.url()).searchParams.get('action_taken') === 'escalated'
		);
		releaseEscalated();
		await staleResponse;
		// One animation frame past the response guarantees the fetch continuation
		// (and any state write it would have made) has run.
		await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));

		await expect(dash.getByText('STALE-AGENT')).toHaveCount(0);
		await expect(dash.getByText('LIVE-AGENT')).toBeVisible();
	});
});
