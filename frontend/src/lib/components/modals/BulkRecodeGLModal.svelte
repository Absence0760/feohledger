<script lang="ts">
	import { api } from '#lib/api.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	interface Props {
		onclose: () => void;
		onapplied?: () => void;
	}

	let { onclose, onapplied }: Props = $props();

	interface RecodeChange {
		invoice_id: string;
		invoice_number: string;
		vendor_name: string;
		old_gl: string | null;
		new_gl: string;
		source: 'vendor_prior' | 'ai';
	}

	interface RecodeReport {
		matched: number;
		would_change?: number;
		applied?: number;
		ai_candidates: number;
		by_source: { vendor_prior: number; ai: number };
		skipped: {
			immutable_status: number;
			no_vendor: number;
			no_change: number;
			no_prior_no_ai: number;
			ai_failed: number;
			invalid_code: number;
		};
		changes: RecodeChange[];
		dry_run: boolean;
	}

	let from_date = $state<string>('');
	let to_date = $state<string>('');
	let include_ai_fallback = $state(false);
	let busy = $state(false);
	let preview = $state<RecodeReport | null>(null);

	async function runDryRun() {
		busy = true;
		try {
			const body: Record<string, unknown> = {
				dry_run: true,
				include_ai_fallback,
				vendor_ids: []
			};
			if (from_date) body.from_date = from_date;
			if (to_date) body.to_date = to_date;
			preview = await api.post<RecodeReport>('/api/invoices/bulk-recode-gl', body);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.bulkRecode.previewFailed'), 'error');
		} finally {
			busy = false;
		}
	}

	function applyButtonLabel(p: RecodeReport): string {
		const priorChanges = p.changes.length;
		const aiCandidates = p.ai_candidates;
		if (aiCandidates > 0 && priorChanges > 0) {
			return m('invoices.bulkRecode.applyBoth', { changes: priorChanges, ai: aiCandidates });
		}
		if (aiCandidates > 0) {
			return m('invoices.bulkRecode.applyAi', { n: aiCandidates });
		}
		return m('invoices.bulkRecode.applyPriors', { n: priorChanges });
	}

	function sourceLabel(source: RecodeChange['source']): string {
		return source === 'ai'
			? m('invoices.bulkRecode.source.ai')
			: m('invoices.bulkRecode.source.vendorPrior');
	}

	async function applyChanges() {
		busy = true;
		try {
			const body: Record<string, unknown> = {
				dry_run: false,
				include_ai_fallback,
				vendor_ids: []
			};
			if (from_date) body.from_date = from_date;
			if (to_date) body.to_date = to_date;
			const report = await api.post<RecodeReport>('/api/invoices/bulk-recode-gl', body);
			toast(m('invoices.bulkRecode.applied', { n: report.applied ?? 0 }), 'success');
			onapplied?.();
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.bulkRecode.applyFailed'), 'error');
		} finally {
			busy = false;
		}
	}
</script>

<!-- The shell (backdrop, focus trap, Esc / backdrop-click close, page-scroll
     lock, background inert) is the shared Modal's; this file owns the body. -->
<Modal ariaLabel={m('invoices.bulkRecode.dialogLabel')} width="lg" {onclose}>
	{#snippet header()}
		<header class="modal-header">
			<div class="help-row">
				<h2>{m('invoices.bulkRecode.title')}</h2>
				<HelpTip term="gl-coding" />
			</div>
			<button type="button" class="btn-icon" onclick={onclose} aria-label={m('invoices.bulkRecode.close')}>&times;</button>
		</header>
	{/snippet}

	<div class="modal-body">
		<p class="intro">{m('invoices.bulkRecode.intro')}</p>

		<div class="filters">
			<label>
				{m('invoices.bulkRecode.from')}
				<input type="date" bind:value={from_date} disabled={busy} />
			</label>
			<label>
				{m('invoices.bulkRecode.to')}
				<input type="date" bind:value={to_date} disabled={busy} />
			</label>
		</div>

		<label class="ai-toggle">
			<input type="checkbox" bind:checked={include_ai_fallback} disabled={busy} />
			{m('invoices.bulkRecode.aiToggle')}
			<span class="ai-warning">{m('invoices.bulkRecode.aiBilled')}</span>
		</label>

		{#if !preview}
			<div class="actions">
				<button class="btn-cancel" onclick={onclose} disabled={busy}>{m('common.cancel')}</button>
				<button class="btn-primary" onclick={runDryRun} disabled={busy}>
					{busy ? m('invoices.bulkRecode.previewing') : m('invoices.bulkRecode.preview')}
				</button>
			</div>
		{:else}
			<div class="report">
				<dl class="summary">
					<dt>{m('invoices.bulkRecode.summary.matched')}</dt>
					<dd>{preview.matched}</dd>
					<dt>{m('invoices.bulkRecode.summary.wouldChange')}</dt>
					<dd class="emph">{preview.would_change ?? preview.applied ?? 0}</dd>
					<dt>{m('invoices.bulkRecode.summary.fromPriors')}</dt>
					<dd>{preview.by_source.vendor_prior}</dd>
					{#if include_ai_fallback}
						<dt>{m('invoices.bulkRecode.summary.aiCandidates')}</dt>
						<dd>
							{preview.ai_candidates}
							<span class="hint">{m('invoices.bulkRecode.summary.aiHint')}</span>
						</dd>
					{/if}
					<dt>{m('invoices.bulkRecode.summary.skippedNoChange')}</dt>
					<dd>{preview.skipped.no_change}</dd>
					<dt>{m('invoices.bulkRecode.summary.skippedImmutable')}</dt>
					<dd>{preview.skipped.immutable_status}</dd>
					<dt>{m('invoices.bulkRecode.summary.skippedNoPrior')}</dt>
					<dd>{preview.skipped.no_prior_no_ai}</dd>
				</dl>

				{#if preview.changes.length > 0}
					<h3>{m('invoices.bulkRecode.changes', { n: preview.changes.length })}</h3>
					<div class="changes">
						<table>
							<thead>
								<tr>
									<th>{m('invoices.bulkRecode.col.invoice')}</th>
									<th>{m('invoices.bulkRecode.col.vendor')}</th>
									<th>{m('invoices.bulkRecode.col.old')}</th>
									<th>{m('invoices.bulkRecode.col.new')}</th>
									<th>{m('invoices.bulkRecode.col.source')}</th>
								</tr>
							</thead>
							<tbody>
								{#each preview.changes.slice(0, 20) as c (c.invoice_id)}
									<tr>
										<td class="mono">{c.invoice_number}</td>
										<td>{c.vendor_name}</td>
										<td class="mono muted">{c.old_gl ?? '—'}</td>
										<td class="mono">{c.new_gl}</td>
										<td><span class="src src-{c.source}">{sourceLabel(c.source)}</span></td>
									</tr>
								{/each}
							</tbody>
						</table>
						{#if preview.changes.length > 20}
							<div class="more">{m('invoices.bulkRecode.more', { n: preview.changes.length - 20 })}</div>
						{/if}
					</div>
				{:else}
					<div class="empty">{m('invoices.bulkRecode.empty')}</div>
				{/if}

				<div class="actions">
					<button class="btn-cancel" onclick={() => (preview = null)} disabled={busy}>
						{m('invoices.bulkRecode.editFilters')}
					</button>
					<button
						class="btn-primary"
						onclick={applyChanges}
						disabled={busy || (preview.changes.length === 0 && preview.ai_candidates === 0)}
					>
						{busy ? m('invoices.bulkRecode.applying') : applyButtonLabel(preview)}
					</button>
				</div>
			</div>
		{/if}
	</div>
</Modal>

<style>
	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 12px;
		padding-bottom: 16px;
		border-bottom: 1px solid var(--border);
	}

	.help-row h2 {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}

	.modal-body {
		padding-top: 20px;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.intro {
		margin: 0;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.filters {
		display: flex;
		gap: 12px;
	}

	.filters label {
		display: flex;
		flex-direction: column;
		font-size: 0.78rem;
		color: var(--text-muted);
		gap: 4px;
	}
	/* Text-entry recipe. No checkbox sits under this selector today, but it
	   outranks the global control base in `app.css`, so the carve-out keeps a
	   later one from losing its tick (`background:` resets the drawn mark). */
	.filters input:not([type='checkbox']):not([type='radio']) {
		padding: 6px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
	}
	.ai-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.85rem;
	}

	.ai-warning {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 8px;
	}

	.summary {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 4px 14px;
		margin: 0 0 6px;
		padding: 10px 14px;
		background: var(--bg);
		border-radius: 6px;
		border: 1px solid var(--border);
	}

	.summary dt {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.summary dd {
		margin: 0;
		font-size: 0.88rem;
		font-weight: 500;
	}
	.summary dd.emph {
		color: var(--accent);
	}

	.summary .hint {
		font-size: 0.72rem;
		color: var(--text-muted);
		margin-left: 6px;
	}

	.report h3 {
		margin: 6px 0 4px;
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.changes {
		max-height: 280px;
		overflow-y: auto;
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.changes table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.changes th {
		background: var(--bg);
		text-align: left;
		padding: 8px 10px;
		font-size: 0.72rem;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}
	.changes td {
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
	}
	.mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
	}

	.muted {
		color: var(--text-muted);
	}

	.more {
		text-align: center;
		padding: 6px;
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.empty {
		text-align: center;
		padding: 18px;
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.src {
		display: inline-block;
		padding: 1px 8px;
		border-radius: 10px;
		font-size: 0.7rem;
		font-weight: 500;
	}

	.src-vendor_prior {
		background: color-mix(in srgb, var(--success) 15%, transparent);
		color: var(--success);
	}

	.src-ai {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}

</style>
