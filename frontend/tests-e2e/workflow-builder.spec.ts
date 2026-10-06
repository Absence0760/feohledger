import {
	API_BASE,
	authedTenantHeaders,
	deleteWorkflowsWhere,
	expect,
	test
} from './fixtures/helpers';

/**
 * No-Code Workflow Builder — management UI e2e.
 *
 * Exercises the list-page management surface Worker D owns:
 *   - create a workflow from a template, open it, see steps render
 *   - open version history after an edit and see ≥1 version
 *   - run a simulation and assert a path renders
 *   - export then re-import a definition
 *
 * These tests *mutate* tenant state, so each one scopes itself to a
 * timestamp-suffixed workflow and cleans up via the API in a finally
 * block. The seeded "Default Workflow" is never deleted.
 *
 * Cross-worker behaviour mirrors the existing workflows specs: a worker
 * runs against its own `e2eN` tenant (see fixtures/helpers.ts), so the
 * throwaway workflows created here can't collide across workers.
 *
 * NOTE: this spec is written against the shared build contract
 * (reviews/workflow-builder-spec.md). Some flows depend on Worker A/B/C
 * pieces that merge at consolidation; the orchestrator runs e2e then.
 */

import type { Page } from '@playwright/test';

/**
 * Every workflow definition this spec creates is named `${MARKER}…`, so the
 * `afterEach` below can sweep by name in SQL rather than through the API.
 *
 * `deleteWorkflowByName` already sweeps by name, but only from inside a
 * `finally` the test has to reach: every create here happens BEFORE the `try`
 * (template pick, `+ New Workflow`, cXML import), so a create whose nav or
 * canvas render fails leaks the row it just made — and an interrupted run
 * leaks whatever is in flight. This tenant is still carrying a `Sim WF <ts>`
 * from exactly that. The SQL sweep also reaches a row the API refuses (the
 * DELETE 409s on an active definition or one with instances), which the
 * name-based API delete silently cannot.
 *
 * `deleteWorkflowsWhere` owns the walk below the definition — its three
 * non-cascading children — and the `is_default = false` seatbelt that keeps a
 * marker typo away from the seeded default.
 */
const MARKER = 'WF Builder E2E ';

async function deleteWorkflowByName(page: Page, name: string) {
	const resp = await page.request.get(`${API_BASE}/api/workflows?page_size=100`, {
		headers: await authedTenantHeaders(page),
	});
	const body = (await resp.json()) as { items: Array<{ id: string; name: string }> };
	for (const wf of body.items.filter((w) => w.name === name)) {
		await page.request.delete(`${API_BASE}/api/workflows/${wf.id}`, {
			headers: await authedTenantHeaders(page),
		});
	}
}

test.describe('no-code workflow builder management', () => {
	test.afterEach(() => deleteWorkflowsWhere(MARKER));

	test('toolbar exposes template + import entry points', async ({ page }) => {
		await page.goto('/workflows');
		await expect(page.getByRole('button', { name: 'New from template' })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Import' })).toBeVisible();
	});

	test('create a workflow from a template, open it, and see steps render', async ({
		page,
	}) => {
		const name = `${MARKER}Template WF ${Date.now()}`;
		await page.goto('/workflows');

		await page.getByRole('button', { name: 'New from template' }).click();
		const dialog = page.getByRole('dialog', { name: 'Template library' });
		await expect(dialog).toBeVisible();

		// Pick the first template card; name it; create.
		const firstCard = dialog.locator('.tl-card').first();
		await expect(firstCard).toBeVisible({ timeout: 10_000 });
		await firstCard.getByRole('button', { name: 'Use template' }).click();
		await firstCard.locator('input').fill(name);
		await firstCard.getByRole('button', { name: 'Create' }).click();

		// createFromTemplate navigates to the new workflow editor.
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/, { timeout: 10_000 });

		try {
			// The editor (Worker C) renders the template's steps.
			await expect(page.locator('.canvas .node').first()).toBeVisible({
				timeout: 10_000,
			});
			expect(await page.locator('.canvas .node').count()).toBeGreaterThan(0);
		} finally {
			await deleteWorkflowByName(page, name);
		}
	});

	test('steps reorder via the per-node Move buttons (keyboard-operable, WCAG 2.5.7)', async ({
		page,
	}) => {
		// The canvas drag-to-reorder is pointer-only; the per-node ↑/↓ buttons are
		// its keyboard + single-pointer alternative. Verify a keyboard user can
		// reorder: Tab to the button, activate with Enter, see the order flip.
		const name = `${MARKER}Reorder WF ${Date.now()}`;
		await page.goto('/workflows');
		await page.getByRole('button', { name: '+ New Workflow' }).click();
		await page.locator('#wf-name').fill(name);
		await page.getByRole('button', { name: /^Create$/ }).click();
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/);
		const id = page.url().match(/\/workflows\/([a-f0-9-]{36})/)![1];

		try {
			// Seed two named steps via the API, then reload the editor.
			const patch = await page.request.patch(`${API_BASE}/api/workflows/${id}`, {
				headers: await authedTenantHeaders(page),
				data: {
					steps: [
						{ number: 1, type: 'approval', name: 'Step Alpha', enabled: true, config: {} },
						{ number: 2, type: 'approval', name: 'Step Beta', enabled: true, config: {} },
					],
				},
			});
			expect(patch.ok()).toBeTruthy();
			await page.reload();

			const nodes = page.locator('.canvas .node');
			await expect(nodes).toHaveCount(2, { timeout: 10_000 });
			await expect(nodes.nth(0)).toContainText('Step Alpha');

			// Reachable by accessible name + operable from the keyboard.
			const moveDown = nodes.nth(0).getByRole('button', { name: 'Move Step Alpha down' });
			await moveDown.focus();
			await expect(moveDown).toBeFocused();
			await page.keyboard.press('Enter');

			// Order flipped (client-side; no save needed to prove the control works).
			await expect(nodes.nth(0)).toContainText('Step Beta');
			await expect(nodes.nth(1)).toContainText('Step Alpha');
		} finally {
			await deleteWorkflowByName(page, name);
		}
	});

	/**
	 * A throwaway workflow holding three named approval steps, opened in the
	 * editor. Returns its name for the `finally` cleanup.
	 */
	async function openWorkflowWithSteps(page: Page, label: string): Promise<string> {
		const name = `${MARKER}${label} ${Date.now()}`;
		await page.goto('/workflows');
		await page.getByRole('button', { name: '+ New Workflow' }).click();
		await page.locator('#wf-name').fill(name);
		await page.getByRole('button', { name: /^Create$/ }).click();
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/);
		const id = page.url().match(/\/workflows\/([a-f0-9-]{36})/)![1];
		const patch = await page.request.patch(`${API_BASE}/api/workflows/${id}`, {
			headers: await authedTenantHeaders(page),
			data: {
				steps: ['Alpha', 'Beta', 'Gamma'].map((n, i) => ({
					number: i + 1,
					type: 'approval',
					name: `Step ${n}`,
					enabled: true,
					config: {},
				})),
			},
		});
		expect(patch.ok()).toBeTruthy();
		await page.reload();
		await expect(page.locator('.canvas .node')).toHaveCount(3, { timeout: 10_000 });
		return name;
	}

	const stepNames = (page: Page) => page.locator('.canvas .node .node-name').allTextContents();

	/** Press on a step's handle and move to `toY` without releasing. */
	async function holdAndMove(page: Page, index: number, toY: (boxes: { y: number; height: number }[]) => number) {
		const nodes = page.locator('.canvas .node');
		const boxes = [];
		for (let i = 0; i < 3; i++) boxes.push((await nodes.nth(i).boundingBox())!);
		const handle = (await nodes.nth(index).locator('.drag-handle').boundingBox())!;
		await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
		await page.mouse.down();
		await page.mouse.move(handle.x + handle.width / 2, toY(boxes), { steps: 15 });
	}

	test('dragging a step reorders the canvas live, before the release', async ({ page }) => {
		const name = await openWorkflowWithSteps(page, 'Drag WF');
		try {
			// Carry Alpha down past Gamma's middle.
			await holdAndMove(page, 0, (b) => b[2].y + b[2].height * 0.8);

			// Still held: the other steps have already slid into their new places,
			// and Alpha's slot shows where it will land.
			await expect.poll(() => stepNames(page)).toEqual(['Step Beta', 'Step Gamma', 'Step Alpha']);
			await expect(page.locator('.canvas .item.placeholder')).toHaveCount(1);
			await expect(page.locator('.canvas .item-body.lifted')).toContainText('Step Alpha');

			await page.mouse.up();
			await expect(page.locator('.canvas .item.placeholder')).toHaveCount(0);
			expect(await stepNames(page)).toEqual(['Step Beta', 'Step Gamma', 'Step Alpha']);
			// Renumbered, and the editor knows there is something to save.
			await expect(page.locator('.canvas .node .node-number')).toHaveText(['1', '2', '3']);
			await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
		} finally {
			await page.mouse.up().catch(() => {});
			await deleteWorkflowByName(page, name);
		}
	});

	test('Escape while dragging puts the step back', async ({ page }) => {
		const name = await openWorkflowWithSteps(page, 'Drag Cancel WF');
		try {
			await holdAndMove(page, 2, (b) => b[0].y + 4);
			await expect.poll(() => stepNames(page)).toEqual(['Step Gamma', 'Step Alpha', 'Step Beta']);

			await page.keyboard.press('Escape');
			await page.mouse.up();
			expect(await stepNames(page)).toEqual(['Step Alpha', 'Step Beta', 'Step Gamma']);
			await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
		} finally {
			await deleteWorkflowByName(page, name);
		}
	});

	test('a click on a step still selects it — a drag starts only after real movement', async ({
		page,
	}) => {
		const name = await openWorkflowWithSteps(page, 'Click WF');
		try {
			await page.locator('.canvas .node').nth(1).click();
			await expect(page.locator('.canvas .node').nth(1)).toHaveClass(/selected/);
			expect(await stepNames(page)).toEqual(['Step Alpha', 'Step Beta', 'Step Gamma']);
		} finally {
			await deleteWorkflowByName(page, name);
		}
	});

	test('the step library adds by click and is not draggable', async ({ page }) => {
		const name = await openWorkflowWithSteps(page, 'Palette WF');
		try {
			const item = page.locator('.palette-item[data-palette-type="delay"]');
			await expect(item).not.toHaveAttribute('draggable', 'true');
			await item.click();
			await expect(page.locator('.canvas .node')).toHaveCount(4);
			await expect(page.locator('.canvas .node').nth(3)).toHaveClass(/selected/);
		} finally {
			await deleteWorkflowByName(page, name);
		}
	});

	test('version history shows ≥1 version after an edit', async ({ page }) => {
		const name = `${MARKER}Versioned WF ${Date.now()}`;
		// Create via the standard create modal (no template dependency).
		await page.goto('/workflows');
		await page.getByRole('button', { name: '+ New Workflow' }).click();
		await page.locator('#wf-name').fill(name);
		await page.getByRole('button', { name: /^Create$/ }).click();
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/);
		const id = page.url().match(/\/workflows\/([a-f0-9-]{36})/)![1];

		try {
			// Edit steps via the API → backend auto-snapshots the prior
			// steps_config into a WorkflowVersion (contract: auto-versioning).
			const patch = await page.request.patch(`${API_BASE}/api/workflows/${id}`, {
				headers: await authedTenantHeaders(page),
				data: {
					steps: [
						{
							number: 1,
							type: 'approval',
							name: 'Approval',
							enabled: true,
							config: {},
						},
					],
				},
			});
			expect(patch.ok()).toBeTruthy();

			// Open the version history modal from the list row.
			await page.goto('/workflows');
			const row = page.locator('table tbody tr', { hasText: name });
			await expect(row).toBeVisible();
			await row.getByRole('button', { name: 'Versions' }).click();

			const dialog = page.getByRole('dialog', { name: 'Version history' });
			await expect(dialog).toBeVisible();
			await expect(dialog.locator('.vh-row').first()).toBeVisible({ timeout: 10_000 });
			expect(await dialog.locator('.vh-row').count()).toBeGreaterThanOrEqual(1);
		} finally {
			await page.request.delete(`${API_BASE}/api/workflows/${id}`, {
				headers: await authedTenantHeaders(page),
			});
		}
	});

	test('simulation renders a step path for a sample invoice', async ({ page }) => {
		const name = `${MARKER}Sim WF ${Date.now()}`;
		await page.goto('/workflows');
		await page.getByRole('button', { name: '+ New Workflow' }).click();
		await page.locator('#wf-name').fill(name);
		await page.getByRole('button', { name: /^Create$/ }).click();
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/);
		const id = page.url().match(/\/workflows\/([a-f0-9-]{36})/)![1];

		try {
			await page.goto('/workflows');
			const row = page.locator('table tbody tr', { hasText: name });
			await row.getByRole('button', { name: 'Simulate' }).click();

			const dialog = page.getByRole('dialog', { name: 'Simulate workflow' });
			await expect(dialog).toBeVisible();

			await dialog.getByLabel('Amount', { exact: true }).fill('5000.00');
			await dialog.getByLabel('Currency', { exact: true }).fill('USD');
			await dialog.getByRole('button', { name: 'Run simulation' }).click();

			// The returned path renders step-by-step with a terminal state.
			const result = dialog.locator('.sim-result');
			await expect(result).toBeVisible({ timeout: 10_000 });
			await expect(result.locator('.sim-step').first()).toBeVisible();
			await expect(result.locator('.sim-terminal-val')).toBeVisible();
		} finally {
			await page.request.delete(`${API_BASE}/api/workflows/${id}`, {
				headers: await authedTenantHeaders(page),
			});
		}
	});

	test('export then import a definition round-trips into a new workflow', async ({
		page,
	}) => {
		const srcName = `${MARKER}Export Src ${Date.now()}`;
		const importName = `${MARKER}Imported ${Date.now()}`;

		// Create a source workflow whose definition we export.
		await page.goto('/workflows');
		await page.getByRole('button', { name: '+ New Workflow' }).click();
		await page.locator('#wf-name').fill(srcName);
		await page.getByRole('button', { name: /^Create$/ }).click();
		await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/);
		const srcId = page.url().match(/\/workflows\/([a-f0-9-]{36})/)![1];

		try {
			// Export: fetch the definition the way the Export button does
			// (download is hard to capture deterministically; the store call
			// is the load-bearing part).
			const exportResp = await page.request.get(
				`${API_BASE}/api/workflows/${srcId}/export`,
				{ headers: await authedTenantHeaders(page) }
			);
			expect(exportResp.ok()).toBeTruthy();
			const definition = await exportResp.json();
			expect(definition.steps_config?.steps?.length ?? 0).toBeGreaterThan(0);

			// Import via the UI: open the modal, paste JSON, set a name, import.
			await page.goto('/workflows');
			await page.getByRole('button', { name: 'Import' }).click();
			const dialog = page.getByRole('dialog', { name: 'Import workflow' });
			await expect(dialog).toBeVisible();

			await dialog.locator('#ie-name').fill(importName);
			await dialog.locator('#ie-json').fill(JSON.stringify(definition));
			await dialog.getByRole('button', { name: 'Import' }).click();

			// Import navigates to the new workflow editor.
			await page.waitForURL(/\/workflows\/[a-f0-9-]{36}/, { timeout: 10_000 });
			await expect(page.locator('.canvas .node').first()).toBeVisible({
				timeout: 10_000,
			});
		} finally {
			await deleteWorkflowByName(page, srcName);
			await deleteWorkflowByName(page, importName);
		}
	});

	test('import surfaces validation errors for malformed JSON', async ({ page }) => {
		await page.goto('/workflows');
		await page.getByRole('button', { name: 'Import' }).click();
		const dialog = page.getByRole('dialog', { name: 'Import workflow' });
		await expect(dialog).toBeVisible();

		await dialog.locator('#ie-json').fill('{ not valid json');
		await dialog.getByRole('button', { name: 'Import' }).click();

		await expect(dialog.locator('.ie-errors')).toBeVisible();
		await expect(dialog.locator('.ie-errors')).toContainText(/JSON/i);
	});
});
