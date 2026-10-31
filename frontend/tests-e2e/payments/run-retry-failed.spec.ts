import type { Page, Route } from '@playwright/test';
import { expect, test } from '../fixtures/helpers';

/**
 * The run dialog's **Retry failed payments** control
 * (`POST /api/payments/runs/{id}/retry-failed`).
 *
 * The endpoint existed for a long time with no screen calling it — a help
 * guide even claimed the Runs tab's load Retry re-sent failed payments. The
 * control is offered from the run's `actions.retry_failed` verdict (built by
 * `payment_controls.run_retry_failed_refusal`, the gate the endpoint raises
 * from; the backend's `test_payment_action_verdicts.py` proves the two agree for
 * every role × run state), so this spec pins what the dialog does with each
 * shape of that verdict:
 *
 *  - `null` → offered; arms before it commits (it re-sends money), and the
 *    second click POSTs exactly once, then states what was sent and why the rest
 *    was skipped, and re-reads the run;
 *  - a refusal the reader can act on → shown disabled with the reason;
 *  - `payment_run_not_retryable` (the run did not finish with failures) and
 *    `role_not_permitted` → not rendered at all.
 *
 * Both reads are stubbed: the subject is the dialog's contract with the server,
 * and a real failed run would need a processor failure seeded into the shared
 * worker tenant to prove something this pins directly.
 */

const RUNS_LIST_PATH = '/api/payments/runs/';
const RUN_ID = 'f417ed00-0000-4000-8000-0000000000f1';

type Verdict = { code: string; message: string; params?: Record<string, unknown> } | null;

function runListRow(status: string) {
	return {
		id: RUN_ID,
		status,
		total_amount: '300.00',
		currency: 'USD',
		executed_at: '2026-09-01T10:05:00Z',
		created_at: '2026-09-01T10:00:00Z',
		payment_count: 2,
		payments_completed: 1,
		payments_failed: 1
	};
}

function runDetail(status: string, retryVerdict: Verdict, failedLeg = 'failed') {
	return {
		id: RUN_ID,
		status,
		total_amount: '300.00',
		currency: 'USD',
		initiated_by: null,
		executed_at: '2026-09-01T10:05:00Z',
		created_at: '2026-09-01T10:00:00Z',
		requires_cfo_approval: false,
		cfo_approved_by: null,
		cfo_approved_at: null,
		payment_count: 2,
		retryable_failures: retryVerdict === null ? 1 : 0,
		actions: {
			approve: { code: 'payment_run_not_draft', message: "Can only approve 'draft' runs" },
			execute: { code: 'payment_run_not_draft', message: "Can only execute 'draft' runs" },
			cancel: { code: 'payment_run_not_draft', message: "Can only cancel 'draft' runs" },
			retry_failed: retryVerdict
		},
		payments: [
			{
				id: `${RUN_ID}-leg-1`,
				invoice_id: `${RUN_ID}-inv-1`,
				invoice_number: 'RETRY-UI-1',
				vendor_name: 'Retry Fixture Ltd',
				amount: '100.00',
				currency: 'USD',
				method: 'ach',
				status: 'completed',
				reference: 'REF-1'
			},
			{
				id: `${RUN_ID}-leg-2`,
				invoice_id: `${RUN_ID}-inv-2`,
				invoice_number: 'RETRY-UI-2',
				vendor_name: 'Retry Fixture Ltd',
				amount: '200.00',
				currency: 'USD',
				method: 'ach',
				status: failedLeg,
				reference: null
			}
		]
	};
}

async function json(route: Route, body: unknown, status = 200) {
	await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

/** Stub the Runs list + the run detail (`detail()` is re-read on every GET, so
 *  a test can change what the reload returns), and open the dialog. */
async function openRun(page: Page, status: string, detail: () => unknown) {
	// The control is only offered where FeohLedger dispatches payments.
	await page.route(
		(url) => url.pathname === '/api/payments/execution-mode',
		(route) => json(route, { mode: 'processor', reason: 'configured' })
	);
	await page.route(
		(url) => url.pathname === RUNS_LIST_PATH,
		(route) => json(route, { items: [runListRow(status)], total: 1, page: 1, page_size: 100 })
	);
	await page.route(
		(url) => url.pathname === `${RUNS_LIST_PATH}${RUN_ID}`,
		(route) => json(route, detail())
	);

	await page.goto('/payments?tab=runs');
	await page.getByRole('button', { name: `View payment run ${RUN_ID.slice(0, 8)}` }).click();
	const dialog = page.getByRole('dialog', { name: 'Payment run' });
	await expect(dialog).toBeVisible();
	await expect(dialog.getByText('RETRY-UI-2')).toBeVisible();
	return dialog;
}

test.describe('RunDetailModal — Retry failed payments', () => {
	test('arms, posts once, states what was sent and skipped, and re-reads the run', async ({
		page
	}) => {
		let current = runDetail('partial', null);
		const dialog = await openRun(page, 'partial', () => current);

		const posts: string[] = [];
		await page.route(
			(url) => url.pathname === `${RUNS_LIST_PATH}${RUN_ID}/retry-failed`,
			async (route) => {
				posts.push(route.request().method());
				// After the retry the reload reports the run settled — retry no
				// longer applies, so the control must go away.
				current = runDetail('completed', {
					code: 'payment_run_not_retryable',
					message: "Can only retry a run that finished with failures (partial or failed), not 'completed'"
				}, 'completed');
				await json(route, {
					id: RUN_ID,
					status: 'completed',
					provider: 'mock',
					message: 'Payment run executed via mock: 1 completed.',
					payments_completed: 1,
					payments_in_flight: 0,
					payments_failed: 0,
					cards_issued: 0,
					payments_retried: 1,
					payments_skipped: 1,
					skip_reasons: ['needs_reconciliation']
				});
			}
		);

		const retry = dialog.getByRole('button', { name: 'Retry failed payments' });
		await expect(retry).toBeEnabled();
		await expect(dialog.getByTestId('run-retry-refusal')).toHaveCount(0);

		// First click only arms: nothing is sent yet.
		await retry.click();
		await expect(dialog.getByTestId('retry-armed-note')).toBeVisible();
		expect(posts).toEqual([]);

		await dialog.getByRole('button', { name: 'Confirm retry' }).click();

		const result = dialog.getByTestId('run-retry-result');
		await expect(result).toContainText('1 payment sent again, 1 skipped.');
		await expect(result).toContainText('it might have reached the payment processor');
		expect(posts).toEqual(['POST']);

		// The reload's verdict says the run no longer has failures to retry.
		await expect(dialog.getByRole('button', { name: /retry/i })).toHaveCount(0);
	});

	test('shows the control disabled, with the reason, for a refusal the reader can act on', async ({
		page
	}) => {
		const dialog = await openRun(page, 'failed', () =>
			runDetail('failed', {
				code: 'payment_run_nothing_to_retry',
				message: 'server English'
			})
		);
		const retry = dialog.getByRole('button', { name: 'Retry failed payments' });
		await expect(retry).toBeDisabled();
		// Localized from the code, not the server's English.
		const reason = dialog.getByTestId('run-retry-refusal');
		await expect(reason).toContainText("None of this run's failed payments can be sent again safely");
		await expect(retry).toHaveAttribute('aria-describedby', 'run-retry-refusal');
	});

	for (const code of ['payment_run_not_retryable', 'role_not_permitted']) {
		test(`renders no control for ${code}`, async ({ page }) => {
			const dialog = await openRun(page, 'completed', () =>
				runDetail('completed', { code, message: 'server English' }, 'completed')
			);
			await expect(dialog.getByRole('button', { name: 'Close' }).last()).toBeVisible();
			await expect(dialog.getByRole('button', { name: 'Retry failed payments' })).toHaveCount(0);
		});
	}
});
