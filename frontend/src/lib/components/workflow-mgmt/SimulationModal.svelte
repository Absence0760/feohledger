<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { workflowStore } from '#lib/stores/workflows.svelte.ts';
	import type { SimInvoice, SimulationResult } from '#lib/types/workflow.ts';

	let {
		open,
		workflowId,
		workflowName,
		onclose,
	}: {
		open: boolean;
		workflowId: string;
		workflowName: string;
		onclose: () => void;
	} = $props();

	// Sample invoice the user shapes to probe the workflow's routing. Amount
	// stays a string so we never lose decimal precision in the input — the
	// backend accepts a string-decimal for money (rail: money is exact).
	let amount = $state('1000.00');
	let currency = $state('USD');
	let vendorId = $state('');
	let glAccount = $state('');
	let costCenter = $state('');
	let department = $state('');

	let result = $state<SimulationResult | null>(null);
	let running = $state(false);

	// Reset the previous result whenever a different workflow's modal opens.
	$effect(() => {
		if (open && workflowId) {
			result = null;
		}
	});

	async function run() {
		running = true;
		try {
			const invoice: SimInvoice = {
				amount: amount.trim(),
				currency: currency.trim() || 'USD',
				vendor_id: vendorId.trim() || null,
				gl_account: glAccount.trim() || null,
				cost_center: costCenter.trim() || null,
				department: department.trim() || null,
			};
			result = await workflowStore.simulate(workflowId, { invoice });
		} catch (e) {
			toast(e instanceof Error ? e.message : m('workflows.mgmt.sim.failed'), 'error');
		} finally {
			running = false;
		}
	}
</script>

<Modal {open} ariaLabel={m('workflows.mgmt.sim.aria')} title={m('workflows.mgmt.sim.title', { name: workflowName })} width="lg" {onclose}>
	<div class="sim-body">
		<form
			class="sim-form"
			onsubmit={(e) => {
				e.preventDefault();
				run();
			}}
		>
			<div class="sim-grid">
				<label>
					{m('workflows.mgmt.sim.amount')}
					<input type="text" inputmode="decimal" bind:value={amount} aria-label={m('workflows.mgmt.sim.amount')} />
				</label>
				<label>
					{m('workflows.mgmt.sim.currency')}
					<input type="text" bind:value={currency} aria-label={m('workflows.mgmt.sim.currency')} maxlength="3" />
				</label>
				<label>
					{m('workflows.mgmt.sim.vendorId')}
					<input type="text" bind:value={vendorId} aria-label={m('workflows.mgmt.sim.vendorId')} placeholder={m('workflows.mgmt.sim.optional')} />
				</label>
				<label>
					{m('workflows.mgmt.sim.glAccount')}
					<input type="text" bind:value={glAccount} aria-label={m('workflows.mgmt.sim.glAccount')} placeholder={m('workflows.mgmt.sim.optional')} />
				</label>
				<label>
					{m('workflows.mgmt.sim.costCenter')}
					<input type="text" bind:value={costCenter} aria-label={m('workflows.mgmt.sim.costCenter')} placeholder={m('workflows.mgmt.sim.optional')} />
				</label>
				<label>
					{m('workflows.mgmt.sim.department')}
					<input type="text" bind:value={department} aria-label={m('workflows.mgmt.sim.department')} placeholder={m('workflows.mgmt.sim.optional')} />
				</label>
			</div>
			<div class="sim-actions">
				<button type="submit" class="sim-run" disabled={running}>
					{running ? m('workflows.mgmt.sim.running') : m('workflows.mgmt.sim.run')}
				</button>
			</div>
		</form>

		{#if result}
			<div class="sim-result" aria-label={m('workflows.mgmt.sim.resultAria')}>
				<div class="sim-terminal">
					{m('workflows.mgmt.sim.terminalState')} <span class="sim-terminal-val">{result.terminal_state}</span>
				</div>

				{#if result.warnings.length > 0}
					<ul class="sim-warnings">
						{#each result.warnings as w, i (i)}
							<li>⚠ {w}</li>
						{/each}
					</ul>
				{/if}

				<ol class="sim-path">
					{#each result.path as step (step.step_number)}
						<li class="sim-step">
							<span class="sim-step-num">{step.step_number}</span>
							<div class="sim-step-main">
								<div class="sim-step-head">
									<span class="sim-step-name">{step.name}</span>
									<span class="sim-step-type">{step.type}</span>
									<span class="sim-outcome outcome-{step.outcome}">{step.outcome}</span>
								</div>
								{#if step.detail}
									<div class="sim-detail">{step.detail}</div>
								{/if}
							</div>
						</li>
					{/each}
				</ol>
			</div>
		{/if}
	</div>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('workflows.mgmt.close')}</button>
	</div>
</Modal>

<style>
	.sim-body {
		max-height: 62vh;
		overflow-y: auto;
	}

	.sim-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 10px 14px;
	}

	.sim-form label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.74rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.sim-form input {
		padding: 7px 9px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.86rem;
	}

	.sim-actions {
		margin: 14px 0 4px;
	}

	.sim-run {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.sim-run:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.sim-result {
		margin-top: 18px;
		border-top: 1px solid var(--border);
		padding-top: 14px;
	}

	.sim-terminal {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	.sim-terminal-val {
		font-weight: 700;
		color: var(--text);
		text-transform: capitalize;
	}

	.sim-warnings {
		list-style: none;
		margin: 0 0 12px;
		padding: 8px 12px;
		border-radius: 6px;
		background: rgba(255, 180, 50, 0.12);
		color: #d4940a;
		font-size: 0.82rem;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.sim-path {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.sim-step {
		display: flex;
		gap: 10px;
		align-items: flex-start;
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 10px 12px;
		background: var(--bg);
	}

	.sim-step-num {
		flex: 0 0 auto;
		width: 24px;
		height: 24px;
		border-radius: 50%;
		background: var(--accent-strong);
		color: #fff;
		display: grid;
		place-items: center;
		font-size: 0.78rem;
		font-weight: 600;
	}

	.sim-step-main {
		flex: 1;
		min-width: 0;
	}

	.sim-step-head {
		display: flex;
		gap: 10px;
		align-items: baseline;
		flex-wrap: wrap;
	}

	.sim-step-name {
		font-weight: 600;
		font-size: 0.88rem;
	}

	.sim-step-type {
		font-size: 0.72rem;
		color: var(--text-muted);
		text-transform: capitalize;
	}

	.sim-outcome {
		font-size: 0.68rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 7px;
		border-radius: 4px;
		margin-left: auto;
	}

	.outcome-ok,
	.outcome-passed,
	.outcome-matched {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.outcome-skipped,
	.outcome-not_matched {
		background: var(--muted-tint);
		color: var(--muted-on-tint);
	}

	.outcome-error,
	.outcome-failed {
		background: rgba(240, 70, 70, 0.15);
		color: var(--danger);
	}

	.sim-detail {
		margin-top: 4px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.btn-cancel {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}
</style>
