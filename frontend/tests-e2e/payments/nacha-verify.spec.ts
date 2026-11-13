import { expect, test } from '../fixtures/helpers';
import {
	routeVerify,
	runDialog,
	switchPaymentsMode,
	verifyAnswer,
	withExportedRun
} from '../fixtures/nachaRun';

/**
 * "Verify before upload" in the run dialog (decisions §362): after exporting a
 * run's bank file, the person about to upload it re-reads the ERP bill of every
 * payment still in it. The dialog states each of the four outcomes:
 *
 *  - verified — every bill is still open (or the ERP reports none back): upload;
 *  - superseded — a bill was paid in the ERP since the export and its payment
 *    was taken out of the run: do NOT upload this file; regenerate it, or, with
 *    nothing left, the run was cancelled;
 *  - held — a bill could not be read just now: verify again first;
 *  - error — the check itself failed, stated in the panel.
 *
 * The verified answer is the real server's on a mock ERP. The other three are
 * routed: no local ERP pays a bill or goes unreachable on cue, and the server's
 * side of them (the payment taken out, the blocking exception, the audit row)
 * is pinned in the backend's `test_nacha.py`.
 *
 * A refused control states its reason, like Download and Regenerate. No
 * refusal the server raises today renders Verify disabled — each one hides it
 * instead (`types/runOutside.ts`) — so the run read is routed to carry one this
 * build does not know, which is exactly the case the reason line exists for.
 *
 * A run exported before the tenant switched to processor mode keeps the panel
 * (decisions §382): the server says it applies (`bank_file_applies`), Verify
 * stays allowed (§362 gives it no record-only gate), and Regenerate and "Bank
 * rejected the file" render disabled beside the server's reasons, so the
 * reader holding the file can check it and learns why the run cannot be
 * released.
 */

test.describe('run dialog — verify the bank file before upload', () => {
	test('states each outcome: error, verified, held, superseded, cancelled', async ({ page }) => {
		await withExportedRun(page, async (run) => {
			const { modal } = run;
			const verify = modal.getByTestId('nacha-verify');
			const result = modal.getByTestId('nacha-verify-result');
			await expect(modal.getByText('Before you upload the file, check that every bill')).toBeVisible();
			// Allowed controls carry no reason line.
			await expect(verify).not.toHaveAttribute('aria-describedby');
			await expect(modal.getByTestId('run-nacha-verify-refusal')).toHaveCount(0);
			await expect(modal.getByTestId('nacha-reject')).toBeEnabled();
			await expect(modal.getByTestId('nacha-reject')).not.toHaveAttribute('aria-describedby');
			await expect(modal.getByTestId('run-nacha-void-refusal')).toHaveCount(0);

			await test.step('error: the check failed, said once in the panel', async () => {
				await routeVerify(page, run.runId, {
					status: 503,
					json: { detail: 'The ERP could not be reached' }
				});
				await verify.click();
				const error = modal.getByTestId('nacha-verify-error');
				await expect(error).toBeVisible();
				await expect(error).toHaveAttribute('role', 'alert');
				await expect(error).not.toBeEmpty();
				await expect(result).toHaveCount(0);
			});

			await test.step('verified: the real check, on a mock ERP', async () => {
				await page.unroute(`**/api/payments/runs/${run.runId}/nacha/verify`);
				const verified = page.waitForResponse(
					(r) =>
						new URL(r.url()).pathname === `/api/payments/runs/${run.runId}/nacha/verify` &&
						r.request().method() === 'POST'
				);
				await verify.click();
				expect((await verified).status()).toBe(200);
				await expect(result).toHaveAttribute('data-result', 'verified');
				await expect(result.getByRole('status')).toHaveText(
					/^(Every bill in this file is still open in your ERP\. You can upload it\.|Your ERP doesn't report bill payments back to FeohLedger, so there were no bills to re-check\.)$/
				);
				await expect(modal.getByTestId('nacha-verify-error')).toHaveCount(0);
				// The run is untouched: still exported, still verifiable.
				await expect(modal.locator('.status-badge')).toHaveText('Exported');
				await expect(verify).toBeEnabled();
			});

			await test.step('held: a bill could not be read — verify again', async () => {
				await routeVerify(page, run.runId, { status: 200, json: verifyAnswer(run, 'held') });
				await verify.click();
				await expect(result).toHaveAttribute('data-result', 'held');
				await expect(result.getByRole('status')).toHaveText(
					"These bills couldn't be checked just now. Verify again before you upload the file:"
				);
				await expect(result.locator('.verify-list li')).toHaveText([run.invoiceNumber]);
				await expect(result.getByRole('alert')).toHaveCount(0);
			});

			await test.step("superseded: don't upload this file, regenerate it", async () => {
				await routeVerify(page, run.runId, {
					status: 200,
					json: verifyAnswer(run, 'superseded', { remaining: 1 })
				});
				await verify.click();
				await expect(result).toHaveAttribute('data-result', 'superseded');
				await expect(result.getByRole('alert')).toHaveText(
					"Don't upload the file you downloaded. These payments were taken out of the run because their bills are no longer open in your ERP:"
				);
				await expect(result.locator('.verify-list li')).toHaveText([run.invoiceNumber]);
				await expect(result).toContainText('Regenerate the file and upload the new one instead.');
				// The held answer it replaced is gone.
				await expect(result.getByRole('status')).toHaveCount(0);
			});

			await test.step('superseded with nothing left: the run was cancelled', async () => {
				await routeVerify(page, run.runId, {
					status: 200,
					json: verifyAnswer(run, 'superseded', { remaining: 0 })
				});
				await verify.click();
				await expect(result.getByRole('alert')).toBeVisible();
				await expect(result).toContainText(
					'No payments are left in the file, so the run was cancelled.'
				);
				await expect(result).not.toContainText('Regenerate the file');
			});
		});
	});

	test('a refused Verify or "Bank rejected the file" says why beside it', async ({ page }) => {
		await withExportedRun(page, async (run) => {
			await page.route(`**/api/payments/runs/${run.runId}`, async (route) => {
				const response = await route.fetch();
				const body = (await response.json()) as { actions: Record<string, unknown> };
				body.actions.nacha_verify = {
					code: 'e2e_unknown_verify_refusal',
					message: 'E2E: verify is refused for a reason this build does not know.'
				};
				body.actions.nacha_void = {
					code: 'e2e_unknown_void_refusal',
					message: 'E2E: the release is refused for a reason this build does not know.'
				};
				await route.fulfill({ response, json: body });
			});
			// Reopen the dialog so it reads the run again.
			await page.keyboard.press('Escape');
			await expect(runDialog(page)).toHaveCount(0);
			await page.getByRole('button', { name: `View payment run ${run.runId.slice(0, 8)}` }).click();
			const modal = runDialog(page);
			await expect(modal.locator('.status-badge')).toHaveText('Exported');

			const verify = modal.getByTestId('nacha-verify');
			await expect(verify).toBeDisabled();
			await expect(verify).toHaveAttribute('aria-describedby', 'run-nacha-verify-refusal');
			await expect(modal.getByTestId('run-nacha-verify-refusal')).toHaveText(
				'E2E: verify is refused for a reason this build does not know.'
			);

			const reject = modal.getByTestId('nacha-reject');
			await expect(reject).toBeDisabled();
			await expect(reject).toHaveAttribute('aria-describedby', 'run-nacha-void-refusal');
			await expect(modal.getByTestId('run-nacha-void-refusal')).toHaveText(
				'E2E: the release is refused for a reason this build does not know.'
			);
		});
	});

	test('a run exported before a switch to processor mode keeps Verify and says why it cannot be released', async ({
		page
	}) => {
		await withExportedRun(page, async (run) => {
			await switchPaymentsMode(page, 'processor');
			// Reopen the dialog so it reads the run, and the mode, again.
			await page.keyboard.press('Escape');
			await expect(runDialog(page)).toHaveCount(0);
			await page.reload();
			const opened = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/payments/runs/${run.runId}` &&
					r.request().method() === 'GET'
			);
			await page.getByRole('button', { name: `View payment run ${run.runId.slice(0, 8)}` }).click();
			const body = (await (await opened).json()) as {
				bank_file_applies: boolean;
				actions: Record<string, { code: string } | null>;
			};
			// The server's verdict, not a restated mode check.
			expect(body.bank_file_applies).toBe(true);
			expect(body.actions.nacha_verify).toBeNull();
			expect(body.actions.nacha_void?.code).toBe('nacha_void_requires_record_only');
			expect(body.actions.nacha_regenerate?.code).toBe('nacha_requires_record_only');

			const modal = runDialog(page);
			await expect(modal.locator('.status-badge')).toHaveText('Exported');
			await expect(modal.getByTestId('run-nacha-panel')).toBeVisible();
			// No first file to download: it is already out.
			await expect(modal.getByRole('button', { name: 'Download bank file (NACHA)' })).toHaveCount(0);

			const verify = modal.getByTestId('nacha-verify');
			await expect(verify).toBeEnabled();
			await expect(verify).not.toHaveAttribute('aria-describedby');
			await expect(modal.getByTestId('run-nacha-verify-refusal')).toHaveCount(0);

			const reject = modal.getByTestId('nacha-reject');
			await expect(reject).toBeDisabled();
			await expect(reject).toHaveAttribute('aria-describedby', 'run-nacha-void-refusal');
			await expect(modal.getByTestId('run-nacha-void-refusal')).toHaveText(
				"This run's bank file was exported while FeohLedger recorded your payments, and payments are now sent through a processor. Releasing the run could pay these suppliers twice. If the bank took the file, record the run as paid. If it didn't, an admin must switch payments back to record-only first."
			);

			const regenerate = modal.getByRole('button', { name: 'Regenerate file' });
			await expect(regenerate).toBeDisabled();
			await expect(regenerate).toHaveAttribute('aria-describedby', 'run-nacha-regenerate-refusal');
			await expect(modal.getByTestId('run-nacha-regenerate-refusal')).toHaveText(
				'A bank file is only available when FeohLedger records your payments rather than sending them. An admin can change this under Settings → Payments.'
			);

			// Verify is the real check, and the run keeps its claim.
			const verified = page.waitForResponse(
				(r) =>
					new URL(r.url()).pathname === `/api/payments/runs/${run.runId}/nacha/verify` &&
					r.request().method() === 'POST'
			);
			await verify.click();
			expect((await verified).status()).toBe(200);
			await expect(modal.getByTestId('nacha-verify-result')).toHaveAttribute(
				'data-result',
				'verified'
			);
			await expect(modal.locator('.status-badge')).toHaveText('Exported');
		});
	});
});
