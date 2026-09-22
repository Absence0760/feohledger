<script lang="ts">
	import { onMount } from 'svelte';
	import { focusTrap } from '$lib/actions/focusTrap';
	import { api } from '$lib/api';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import {
		paymentMethodLabelKey,
		paymentStatusLabelKey,
		runStatusLabelKey,
		PAYMENT_STATUS_TONES,
		runStatusTone
	} from '$lib/types/payment';
	import type { PaymentStatus } from '$lib/types/payment';
	import { auth } from '$lib/stores/auth.svelte';
	import { PERM_PAYMENT_EXECUTE } from '$lib/types/admin';
	import { formatMoney } from '$lib/utils/money';
	// The bare (no-symbol) rendering `fmt` falls back to when the server could
	// not establish a figure's currency — the same primitive `/payments` and
	// `/discounts` use for their own unprovable-currency cases, so a change to
	// rounding or locale can't land in one and not the others.
	import { formatAmountWithoutCurrency } from '$lib/utils/discountRecommendation';
	import { m } from '$lib/i18n/store.svelte';

	let {
		runId,
		onclose,
		onchange,
	}: {
		runId: string;
		onclose: () => void;
		// Fired after Execute completes so the parent can refresh queue + summary + runs list.
		onchange?: () => void;
	} = $props();

	// Freeze background page scroll while the dialog is open (restored on close),
	// so a wheel event over the backdrop can't bleed through to the list behind it.
	$effect(() => {
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = prev;
		};
	});

	interface RunPayment {
		id: string;
		invoice_id: string;
		invoice_number: string | null;
		vendor_name: string | null;
		// Exact Decimal STRING money (never float); formatMoney coerces to display.
		amount: string;
		/**
		 * What `amount` is denominated in — the invoice's own currency, off the
		 * row `GET /api/payments/runs/{id}` already joins (`api/payments.py`).
		 * `payments` has no currency column; a payment settles in its invoice's
		 * currency.
		 *
		 * `null` means the server could not establish it (a legacy invoice
		 * carrying none) — render the bare figure, never a substituted default
		 * (`docs/decisions.md` §79/§82/§107).
		 */
		currency: string | null;
		method: string | null;
		status: string;
		reference: string | null;
	}

	interface RunDetail {
		id: string;
		status: string;
		total_amount: string;
		/**
		 * What `total_amount` is denominated in. `payment_runs` has no currency
		 * column either — the total is one bare `Numeric`, kept meaningful by
		 * `create_payment_run_for_invoices` refusing a run whose invoices span
		 * more than one currency, so the server derives the code from the legs
		 * (`services/payment_runs.py::one_currency`).
		 *
		 * `null` when it could not be PROVEN: a run with no payments, or a
		 * legacy run predating that guard whose legs disagree — in which case
		 * the total is denominated in nothing real and a code would be worse
		 * than none.
		 */
		currency: string | null;
		initiated_by: string | null;
		executed_at: string | null;
		created_at: string;
		requires_cfo_approval: boolean;
		cfo_approved_by: string | null;
		cfo_approved_at: string | null;
		payments: RunPayment[];
	}

	let run = $state<RunDetail | null>(null);
	let loading = $state(true);
	let executing = $state(false);
	let cancelling = $state(false);
	let confirmCancel = $state(false);
	// Execute is the one irreversible money-moving control in the web app, so
	// it arms before it commits — the same two-click shape its sibling
	// `confirmCancel` already uses in this footer (and that void, credit-memo
	// void, API-key revoke and the mobile app all use), rather than a bespoke
	// dialog. First click arms, second click moves the money.
	let confirmExecute = $state(false);
	let error = $state('');

	/** Arm one footer commit and disarm the other.
	 *
	 *  Two armed red buttons side by side is how a mis-click becomes the wrong
	 *  irreversible action: arming Execute must retract a previously-armed
	 *  Cancel and vice versa, so at most one control is ever one click from
	 *  committing. */
	function arm(which: 'execute' | 'cancel') {
		confirmExecute = which === 'execute';
		confirmCancel = which === 'cancel';
	}

	async function load() {
		loading = true;
		error = '';
		try {
			run = await api.get<RunDetail>(`/api/payments/runs/${runId}`);
		} catch (err) {
			error = err instanceof Error ? err.message : m('paymentRuns.runDetail.loadFailed');
		} finally {
			loading = false;
		}
	}

	onMount(load);

	async function execute() {
		if (!run || run.status !== 'draft') return;
		executing = true;
		try {
			const result = await api.post<{ message: string }>(
				`/api/payments/runs/${runId}/execute`,
				{}
			);
			toast(result.message, 'success');
			await load();
			onchange?.();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('paymentRuns.runDetail.executeFailed'), 'error');
		} finally {
			executing = false;
			// Disarm once the attempt is over, whichever way it went. On success
			// the run is no longer a draft and the footer branch is gone anyway;
			// on failure the run is still payable, and leaving a live money
			// button one click from firing is exactly what arming exists to
			// prevent.
			confirmExecute = false;
		}
	}

	let approving = $state(false);

	async function approveCfo() {
		if (!run || !run.requires_cfo_approval || run.cfo_approved_at) return;
		approving = true;
		try {
			await api.post(`/api/payments/runs/${runId}/approve`, {});
			toast(m('paymentRuns.runDetail.approvedToast'), 'success');
			await load();
			onchange?.();
		} catch (err) {
			const e = err as { detail?: string; message?: string } | null;
			toast(e?.detail ?? e?.message ?? m('paymentRuns.runDetail.approveFailed'), 'error');
		} finally {
			approving = false;
		}
	}

	async function cancelDraft() {
		if (!run || run.status !== 'draft') return;
		cancelling = true;
		try {
			const result = await api.post<{ message: string; released_invoices: number }>(
				`/api/payments/runs/${runId}/cancel`,
				{}
			);
			toast(result.message, 'success');
			onchange?.();
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('paymentRuns.runDetail.cancelFailed'), 'error');
		} finally {
			cancelling = false;
		}
	}

	function handleBackdrop(e: MouseEvent) {
		if (e.target === e.currentTarget) onclose();
	}

	// Esc + focus trap/restore are handled by the shared `focusTrap` action.

	/** Render money under the currency the SERVER stated, honestly.
	 *
	 *  Every money cell in this dialog goes through it. The five call sites used
	 *  to omit the argument entirely, so `formatMoney` fell back to
	 *  `DEFAULT_CURRENCY` and the whole dialog — run total, per-payment amounts,
	 *  and the amount printed on the Execute button that moves the money —
	 *  rendered in `$` for every tenant. Worse than the `/payments` bug
	 *  `docs/decisions.md` §107 fixed: this ignored the tenant's own default
	 *  too.
	 *
	 *  A stated code formats normally; an unstated one renders BARE rather than
	 *  borrowing the org default. `null` is exactly where the backend refused to
	 *  guess, so substituting a code there would reinstate the fabrication the
	 *  wire field exists to end — a symbol the reader takes as established fact
	 *  when nobody established it (§79/§82).
	 *
	 *  A payment row passes its OWN code, never the run's: they cannot
	 *  disagree (`one_currency` returns a code only when every leg carried
	 *  that same one, so a stated run currency implies every payment states it
	 *  too), and a fallback that can never fire is a claim about the data that
	 *  nothing checks.
	 *
	 *  Nothing here adds, compares or converts — the value passes through
	 *  untouched (money arrives as string-Decimal; `formatMoney` coerces it).
	 */
	function fmt(amount: number | string | null | undefined, currency: string | null): string {
		return currency ? formatMoney(amount, { currency }) : formatAmountWithoutCurrency(amount);
	}

	function fmtDate(s: string | null): string {
		if (!s) return '—';
		return new Date(s).toLocaleString('en-US', {
			month: 'short',
			day: 'numeric',
			year: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
	}

	// Rail and status are message keys, not English literals — a value this
	// build doesn't know renders raw rather than blank. The parameter is
	// `method`, not `m`: the i18n accessor is `m()` and a parameter of that
	// name would shadow it inside the function that needs it.
	function methodLabel(method: string | null): string {
		if (!method) return '—';
		const key = paymentMethodLabelKey(method);
		return key ? m(key) : method;
	}

	// The per-payment badge rendered the RAW enum value (`payment_scheduled`,
	// `pending_compliance`) while `/payments` rendered the same union through
	// its label map one click away — so the dialog showed a snake_case status
	// in an otherwise fully translated table.
	function statusLabel(status: string): string {
		const key = paymentStatusLabelKey(status);
		return key ? m(key) : status;
	}

	// Same rule for the RUN status in the header. It had no label map at all,
	// so this pill printed the raw enum (`executing`, `partial`) directly above
	// a table whose own status column round 24 had already keyed.
	function runLabel(status: string): string {
		const key = runStatusLabelKey(status);
		return key ? m(key) : status;
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions a11y_no_noninteractive_element_interactions -->
<div class="backdrop" onclick={handleBackdrop} role="presentation">
	<div use:focusTrap={{ onEscape: onclose }} class="modal" role="dialog" aria-label="Payment run" tabindex="-1">
		<header>
			<div class="title-block">
				<h2>{m('paymentRuns.runDetail.title')}</h2>
				{#if run}
					<span class="run-id">{run.id.slice(0, 8)}</span>
					<!-- `status-badge` is the e2e hook (tests-e2e/payments read it by
					     class and assert on textContent); the tone comes from the
					     shared map, never from a rule on the variant. -->
					<Badge tone={runStatusTone(run.status)} variant="status-badge {run.status}">{runLabel(run.status)}</Badge>
				{/if}
			</div>
			<button class="close-btn" onclick={onclose} aria-label={m('paymentRuns.runDetail.close')}>&times;</button>
		</header>

		<div class="body">
			{#if loading}
				<div class="loading">{m('common.loading')}</div>
			{:else if error}
				<div class="error" role="alert">{error}</div>
			{:else if run}
				<dl class="meta">
					<dt>{m('paymentRuns.runDetail.total')}</dt>
					<dd class="total" data-testid="run-total">{fmt(run.total_amount, run.currency)}</dd>
					<dt>{m('paymentRuns.runDetail.payments')}</dt>
					<dd>{run.payments.length}</dd>
					<dt>{m('paymentRuns.runDetail.created')}</dt>
					<dd>{fmtDate(run.created_at)}</dd>
					{#if run.executed_at}
						<dt>{m('paymentRuns.runDetail.executed')}</dt>
						<dd>{fmtDate(run.executed_at)}</dd>
					{/if}
				</dl>

				<table>
					<thead>
						<tr>
							<th>{m('paymentRuns.runDetail.colInvoice')}</th>
							<th>{m('paymentRuns.runDetail.colVendor')}</th>
							<th class="right">{m('paymentRuns.runDetail.colAmount')}</th>
							<th>{m('paymentRuns.runDetail.colMethod')}</th>
							<th>{m('paymentRuns.runDetail.colStatus')}</th>
							<th>{m('paymentRuns.runDetail.colReference')}</th>
						</tr>
					</thead>
					<tbody>
						{#each run.payments as p (p.id)}
							<tr>
								<td class="mono">{p.invoice_number ?? '—'}</td>
								<td>{p.vendor_name ?? '—'}</td>
								<td class="right mono" data-testid="run-payment-amount">
									{fmt(p.amount, p.currency)}
								</td>
								<td>{methodLabel(p.method)}</td>
								<!-- No `?? 'neutral'`: the map is total over `PaymentStatus`,
								     and a value off the union lands on `Badge`'s own `tone`
								     default — which IS neutral — rather than a fallback
								     restating it. -->
								<td>
									<Badge tone={PAYMENT_STATUS_TONES[p.status as PaymentStatus]} variant={p.status}>{statusLabel(p.status)}</Badge>
								</td>
								<td class="mono muted">{p.reference ?? '—'}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</div>

		<footer>
			{#if run?.status === 'draft'}
				{@const pendingCfo = run.requires_cfo_approval && !run.cfo_approved_at}
				{@const cfoApproved = run.requires_cfo_approval && !!run.cfo_approved_at}

				{#if pendingCfo}
					<p class="footer-note pending">
						<strong>{m('paymentRuns.runDetail.pendingCfoStrong')}</strong> {m('paymentRuns.runDetail.pendingCfoBody')}
					</p>
				{:else if cfoApproved}
					<p class="footer-note approved">
						{m('paymentRuns.runDetail.cfoApprovedNote', { date: fmtDate(run.cfo_approved_at) })}
					</p>
				{:else}
					<p class="footer-note">
						{m('paymentRuns.runDetail.draftNotePre')}<strong>{m('paymentRuns.runDetail.draftWord')}</strong>{m('paymentRuns.runDetail.draftNotePost')}
					</p>
				{/if}

				{#if confirmExecute}
					<!-- Announced (role="alert") the moment Execute arms, so a
					     screen-reader user learns the next click moves money
					     without having to re-read the button. -->
					<p class="footer-note armed-note" role="alert" data-testid="execute-armed-note">
						{m('paymentRuns.runDetail.executeArmedNote', {
							amount: fmt(run.total_amount, run.currency)
						})}
					</p>
				{/if}

				<div class="actions">
					<button class="btn-cancel" onclick={onclose}>{m('paymentRuns.runDetail.close')}</button>
					{#if confirmCancel}
						<button
							class="btn-discard armed"
							disabled={cancelling}
							onclick={cancelDraft}
						>
							{cancelling ? m('paymentRuns.runDetail.cancelling') : m('paymentRuns.runDetail.confirmCancel')}
						</button>
					{:else}
						<button
							class="btn-discard"
							disabled={cancelling || executing || approving}
							onclick={() => arm('cancel')}
						>
							{m('paymentRuns.runDetail.cancelRun')}
						</button>
					{/if}
					<!-- `hasRole('cfo')`, NOT `auth.isCfo` (= admin | cfo): the backend
					     gate is `require_roles(ROLE_CFO)` and require_roles does not
					     special-case admin, so an admin without the cfo role saw
					     "Approve as CFO" and every click 403'd — while Execute stayed
					     disabled on `pendingCfo`, leaving the run with no usable
					     control at all. Widening the backend is not the fix: a CFO
					     sign-off an admin can grant themselves is not a sign-off. -->
					{#if pendingCfo && auth.hasRole('cfo')}
						<button
							class="btn-approve"
							disabled={approving}
							onclick={approveCfo}
						>
							{approving ? m('paymentRuns.runDetail.approving') : m('paymentRuns.runDetail.approveAsCfo')}
						</button>
					{/if}
					{#if auth.can(PERM_PAYMENT_EXECUTE)}
						{#if confirmExecute}
							<!-- Armed. The label is deliberately DISTINCT from the
							     unarmed one ("Confirm execute · …" vs "Execute · …")
							     so the change is announced to a screen reader, and so
							     a test can wait on the armed control by name instead
							     of on a timer. -->
							<button
								class="btn-execute armed"
								disabled={executing || cancelling || pendingCfo}
								onclick={execute}
							>
								{executing
									? m('paymentRuns.runDetail.executing')
									: m('paymentRuns.runDetail.confirmExecuteAmount', {
											amount: fmt(run.total_amount, run.currency)
										})}
							</button>
						{:else}
							<button
								class="btn-execute"
								disabled={executing || cancelling || pendingCfo}
								title={pendingCfo ? m('paymentRuns.runDetail.awaitingCfo') : ''}
								onclick={() => arm('execute')}
							>
								{m('paymentRuns.runDetail.executeAmount', {
									amount: fmt(run.total_amount, run.currency)
								})}
							</button>
						{/if}
					{/if}
				</div>
			{:else}
				<div class="actions">
					<button class="btn-cancel" onclick={onclose}>{m('paymentRuns.runDetail.close')}</button>
				</div>
			{/if}
		</footer>
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
		width: min(820px, 95vw);
		max-height: 90vh;
		display: flex;
		flex-direction: column;
		box-shadow: 0 16px 48px rgba(0, 0, 0, 0.3);
	}

	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 16px 20px;
		border-bottom: 1px solid var(--border);
	}

	.title-block {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	h2 {
		margin: 0;
		font-size: 1.1rem;
		font-weight: 600;
	}

	.run-id {
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.82rem;
		color: var(--text-muted);
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

	.close-btn:hover {
		color: var(--text);
	}

	.body {
		padding: 20px;
		overflow-y: auto;
		flex: 1;
	}

	.loading,
	.error {
		padding: 40px;
		text-align: center;
		color: var(--text-muted);
	}

	.error {
		color: var(--danger);
	}

	dl.meta {
		display: grid;
		grid-template-columns: 90px 1fr 90px 1fr;
		gap: 8px 14px;
		margin: 0 0 18px;
		padding-bottom: 14px;
		border-bottom: 1px solid var(--border);
	}

	dt {
		font-size: 0.75rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
		align-self: center;
	}

	dd {
		margin: 0;
		font-size: 0.9rem;
	}

	dd.total {
		font-weight: 700;
		font-size: 1rem;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}

	th {
		background: var(--bg);
		text-align: left;
		padding: 8px 10px;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}

	td {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		white-space: nowrap;
	}

	tr:last-child td {
		border-bottom: none;
	}

	.mono {
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.8rem;
	}

	.right {
		text-align: right;
	}

	.muted {
		color: var(--text-muted);
	}

	/* No status-badge rules here on purpose. Both pills in this dialog are
	   `<Badge>`, and their tones come from `PAYMENT_STATUS_TONES` /
	   `RUN_STATUS_TONES` in `$lib/types/payment` — shared with `/payments`,
	   which badges the same two vocabularies one click away. This file used to
	   carry its own seven rules and they had already drifted: `draft` was
	   amber here and flat there, and `submitted` / `cancelled` / `voided` /
	   `pending_compliance` had no rule at all, so half the payment-status
	   union rendered untinted in this table. The `status-badge` / `<status>`
	   classes survive as `variant` — selector hooks for the e2e suite, never
	   colour (decisions.md §47). */

	footer {
		padding: 14px 20px;
		border-top: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.footer-note {
		margin: 0;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}

	.btn-cancel {
		padding: 8px 16px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-cancel:hover {
		background: var(--bg);
	}

	.btn-execute {
		padding: 8px 20px;
		border-radius: 4px;
		border: none;
		background: var(--success-strong);
		color: #fff;
		font-size: 0.88rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-execute:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-execute:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Armed: the next click moves money. Reads as a warning, not as the
	   calm green "ready" state, and mirrors how `.btn-discard.armed`
	   changes appearance one step before it commits. */
	.btn-execute.armed {
		/* --danger-strong, not --danger: this is a fill carrying white text
		   (5.37:1), the same call `app.css` records for every red fill. */
		background: var(--danger-strong);
		box-shadow: 0 0 0 2px rgba(196, 53, 53, 0.3);
	}

	.footer-note.armed-note {
		color: var(--danger);
		font-weight: 600;
	}

	.btn-discard {
		padding: 8px 16px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-discard:hover:not(:disabled):not(.armed) {
		border-color: var(--danger);
		color: var(--danger);
	}

	.btn-discard.armed {
		border-color: var(--danger);
		background: rgba(224, 64, 64, 0.1);
		color: var(--danger);
	}

	.btn-discard:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.footer-note.pending {
		color: #d4940a;
	}

	.footer-note.approved {
		color: #1fa86a;
	}

	.btn-approve {
		padding: 8px 16px;
		border-radius: 4px;
		border: 1px solid var(--accent-strong);
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-approve:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn-approve:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
