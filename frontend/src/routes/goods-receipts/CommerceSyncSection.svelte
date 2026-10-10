<script lang="ts">
	/**
	 * A receipt's commerce-platform sync — one row per received line and
	 * direction, with its status chip and a Retry for a failed push or a skip an
	 * admin has since fixed (decisions §268). Renders nothing for a receipt that
	 * planned no sync (no platform connected when it was recorded).
	 *
	 * The push runs after the receipt commits, so a just-recorded receipt can
	 * still read `pending`; the section re-reads when its receipt changes and
	 * after a retry. A retry re-sends under the same idempotency key, so it can
	 * never put a line on the shelf twice.
	 */
	import { listCommerceSyncs, retryCommerceSync } from '#lib/api/commerce.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		COMMERCE_SYNC_STATUS_KEYS,
		COMMERCE_SYNC_STATUS_TONES,
		syncReasonKey,
		type CommerceSync
	} from '#lib/types/commerce.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

	let {
		receiptId,
		version = '',
		canRetry
	}: {
		receiptId: string;
		/** Changes when the receipt does (its status) — a cancel plans reversals. */
		version?: string;
		/** The viewer may record receipts (`RECEIPT_ENTRY_ROLES`), so may retry. */
		canRetry: boolean;
	} = $props();

	let rows = $state<CommerceSync[]>([]);
	let loaded = $state(false);
	let failed = $state(false);
	let retrying = $state<string | null>(null);
	const sequence = createRequestSequencer();

	async function load(id: string) {
		const token = sequence.start();
		try {
			const data = await listCommerceSyncs(id);
			if (!sequence.canCommit(token)) return;
			rows = data.items;
			failed = false;
		} catch {
			if (!sequence.isCurrentRequest(token)) return;
			failed = true;
		} finally {
			if (sequence.isCurrentRequest(token)) loaded = true;
		}
	}

	/** A push runs after the receipt commits, so a fresh receipt can read pending. */
	const inProgress = $derived(rows.some((r) => r.status === 'pending' || r.status === 'in_flight'));

	$effect(() => {
		const id = receiptId;
		void version;
		loaded = false;
		rows = [];
		void load(id);
	});

	async function retry(row: CommerceSync) {
		retrying = row.id;
		try {
			await retryCommerceSync(row.id);
			toast(m('commerceSync.retryQueued'), 'success');
			await load(receiptId);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('commerceSync.retryFailed'), 'error');
		} finally {
			retrying = null;
		}
	}

	function reasonText(row: CommerceSync): string {
		const key = syncReasonKey(row.reason);
		if (!key) return '';
		const text = m(key);
		// A provider's own paraphrase, kept for an unrecognised code.
		return key === 'commerceSync.reason.other' && row.error_message
			? `${text} ${row.error_message}`
			: text;
	}
</script>

{#if failed}
	<p class="muted" role="alert" data-testid="commerce-sync-failed">{m('commerceSync.loadFailed')}</p>
{:else if loaded && rows.length}
	<section class="commerce-sync" data-testid="commerce-sync">
		<div class="head">
			<h3>{m('commerceSync.title')}</h3>
			{#if inProgress}
				<button
					type="button"
					class="btn-inline"
					onclick={() => load(receiptId)}
					data-testid="commerce-sync-refresh"
				>
					{m('commerceSync.refresh')}
				</button>
			{/if}
		</div>
		<table class="line-table">
			<thead>
				<tr>
					<th scope="col">{m('commerceSync.col.item')}</th>
					<th scope="col" class="right">{m('commerceSync.col.quantity')}</th>
					<th scope="col">{m('commerceSync.col.status')}</th>
					<th scope="col"><span class="visually-hidden">{m('commerceSync.col.actions')}</span></th>
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.id)}
					<tr data-testid="commerce-sync-row" data-status={row.status}>
						<td>
							<span class="mono">{row.sku ?? '—'}</span>
							<span class="direction">
								{row.direction === 'reverse'
									? m('commerceSync.direction.reverse')
									: m('commerceSync.direction.receive')}
							</span>
						</td>
						<td class="right mono">{row.quantity_delta}</td>
						<td>
							<Badge tone={COMMERCE_SYNC_STATUS_TONES[row.status]} variant={row.status}>
								{m(COMMERCE_SYNC_STATUS_KEYS[row.status])}
							</Badge>
							{#if row.reason}
								<span class="reason" data-testid="commerce-sync-reason">{reasonText(row)}</span>
							{/if}
						</td>
						<td class="right">
							{#if row.can_retry && canRetry}
								<button
									type="button"
									class="btn-inline"
									disabled={retrying === row.id}
									aria-label={m('commerceSync.retryAria', { sku: row.sku ?? '—' })}
									onclick={() => retry(row)}
									data-testid="commerce-sync-retry"
								>
									{m('commerceSync.retry')}
								</button>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</section>
{/if}

<style>
	.commerce-sync {
		margin-top: 20px;
	}

	.head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 8px;
	}

	h3 {
		font-size: 0.9rem;
		font-weight: 600;
		margin: 0;
	}

	.direction,
	.reason {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
		overflow-wrap: anywhere;
	}

	.muted {
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	/* The receipt modal's line-table look, which is scoped to its page. */
	.line-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}

	.line-table th {
		text-align: left;
		padding: 6px 10px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}

	.line-table td {
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
	}

	.line-table .mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}

	.line-table .right {
		text-align: right;
	}

	.btn-inline {
		background: none;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 10px;
		min-height: 24px;
		font-size: 0.78rem;
		font-family: inherit;
		color: var(--text);
		cursor: pointer;
	}

	.btn-inline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
</style>
