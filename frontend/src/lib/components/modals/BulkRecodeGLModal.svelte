<script lang="ts">
	import { focusTrap } from '#lib/actions/focusTrap.ts';
	import { api } from '#lib/api.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';

	interface Props {
		onclose: () => void;
		onapplied?: () => void;
	}

	let { onclose, onapplied }: Props = $props();

	// Freeze background page scroll while the dialog is open (restored on close),
	// so a wheel event over the backdrop can't bleed through to the list behind it.
	$effect(() => {
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = prev;
		};
	});

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
			toast(err instanceof Error ? err.message : 'Preview failed', 'error');
		} finally {
			busy = false;
		}
	}

	function applyButtonLabel(p: RecodeReport): string {
		const priorChanges = p.changes.length;
		const aiCandidates = p.ai_candidates;
		if (aiCandidates > 0 && priorChanges > 0) {
			return `Apply ${priorChanges} + run AI on ${aiCandidates}`;
		}
		if (aiCandidates > 0) {
			return `Run AI on ${aiCandidates} invoice(s)`;
		}
		return `Apply ${priorChanges} change(s)`;
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
			toast(`Re-coded ${report.applied ?? 0} invoice(s)`, 'success');
			onapplied?.();
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Apply failed', 'error');
		} finally {
			busy = false;
		}
	}

	// Esc + focus trap/restore are handled by the shared `focusTrap` action.
</script>

<div
	class="backdrop"
	role="presentation"
	onclick={(e) => {
		if (e.target === e.currentTarget) onclose();
	}}
>
	<div use:focusTrap={{ onEscape: onclose }} class="modal" role="dialog" aria-label="Bulk re-code GL" tabindex="-1">
		<header class="modal-header">
			<h2>Bulk Re-code GL Codes</h2>
			<button class="close-btn" onclick={onclose} aria-label="Close">&times;</button>
		</header>

		<div class="modal-body">
			<p class="intro">
				Re-applies cached vendor GL corrections to invoices in the selected scope.
				Posted, paid, and in-flight invoices are skipped automatically.
			</p>

			<div class="filters">
				<label>
					From
					<input type="date" bind:value={from_date} disabled={busy} />
				</label>
				<label>
					To
					<input type="date" bind:value={to_date} disabled={busy} />
				</label>
			</div>

			<label class="ai-toggle">
				<input type="checkbox" bind:checked={include_ai_fallback} disabled={busy} />
				Include AI fallback for invoices with no learned GL code
				<span class="ai-warning">(billed per invoice)</span>
			</label>

			{#if !preview}
				<div class="actions">
					<button class="btn-cancel" onclick={onclose} disabled={busy}>Cancel</button>
					<button class="btn-primary" onclick={runDryRun} disabled={busy}>
						{busy ? 'Previewing…' : 'Preview Changes'}
					</button>
				</div>
			{:else}
				<div class="report">
					<dl class="summary">
						<dt>Matched</dt>
						<dd>{preview.matched}</dd>
						<dt>Would change</dt>
						<dd class="emph">{preview.would_change ?? preview.applied ?? 0}</dd>
						<dt>From cached priors</dt>
						<dd>{preview.by_source.vendor_prior}</dd>
						{#if include_ai_fallback}
							<dt>AI candidates</dt>
							<dd>
								{preview.ai_candidates}
								<span class="hint">(would re-extract on apply)</span>
							</dd>
						{/if}
						<dt>Skipped (already coded)</dt>
						<dd>{preview.skipped.no_change}</dd>
						<dt>Skipped (immutable status)</dt>
						<dd>{preview.skipped.immutable_status}</dd>
						<dt>Skipped (no learned code)</dt>
						<dd>{preview.skipped.no_prior_no_ai}</dd>
					</dl>

					{#if preview.changes.length > 0}
						<h3>Changes ({preview.changes.length})</h3>
						<div class="changes">
							<table>
								<thead>
									<tr>
										<th>Invoice</th>
										<th>Vendor</th>
										<th>Old</th>
										<th>New</th>
										<th>Source</th>
									</tr>
								</thead>
								<tbody>
									{#each preview.changes.slice(0, 20) as c (c.invoice_id)}
										<tr>
											<td class="mono">{c.invoice_number}</td>
											<td>{c.vendor_name}</td>
											<td class="mono muted">{c.old_gl ?? '—'}</td>
											<td class="mono">{c.new_gl}</td>
											<td><span class="src src-{c.source}">{c.source}</span></td>
										</tr>
									{/each}
								</tbody>
							</table>
							{#if preview.changes.length > 20}
								<div class="more">…and {preview.changes.length - 20} more</div>
							{/if}
						</div>
					{:else}
						<div class="empty">No changes would land in this scope.</div>
					{/if}

					<div class="actions">
						<button class="btn-cancel" onclick={() => (preview = null)} disabled={busy}>
							Edit filters
						</button>
						<button
							class="btn-primary"
							onclick={applyChanges}
							disabled={busy || (preview.changes.length === 0 && preview.ai_candidates === 0)}
						>
							{busy ? 'Applying…' : applyButtonLabel(preview)}
						</button>
					</div>
				</div>
			{/if}
		</div>
	</div>
</div>

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.5);
		display: grid;
		place-items: center;
		z-index: 100;
		backdrop-filter: blur(2px);
	}
	.modal {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		width: min(720px, 95vw);
		max-height: 90vh;
		display: flex;
		flex-direction: column;
		box-shadow: 0 16px 48px rgba(0, 0, 0, 0.3);
	}
	.modal-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 16px 20px;
		border-bottom: 1px solid var(--border);
	}
	.modal h2 {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}
	.close-btn {
		background: none;
		border: none;
		font-size: 1.5rem;
		cursor: pointer;
		color: var(--text-muted);
		line-height: 1;
		padding: 0 4px;
	}
	.modal-body {
		padding: 20px;
		overflow-y: auto;
		flex: 1;
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
	.btn-cancel,
	.btn-primary {
		padding: 8px 16px;
		border-radius: 6px;
		font-family: inherit;
		font-size: 0.85rem;
		cursor: pointer;
		border: 1px solid var(--border);
	}
	.btn-cancel {
		background: var(--surface);
		color: var(--text);
	}
	.btn-primary {
		background: var(--accent-strong);
		color: white;
		border-color: var(--accent-strong);
	}
	.btn-primary:disabled,
	.btn-cancel:disabled {
		opacity: 0.5;
		cursor: not-allowed;
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
		background: rgba(31, 168, 106, 0.15);
		color: #1fa86a;
	}
	.src-ai {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}
</style>
