<script lang="ts">
	/**
	 * One imported bank statement, line by line — where the reconciliation
	 * judgment actually gets made.
	 *
	 * The rule this view exists to honour: **confidence scores an IDENTITY, not
	 * a reconciliation.** A 50–70 fuzzy vendor-name hit is a suggestion a human
	 * still owes a decision on, and a confidence-100 reference hit can still be
	 * a discrepancy (the bank moved a different amount / currency, or moved
	 * money against a payment our books say never went out). So every row
	 * renders its state from `transactionMatchState`, never from
	 * `matched_payment_id` alone, and the rows needing a decision carry the
	 * prominent affordance.
	 *
	 * Resolve semantics (`POST .../resolve`) as the backend defines them:
	 *   - sending a payment id supplies an IDENTITY; the server re-runs the
	 *     SAME `classify_discrepancy` the matcher used, so a human cannot click
	 *     a $10 line into place as the clean clearing of a $10,000 payment;
	 *   - re-sending a row's EXISTING payment id is therefore how a low-
	 *     confidence auto-match is CONFIRMED — it stamps the human's decision
	 *     at confidence 100 after re-checking it;
	 *   - sending `null` clears the match back to unmatched.
	 */
	import type {
		BankStatement,
		BankTransaction,
		MatchState,
		UnclearedPayment
	} from '$lib/types/bankReconciliation';
	import {
		MATCH_STATE_TONES,
		needsHumanDecision,
		transactionMatchState
	} from '$lib/types/bankReconciliation';
	import { getOutstandingItems, resolveBankTransaction } from '$lib/api/bankReconciliation';
	import Badge from '$lib/components/ui/Badge.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { formatDate } from '$lib/utils/time';

	let {
		statement,
		onclose,
		onchanged
	}: {
		statement: BankStatement;
		onclose: () => void;
		/** Fired with the refreshed statement after every successful resolve. */
		onchanged: (s: BankStatement) => void;
	} = $props();

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let detail = $state<BankStatement>(statement);
	/* eslint-enable svelte/state-referenced-locally */

	// Mutate is admin | ap_manager (`_WRITE_ROLES`) — a clerk reads the same
	// rows and gets no resolve controls.
	const canResolve = $derived(auth.isManager);

	const transactions = $derived(detail.transactions ?? []);

	const rows = $derived(
		transactions.map((tx) => ({ tx, state: transactionMatchState(tx) }))
	);

	const needsDecisionCount = $derived(rows.filter((r) => needsHumanDecision(r.state)).length);

	const STATE_LABELS: Record<MatchState, string> = $derived({
		credit: m('bankRecon.state.credit'),
		unmatched: m('bankRecon.state.unmatched'),
		discrepancy: m('bankRecon.state.discrepancy'),
		confirmed: m('bankRecon.state.confirmed'),
		probable: m('bankRecon.state.probable'),
		suggested: m('bankRecon.state.suggested')
	});

	/**
	 * A match method's label. An UNKNOWN method falls back to the raw value
	 * rather than to a friendly-sounding default — a method this frontend has
	 * never heard of must read as unfamiliar, not as "manual". Written out
	 * rather than built from a template so the key set stays statically
	 * checkable against the catalogue.
	 */
	const METHOD_LABELS: Record<string, string> = $derived({
		provider_id: m('bankRecon.method.provider_id'),
		amount_date: m('bankRecon.method.amount_date'),
		fuzzy_vendor: m('bankRecon.method.fuzzy_vendor'),
		manual: m('bankRecon.method.manual'),
		amount_mismatch: m('bankRecon.method.amount_mismatch'),
		currency_mismatch: m('bankRecon.method.currency_mismatch'),
		status_conflict: m('bankRecon.method.status_conflict')
	});

	function methodLabel(method: string | null): string {
		if (!method) return '—';
		return METHOD_LABELS[method] ?? method;
	}

	function txLabel(tx: BankTransaction): string {
		return tx.counterparty_name || tx.reference || tx.description || tx.id.slice(0, 8);
	}

	const COLUMNS = $derived([
		{ label: m('bankRecon.col.date') },
		{ label: m('bankRecon.col.counterparty') },
		{ label: m('bankRecon.col.reference') },
		{ label: m('bankRecon.col.amount'), class: 'right' },
		{ label: m('bankRecon.col.match') },
		{ label: m('bankRecon.col.variance'), class: 'right' },
		{ class: 'actions-col' }
	]);

	// --- Resolve ----------------------------------------------------------

	let busyTxId = $state<string | null>(null);

	async function resolve(tx: BankTransaction, paymentId: string | null) {
		if (!canResolve || busyTxId) return;
		busyTxId = tx.id;
		try {
			const updated = await resolveBankTransaction(detail.id, tx.id, paymentId);
			detail = updated;
			pickerTxId = null;
			onchanged(updated);
			toast(
				paymentId ? m('bankRecon.toast.matched') : m('bankRecon.toast.cleared'),
				'success'
			);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('bankRecon.toast.resolveFailed'), 'error');
		} finally {
			busyTxId = null;
		}
	}

	// --- Payment picker ---------------------------------------------------
	//
	// The candidate set is `/outstanding`'s `uncleared_payments` bucket, which
	// is exactly "payments our books say went out that no bank line claims" —
	// the only payments a bank debit can legitimately be pointed at. Anything
	// already claimed would be refused with a 409 by the one-payment-one-line
	// invariant, so offering it would be offering a dead end.

	let pickerTxId = $state<string | null>(null);
	let candidates = $state<UnclearedPayment[] | null>(null);
	let candidatesLoading = $state(false);
	let candidatesError = $state(false);
	let candidateFilter = $state('');

	async function loadCandidates() {
		candidatesLoading = true;
		candidatesError = false;
		try {
			// `limit` caps rows only; the counts on that response are whole-set.
			const res = await getOutstandingItems({ limit: 1000 });
			candidates = res.uncleared_payments;
		} catch {
			candidates = null;
			candidatesError = true;
		} finally {
			candidatesLoading = false;
		}
	}

	function openPicker(tx: BankTransaction) {
		pickerTxId = pickerTxId === tx.id ? null : tx.id;
		candidateFilter = '';
		if (pickerTxId && candidates === null && !candidatesLoading) void loadCandidates();
	}

	const filteredCandidates = $derived.by(() => {
		const all = candidates ?? [];
		const term = candidateFilter.trim().toLowerCase();
		if (!term) return all;
		return all.filter((p) =>
			[p.vendor_name, p.invoice_number, p.method, p.status]
				.filter((v): v is string => !!v)
				.some((v) => v.toLowerCase().includes(term))
		);
	});

	function candidateLabel(p: UnclearedPayment): string {
		return p.vendor_name || p.invoice_number || p.payment_id.slice(0, 8);
	}
</script>

<Modal
	ariaLabel={m('bankRecon.detail.aria', { account: detail.account_identifier })}
	width="lg"
	{onclose}
>
	{#snippet header()}
		<h2>{detail.account_identifier}</h2>
		<p class="detail-sub">
			{formatDate(detail.period_start)} – {formatDate(detail.period_end)} · {detail.currency} · {detail.source_format}
		</p>
	{/snippet}

	<div class="detail-summary">
		<span class="summary-figure">
			{m('bankRecon.detail.reconciledOf', {
				matched: detail.matched_count,
				count: detail.transaction_count
			})}
		</span>
		{#if needsDecisionCount > 0}
			<Badge tone="warning" variant="needs-review">
				{m('bankRecon.detail.needsReview', { count: needsDecisionCount })}
			</Badge>
		{/if}
		{#if detail.discrepancy_count > 0}
			<Badge tone="danger" variant="discrepancy-count">
				{m('bankRecon.kpi.discrepancies')}: {detail.discrepancy_count}
			</Badge>
		{/if}
	</div>

	<h3 class="section-title">{m('bankRecon.detail.transactions')}</h3>

	<DataTable
		columns={COLUMNS}
		isEmpty={rows.length === 0}
		empty={m('bankRecon.detail.noTransactions')}
		colspan={7}
	>
		{#snippet body()}
			{#each rows as { tx, state } (tx.id)}
				<tr class:attention={needsHumanDecision(state)}>
					<td class="muted">{formatDate(tx.transaction_date)}</td>
					<td>
						<span class="counterparty">{tx.counterparty_name ?? '—'}</span>
						{#if tx.description}
							<span class="desc muted">{tx.description}</span>
						{/if}
					</td>
					<td class="mono muted">{tx.reference ?? '—'}</td>
					<td class="right"><Money amount={tx.amount} currency={tx.currency} mono /></td>
					<td>
						<div class="match-cell">
							<Badge tone={MATCH_STATE_TONES[state]} variant={state}>
								{STATE_LABELS[state]}
							</Badge>
							{#if tx.matched_payment_id}
								<span class="match-meta muted">
									{methodLabel(tx.match_method)}{#if tx.match_confidence !== null}
										· {m('bankRecon.confidence', { confidence: tx.match_confidence })}
									{/if}
								</span>
								{#if tx.matched_invoice_number}
									<span class="match-meta muted">{tx.matched_invoice_number}</span>
								{/if}
								{#if tx.matched_payment_amount !== null && tx.matched_payment_amount !== undefined}
									<span class="match-meta muted">
										{m('bankRecon.col.ourAmount')}:
										<Money
											amount={tx.matched_payment_amount}
											currency={tx.matched_payment_currency}
										/>
										{#if tx.matched_payment_status}· {tx.matched_payment_status}{/if}
									</span>
								{/if}
							{/if}
							{#if state === 'suggested'}
								<!-- The whole point of the surface: say out loud that this
								     is a guess, so nobody reads the row as a fact. -->
								<span class="suggested-help">{m('bankRecon.suggestedHelp')}</span>
							{/if}
						</div>
					</td>
					<td class="right">
						{#if tx.variance_amount !== null && tx.variance_amount !== undefined}
							<Money amount={tx.variance_amount} currency={tx.currency} mono />
						{:else}
							<span class="muted">—</span>
						{/if}
					</td>
					<td class="actions">
						{#if canResolve && tx.direction === 'debit'}
							{#if state === 'suggested'}
								<RowAction
									variant="success"
									disabled={busyTxId === tx.id}
									onclick={() => resolve(tx, tx.matched_payment_id)}
									ariaLabel={m('bankRecon.action.confirmMatchAria', { label: txLabel(tx) })}
								>
									{m('bankRecon.action.confirmMatch')}
								</RowAction>
							{/if}
							{#if tx.matched_payment_id}
								<RowAction
									variant="danger"
									disabled={busyTxId === tx.id}
									onclick={() => resolve(tx, null)}
									ariaLabel={m('bankRecon.action.clearMatchAria', { label: txLabel(tx) })}
								>
									{m('bankRecon.action.clearMatch')}
								</RowAction>
							{:else}
								<RowAction
									variant="accent"
									disabled={busyTxId === tx.id}
									onclick={() => openPicker(tx)}
									ariaLabel={m('bankRecon.action.matchAria', { label: txLabel(tx) })}
								>
									{m('bankRecon.action.match')}
								</RowAction>
							{/if}
						{/if}
					</td>
				</tr>

				{#if pickerTxId === tx.id}
					<tr class="picker-row">
						<td colspan="7">
							<div class="picker">
								<div class="picker-head">
									<strong>{m('bankRecon.picker.title')}</strong>
									<SearchBox
										bind:value={candidateFilter}
										placeholder={m('bankRecon.picker.placeholder')}
										ariaLabel={m('bankRecon.picker.aria')}
									/>
								</div>
								{#if candidatesLoading}
									<p class="picker-note muted">{m('common.loading')}</p>
								{:else if candidatesError}
									<p class="picker-note" role="alert">
										{m('bankRecon.error.load')}
										<button class="link-btn" onclick={loadCandidates}>
											{m('bankRecon.error.retry')}
										</button>
									</p>
								{:else if filteredCandidates.length === 0}
									<p class="picker-note muted">
										{candidateFilter.trim()
											? m('bankRecon.empty.filtered')
											: m('bankRecon.picker.empty')}
									</p>
								{:else}
									<ul class="picker-list">
										{#each filteredCandidates.slice(0, 50) as p (p.payment_id)}
											<li>
												<span class="picker-vendor">{candidateLabel(p)}</span>
												<span class="muted">{p.invoice_number ?? '—'}</span>
												<!-- The payment's own currency (its invoice's), which
												     `UnclearedPaymentResponse` carries. This used to label
												     every candidate with the org reporting currency on the
												     belief that the row had none — so a EUR payment offered
												     as a match for a EUR bank line read as dollars. -->
												<Money amount={p.amount} currency={p.currency} mono />
												<span class="muted">{formatDate(p.sent_on)}</span>
												<RowAction
													variant="accent"
													disabled={busyTxId === tx.id}
													onclick={() => resolve(tx, p.payment_id)}
													ariaLabel={m('bankRecon.picker.selectAria', {
														label: candidateLabel(p)
													})}
												>
													{m('bankRecon.picker.select')}
												</RowAction>
											</li>
										{/each}
									</ul>
								{/if}
							</div>
						</td>
					</tr>
				{/if}
			{/each}
		{/snippet}
	</DataTable>

	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
	</div>
</Modal>

<style>
	.detail-sub {
		margin: 2px 0 0;
		font-size: 0.8rem;
		color: var(--text-muted);
	}
	.detail-summary {
		display: flex;
		align-items: center;
		gap: 10px;
		flex-wrap: wrap;
		margin: 12px 0;
	}
	.summary-figure {
		font-size: 0.9rem;
		color: var(--text);
	}
	.section-title {
		margin: 16px 0 8px;
		font-size: 0.9rem;
		color: var(--text);
	}
	.muted {
		color: var(--text-muted);
	}
	.mono {
		font-variant-numeric: tabular-nums;
		font-family: var(--font-mono);
	}
	.counterparty {
		display: block;
	}
	.desc {
		display: block;
		font-size: 0.75rem;
	}
	.match-cell {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 3px;
	}
	.match-meta {
		font-size: 0.75rem;
	}
	.suggested-help {
		font-size: 0.75rem;
		color: var(--warning-on-tint);
		max-width: 24rem;
		line-height: 1.4;
	}
	/* A row still owing a decision gets a left rule — a position + shape cue,
	   not colour alone (WCAG 1.4.1); the state Badge beside it carries the
	   text. */
	tr.attention td:first-child {
		box-shadow: inset 3px 0 0 var(--warning-on-tint);
	}
	.picker-row td {
		background: var(--bg);
	}
	.picker {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 10px 4px;
	}
	.picker-head {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
		font-size: 0.85rem;
	}
	.picker-note {
		margin: 0;
		font-size: 0.85rem;
	}
	.picker-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-height: 260px;
		overflow-y: auto;
	}
	.picker-list li {
		display: grid;
		grid-template-columns: minmax(8rem, 1.5fr) minmax(6rem, 1fr) auto auto auto;
		align-items: center;
		gap: 10px;
		padding: 6px 8px;
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.85rem;
	}
	.picker-vendor {
		color: var(--text);
	}
	.link-btn {
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-on-tint);
		text-decoration: underline;
		cursor: pointer;
		font: inherit;
	}
</style>
