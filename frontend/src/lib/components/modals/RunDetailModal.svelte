<script lang="ts">
	import { onMount } from 'svelte';
	import { focusTrap } from '#lib/actions/focusTrap.ts';
	import { api } from '#lib/api.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import {
		paymentMethodLabelKey,
		paymentStatusLabelKey,
		runStatusLabelKey,
		PAYMENT_STATUS_TONES,
		runStatusTone
	} from '#lib/types/payment.ts';
	import type { PaymentStatus } from '#lib/types/payment.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { PERM_PAYMENT_EXECUTE, PERM_PAYMENT_RECORD_EXTERNAL } from '#lib/types/admin.ts';
	import {
		downloadRunNacha,
		recordRunOutside,
		retryFailedRunPayments,
		voidRunNachaExport
	} from '#lib/api/payments.ts';
	import {
		isRunRetryOffered,
		retrySkipReasonKey,
		type RetryFailedResult
	} from '#lib/types/runRetry.ts';
	import {
		isAllowed,
		isOffered,
		refusalText,
		type ActionRefusal
	} from '#lib/api/actionVerdicts.ts';
	import {
		NACHA_SEC_CODES,
		canDispatchPayments,
		canDownloadBankFile,
		isBusinessDay,
		isExternalPayment,
		localToday,
		nextBusinessDay,
		utcToday,
		type NachaSecCode,
		type PaymentExecutionMode
	} from '#lib/types/paymentExecution.ts';
	import { formatMoney, isPositiveAmount } from '#lib/utils/money.ts';
	// The bare (no-symbol) rendering `fmt` falls back to when the server could
	// not establish a figure's currency — the same primitive `/payments` and
	// `/discounts` use for their own unprovable-currency cases, so a change to
	// rounding or locale can't land in one and not the others.
	import { formatAmountWithoutCurrency } from '#lib/utils/discountRecommendation.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';

	let {
		runId,
		onclose,
		onchange,
		executionMode = null,
	}: {
		runId: string;
		onclose: () => void;
		// Fired after Execute completes so the parent can refresh queue + summary + runs list.
		onchange?: () => void;
		/**
		 * The tenant's payment execution mode (`GET /api/payments/execution-mode`,
		 * issue #517), read once by the page. `null` = not known yet, or the read
		 * failed — and Execute is hidden then too (`canDispatchPayments` fails
		 * closed): the server would 409 a record-only tenant's execute, and the
		 * record-outside path below works in either mode.
		 */
		executionMode?: PaymentExecutionMode | null;
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
		/** `"external"` once the run was recorded as paid outside FeohLedger. */
		provider?: string | null;
		/**
		 * The accepted early-payment discount this payment takes, already
		 * deducted from `amount` (migration 0104), and the invoice amount it
		 * was taken from. `null` when it pays the full net — never a zero
		 * standing in for "no discount". Optional so an older backend parses.
		 */
		discount_amount?: string | null;
		discount_offer_id?: string | null;
		invoice_amount?: string | null;
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
		/** Early-payment discounts the run's active payments take — already
		 *  deducted from `total_amount`. Exact Decimal string. */
		discount_total?: string;
		/** The CALLER's verdict on the three draft-run decisions — each `null`
		 *  when its endpoint would accept them, else the coded refusal it would
		 *  answer with (`api/payments.py::run_action_refusals`, from the gates
		 *  `/approve`, `/execute` and `/cancel` raise from). The footer offers a
		 *  control from this and never restates its rule (issue #575). */
		actions: {
			approve: ActionRefusal;
			execute: ActionRefusal;
			cancel: ActionRefusal;
			/** `POST /runs/{id}/retry-failed`'s verdict
			 *  (`payment_controls.run_retry_failed_refusal`). Optional so an older
			 *  backend parses — and an absent verdict offers nothing. */
			retry_failed?: ActionRefusal;
		};
		/** Failures `/retry-failed` would actually send again (retry-safe,
		 *  latest attempt). Only used to word the armed note — the verdict, not
		 *  this count, decides whether the control is offered. */
		retryable_failures?: number;
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

	// --- Paying the run outside FeohLedger (issue #517) -----------------------
	// A record-only tenant's run is paid by the customer's own bank: the
	// operator downloads the run's NACHA file (which CLAIMS the run,
	// `draft → exported`), uploads it, and records the run as paid — or, if the
	// bank rejected the file, releases the run back to `draft`. Recording is
	// also offered on a processor tenant's draft (Execute stays the primary
	// there).
	//
	// Two permissions, deliberately split by the backend: the file and its void
	// are `payment.execute` (uploading the file is what moves the money), the
	// record is `payment.record_external`. Maker-checker and CFO sign-off apply
	// server-side to all of them exactly as they do to Execute.
	const canRecord = $derived(auth.can(PERM_PAYMENT_RECORD_EXTERNAL));
	// Execute: only where FeohLedger sends payments (the server's execution
	// mode), and only as the server's verdict for THIS reader allows — the
	// run's creator used to get an enabled Execute that could only 403.
	const showExecute = $derived(
		canDispatchPayments(executionMode) && isOffered(run?.actions?.execute)
	);
	// A refusal the pending-CFO note above the buttons does not already state.
	const executeRefusal = $derived(
		run?.actions?.execute && run.actions.execute.code !== 'payment_run_awaiting_cfo'
			? run.actions.execute
			: null
	);
	// Retry failed payments: on a run that finished with failures, only where
	// FeohLedger sends payments (the endpoint shares /execute's processor
	// pre-flight), and only as the server's verdict for THIS reader allows. It
	// re-sends money, so it arms before it commits, like Execute.
	const showRetry = $derived(
		canDispatchPayments(executionMode) && isRunRetryOffered(run?.actions?.retry_failed)
	);
	let confirmRetry = $state(false);
	let retrying = $state(false);
	// What the last retry did, kept on screen after the reload: which payments
	// went out again and, for the rest, why not — the reasons are the
	// operator's next step (reconcile, unblock, rebuild the run).
	let retryResult = $state<RetryFailedResult | null>(null);

	async function retryFailed() {
		if (!run || !isAllowed(run.actions?.retry_failed)) return;
		retrying = true;
		try {
			const result = await retryFailedRunPayments(run.id);
			retryResult = result;
			toast(
				m('paymentRuns.runDetail.retryResult', {
					retried: result.payments_retried,
					skipped: result.payments_skipped
				}),
				'success'
			);
			await load();
			onchange?.();
		} catch (err) {
			toast(errorMessage(err, m('paymentRuns.runDetail.retryError')), 'error');
			// The run may have moved under us (someone else retried, a webhook
			// settled it): re-read so the control reflects the server again.
			await load();
		} finally {
			retrying = false;
			// Disarm whichever way it went, as Execute does.
			confirmRetry = false;
		}
	}

	function skipLabel(reason: string): string {
		const key = retrySkipReasonKey(reason);
		return key ? m(key) : reason;
	}

	const showBankFile = $derived(
		auth.can(PERM_PAYMENT_EXECUTE) && canDownloadBankFile(executionMode)
	);

	// The UTC date bounds the effective date (the server refuses one before
	// it); the reader's LOCAL date bounds paid-on (the server allows a day of
	// slack past UTC for exactly that reader). Both refreshed when a form opens.
	let todayUtc = $state(utcToday());
	let todayLocal = $state(localToday());

	let nachaEffectiveDate = $state(nextBusinessDay());
	let nachaSecCode = $state<NachaSecCode>('CCD');
	let downloadingNacha = $state(false);
	let nachaError = $state('');
	// A second file for an exported run is armed: uploading both pays twice.
	let confirmRegenerate = $state(false);

	const nachaDateOk = $derived(
		!!nachaEffectiveDate && nachaEffectiveDate >= todayUtc && isBusinessDay(nachaEffectiveDate)
	);

	async function downloadBankFile(regenerate: boolean) {
		if (!run || !nachaDateOk) return;
		if (regenerate ? run.status !== 'exported' : run.status !== 'draft') return;
		downloadingNacha = true;
		nachaError = '';
		try {
			await downloadRunNacha(run.id, nachaEffectiveDate, nachaSecCode, regenerate);
			if (!regenerate) toast(m('paymentRuns.runDetail.nachaExportedToast'), 'success');
			// The first export moved the run to `exported`; a regenerate bumped
			// its file-ID modifier. Either way the server's view is the truth.
			await load();
			onchange?.();
		} catch (err) {
			// Stated in the panel (role="alert"), beside the button that failed —
			// and only there: one error, one signal (`docs/ui-patterns.md` § Toast).
			nachaError = err instanceof Error ? err.message : m('paymentRuns.runDetail.nachaFailed');
		} finally {
			downloadingNacha = false;
			confirmRegenerate = false;
		}
	}

	// "Bank rejected the file": release an exported run back to draft. An
	// attestation FeohLedger can't check, so the reason is required.
	let rejectOpen = $state(false);
	let rejectReason = $state('');
	let rejecting = $state(false);
	let rejectError = $state('');

	function openReject() {
		confirmRegenerate = false;
		recordOpen = false;
		rejectReason = '';
		rejectError = '';
		rejectOpen = true;
	}

	async function rejectExport() {
		if (!run || run.status !== 'exported') return;
		const reason = rejectReason.trim();
		if (!reason) return;
		rejecting = true;
		rejectError = '';
		try {
			await voidRunNachaExport(run.id, reason);
			toast(m('paymentRuns.runDetail.nachaRejectedToast'), 'success');
			rejectOpen = false;
			await load();
			onchange?.();
		} catch (err) {
			// In the form (role="alert"), not also a toast: one error, one signal.
			rejectError = err instanceof Error ? err.message : m('paymentRuns.runDetail.nachaRejectFailed');
		} finally {
			rejecting = false;
		}
	}

	let recordOpen = $state(false);
	let recordReference = $state('');
	let recordPaidOn = $state(localToday());
	let recordingRun = $state(false);
	let recordError = $state('');

	function openRecord() {
		// Opening the record form retracts every armed commit: only one is ever
		// one click away.
		confirmExecute = false;
		confirmCancel = false;
		confirmRegenerate = false;
		rejectOpen = false;
		todayLocal = localToday();
		recordPaidOn = todayLocal;
		recordReference = '';
		recordError = '';
		recordOpen = true;
	}

	async function recordRunPaid() {
		if (!run || (run.status !== 'draft' && run.status !== 'exported')) return;
		const reference = recordReference.trim();
		if (!reference || !recordPaidOn) return;
		recordingRun = true;
		recordError = '';
		try {
			const result = await recordRunOutside(run.id, { reference, paid_on: recordPaidOn });
			toast(m('paymentRuns.runDetail.recordedToast', { n: result.payment_count }), 'success');
			recordOpen = false;
			await load();
			onchange?.();
		} catch (err) {
			// A coded refusal names the invoice that stopped the WHOLE run
			// (nothing was recorded), so it stays on screen beside the form — and
			// only there: one error, one signal.
			recordError = err instanceof Error ? err.message : m('paymentRuns.runDetail.recordFailed');
		} finally {
			recordingRun = false;
		}
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
		if (!run || !isAllowed(run.actions?.execute)) return;
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
		if (!run || !isAllowed(run.actions?.approve)) return;
		approving = true;
		try {
			await api.post(`/api/payments/runs/${runId}/approve`, {});
			toast(m('paymentRuns.runDetail.approvedToast'), 'success');
			await load();
			onchange?.();
		} catch (err) {
			toast(errorMessage(err, m('paymentRuns.runDetail.approveFailed')), 'error');
		} finally {
			approving = false;
		}
	}

	async function cancelDraft() {
		if (!run || !isAllowed(run.actions?.cancel)) return;
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
			// Disarm whichever way it went, as Execute does: a failed cancel must
			// not leave a destructive control one click from firing.
			confirmCancel = false;
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
	<div use:focusTrap={{ onEscape: onclose }} class="modal" role="dialog" aria-modal="true" aria-label={m('paymentRuns.runDetail.dialogLabel')} tabindex="-1">
		<header>
			<div class="title-block">
				<h2>{m('paymentRuns.runDetail.title')}</h2>
				<HelpTip term="payment-run" />
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
					{#if run.discount_total && isPositiveAmount(run.discount_total)}
						<!-- Already deducted from the total above: what the accepted
						     early-payment offers save, so the CFO signing the run sees why
						     it is below the invoices it pays. -->
						<dt>{m('paymentRuns.runDetail.discountTotal')}</dt>
						<dd data-testid="run-discount-total">{fmt(run.discount_total, run.currency)}</dd>
					{/if}
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
							<th>{m('paymentRuns.runDetail.colMethod')} <HelpTip term="payment-rail" /></th>
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
									{#if p.discount_amount}
										<div class="muted" data-testid="run-payment-discount">
											{m('paymentRuns.runDetail.rowDiscount', { amount: fmt(p.discount_amount, p.currency) })}
										</div>
									{/if}
								</td>
								<td>
									{methodLabel(p.method)}
									{#if isExternalPayment(p)}
										<div class="muted" data-testid="run-payment-external">
											{m('payments.provider.external')}
										</div>
									{/if}
								</td>
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

				{#if run.status === 'draft' || run.status === 'exported'}
					{@const pendingCfo = run.requires_cfo_approval && !run.cfo_approved_at}
					{@const exported = run.status === 'exported'}
					{#if showBankFile}
						<section class="outside-panel" aria-labelledby="run-nacha-heading" data-testid="run-nacha-panel">
							<div class="panel-title-row">
								<h3 id="run-nacha-heading">{m('paymentRuns.runDetail.nachaHeading')}</h3>
								<HelpTip term="nacha-file" />
							</div>
							<p class="panel-hint">{m('paymentRuns.runDetail.nachaHint')}</p>
							{#if !exported}
								<p class="panel-hint">{m('paymentRuns.runDetail.nachaClaimNote')}</p>
							{/if}
							{#if !exported || confirmRegenerate}
								<div class="panel-fields">
									<label>
										<span>{m('paymentRuns.runDetail.nachaEffectiveDate')}</span>
										<input
											type="date"
											bind:value={nachaEffectiveDate}
											min={todayUtc}
											required
											aria-invalid={!nachaDateOk}
											aria-describedby={nachaDateOk ? undefined : 'run-nacha-date-error'}
											data-testid="nacha-effective-date"
										/>
									</label>
									<label>
										<span>{m('paymentRuns.runDetail.nachaSecCode')}</span>
										<select bind:value={nachaSecCode} data-testid="nacha-sec-code">
											{#each NACHA_SEC_CODES as code (code)}
												<option value={code}>
													{code === 'CCD'
														? m('paymentRuns.runDetail.nachaSecCcd')
														: m('paymentRuns.runDetail.nachaSecPpd')}
												</option>
											{/each}
										</select>
									</label>
								</div>
								{#if !nachaDateOk}
									<p class="panel-hint warn" id="run-nacha-date-error">
										{m('paymentRuns.runDetail.nachaNotBusinessDay')}
									</p>
								{/if}
							{/if}
							{#if confirmRegenerate}
								<!-- Announced the moment Regenerate arms: the next click
								     produces a SECOND file for money already on its way. -->
								<p class="panel-error" role="alert" data-testid="nacha-regenerate-warning">
									{m('paymentRuns.runDetail.nachaRegenerateWarning')}
								</p>
							{/if}
							{#if nachaError}
								<p class="panel-error" role="alert" data-testid="nacha-error">{nachaError}</p>
							{/if}
							{#if rejectOpen}
								<form class="reject-form" onsubmit={(e) => { e.preventDefault(); rejectExport(); }}>
									<p class="panel-hint">{m('paymentRuns.runDetail.nachaRejectIntro')}</p>
									<label>
										<span>{m('paymentRuns.runDetail.nachaRejectReason')}</span>
										<input
											type="text"
											bind:value={rejectReason}
											maxlength="500"
											required
											data-testid="nacha-reject-reason"
										/>
									</label>
									{#if rejectError}
										<p class="panel-error" role="alert" data-testid="nacha-reject-error">{rejectError}</p>
									{/if}
									<div class="panel-actions">
										<button type="button" class="btn-cancel" onclick={() => (rejectOpen = false)}>
											{m('common.cancel')}
										</button>
										<button
											type="submit"
											class="btn-discard armed"
											disabled={rejecting || !rejectReason.trim()}
											data-testid="nacha-reject-confirm"
										>
											{rejecting
												? m('paymentRuns.runDetail.nachaRejecting')
												: m('paymentRuns.runDetail.nachaRejectConfirm')}
										</button>
									</div>
								</form>
							{:else}
								<div class="panel-actions">
									{#if !exported}
										<button
											type="button"
											class="btn-secondary"
											disabled={downloadingNacha || pendingCfo || !nachaDateOk}
											title={pendingCfo ? m('paymentRuns.runDetail.awaitingCfo') : ''}
											onclick={() => downloadBankFile(false)}
										>
											{downloadingNacha
												? m('paymentRuns.runDetail.nachaDownloading')
												: m('paymentRuns.runDetail.nachaDownload')}
										</button>
									{:else}
										<button
											type="button"
											class="btn-discard"
											disabled={downloadingNacha}
											onclick={openReject}
										>
											{m('paymentRuns.runDetail.nachaReject')}
										</button>
										{#if confirmRegenerate}
											<button
												type="button"
												class="btn-discard armed"
												disabled={downloadingNacha || !nachaDateOk}
												onclick={() => downloadBankFile(true)}
											>
												{downloadingNacha
													? m('paymentRuns.runDetail.nachaDownloading')
													: m('paymentRuns.runDetail.nachaConfirmRegenerate')}
											</button>
										{:else}
											<button
												type="button"
												class="btn-secondary"
												disabled={downloadingNacha}
												onclick={() => {
													recordOpen = false;
													confirmRegenerate = true;
												}}
											>
												{m('paymentRuns.runDetail.nachaRegenerate')}
											</button>
										{/if}
									{/if}
								</div>
							{/if}
						</section>
					{/if}

					{#if canRecord}
						<section class="outside-panel" aria-labelledby="run-record-heading" data-testid="run-record-panel">
							<div class="panel-title-row">
								<h3 id="run-record-heading">{m('paymentRuns.runDetail.recordHeading')}</h3>
								<HelpTip term="record-only" />
							</div>
							{#if recordOpen}
								<p class="panel-hint">{m('paymentRuns.runDetail.recordIntro')}</p>
								<form onsubmit={(e) => { e.preventDefault(); recordRunPaid(); }}>
									<div class="panel-fields">
										<label>
											<span>{m('paymentRuns.runDetail.recordReference')}</span>
											<input
												type="text"
												bind:value={recordReference}
												maxlength="255"
												required
												aria-describedby="run-record-reference-hint"
												data-testid="run-record-reference"
											/>
										</label>
										<label>
											<span>{m('paymentRuns.runDetail.recordPaidOn')}</span>
											<input
												type="date"
												bind:value={recordPaidOn}
												max={todayLocal}
												required
												data-testid="run-record-paid-on"
											/>
										</label>
									</div>
									<p class="panel-hint" id="run-record-reference-hint">
										{m('paymentRuns.runDetail.recordReferenceHint')}
									</p>
									{#if recordError}
										<p class="panel-error" role="alert" data-testid="run-record-error">{recordError}</p>
									{/if}
									<div class="panel-actions">
										<button type="button" class="btn-cancel" onclick={() => (recordOpen = false)}>
											{m('common.cancel')}
										</button>
										<button
											type="submit"
											class="btn-approve"
											disabled={recordingRun ||
												pendingCfo ||
												!recordReference.trim() ||
												!recordPaidOn ||
												recordPaidOn > todayLocal}
											title={pendingCfo ? m('paymentRuns.runDetail.awaitingCfo') : ''}
											data-testid="run-record-confirm"
										>
											{recordingRun
												? m('paymentRuns.runDetail.recordSubmitting')
												: m('paymentRuns.runDetail.recordSubmit', {
														amount: fmt(run.total_amount, run.currency)
													})}
										</button>
									</div>
								</form>
							{:else}
								<div class="panel-actions">
									<!-- Once the file is out, recording the run is THE next step,
									     so it becomes the primary control. -->
									<button
										type="button"
										class={exported ? 'btn-approve' : 'btn-secondary'}
										disabled={executing || cancelling || downloadingNacha || rejecting}
										onclick={openRecord}
									>
										{m('paymentRuns.runDetail.recordOpen')}
									</button>
								</div>
							{/if}
						</section>
					{/if}
				{/if}
			{/if}
		</div>

		<footer>
			{#if run?.status === 'draft'}
				{@const pendingCfo = run.requires_cfo_approval && !run.cfo_approved_at}
				{@const cfoApproved = run.requires_cfo_approval && !!run.cfo_approved_at}

				{#if pendingCfo}
					<p class="footer-note pending">
						<strong>{m('paymentRuns.runDetail.pendingCfoStrong')}</strong> {m('paymentRuns.runDetail.pendingCfoBody')}
						<HelpTip term="cfo-gate" />
					</p>
				{:else if cfoApproved}
					<p class="footer-note approved">
						{m('paymentRuns.runDetail.cfoApprovedNote', { date: fmtDate(run.cfo_approved_at) })}
					</p>
				{:else if executionMode === 'record_only'}
					<p class="footer-note" data-testid="run-record-only-note">
						{m('paymentRuns.runDetail.recordOnlyNote')}
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
					{:else if isOffered(run.actions?.cancel)}
						<button
							class="btn-discard"
							disabled={cancelling || executing || approving || !isAllowed(run.actions?.cancel)}
							onclick={() => arm('cancel')}
						>
							{m('paymentRuns.runDetail.cancelRun')}
						</button>
					{/if}
					<!-- The server's verdict (`actions.approve`), never a role check
					     here: the sign-off is `require_roles(ROLE_CFO)` (an admin is
					     not a CFO — a sign-off one can grant themselves is not a
					     sign-off), and maker-checker refuses the CFO who CREATED the
					     run. That CFO used to see an enabled "Approve as CFO" that
					     could only 403; they now see it disabled, with the reason.
					     `pendingCfo` decides only whether the run HAS a CFO step to
					     render a control for — a run that needs no sign-off shows no
					     disabled "Approve as CFO"; whether it may be pressed is the
					     verdict alone. -->
					{#if pendingCfo && isOffered(run.actions?.approve)}
						<button
							class="btn-approve"
							disabled={approving || !isAllowed(run.actions?.approve)}
							aria-describedby={run.actions?.approve ? 'run-approve-refusal' : undefined}
							onclick={approveCfo}
						>
							{approving ? m('paymentRuns.runDetail.approving') : m('paymentRuns.runDetail.approveAsCfo')}
						</button>
					{/if}
					<!-- Execute only where FeohLedger actually sends payments. On a
					     record-only tenant (or before the mode is known — fail
					     closed) it is not rendered at all: the server would refuse it
					     with `payments_record_only`, and the record path above is the
					     way to `paid`. -->
					{#if showExecute}
						{#if confirmExecute}
							<!-- Armed. The label is deliberately DISTINCT from the
							     unarmed one ("Confirm execute · …" vs "Execute · …")
							     so the change is announced to a screen reader, and so
							     a test can wait on the armed control by name instead
							     of on a timer. -->
							<button
								class="btn-execute armed"
								disabled={executing || cancelling || !isAllowed(run.actions?.execute)}
								aria-describedby={executeRefusal ? 'run-execute-refusal' : undefined}
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
								disabled={executing || cancelling || !isAllowed(run.actions?.execute) || recordingRun}
								aria-describedby={executeRefusal ? 'run-execute-refusal' : undefined}
								title={pendingCfo ? m('paymentRuns.runDetail.awaitingCfo') : ''}
								onclick={() => {
									recordOpen = false;
									arm('execute');
								}}
							>
								{m('paymentRuns.runDetail.executeAmount', {
									amount: fmt(run.total_amount, run.currency)
								})}
							</button>
						{/if}
					{/if}
				</div>
				<!-- Why a control above is disabled, in the server's own words
				     (localized from its code) — e.g. a colleague must sign off or
				     execute a run this reader created. -->
				{#if pendingCfo && isOffered(run.actions?.approve) && run.actions?.approve}
					<p class="footer-note" id="run-approve-refusal" data-testid="run-approve-refusal">
						{refusalText(run.actions.approve, m)}
					</p>
				{/if}
				{#if showExecute && executeRefusal}
					<p class="footer-note" id="run-execute-refusal" data-testid="run-execute-refusal">
						{refusalText(executeRefusal, m)}
					</p>
				{/if}
			{:else if run?.status === 'exported'}
				<!-- Claimed by its bank file: no Execute, no Cancel run. The two
				     exits — record as paid, or the bank rejected the file — are
				     in the panels above. -->
				<p class="footer-note" data-testid="run-exported-note">
					{m('paymentRuns.runDetail.exportedNote')}
				</p>
				<div class="actions">
					<button class="btn-cancel" onclick={onclose}>{m('paymentRuns.runDetail.close')}</button>
				</div>
			{:else}
				{#if retryResult}
					<div class="footer-note" role="status" data-testid="run-retry-result">
						<p>
							{m('paymentRuns.runDetail.retryResult', {
								retried: retryResult.payments_retried,
								skipped: retryResult.payments_skipped
							})}
						</p>
						{#if retryResult.skip_reasons.length > 0}
							<p>{m('paymentRuns.runDetail.retrySkippedHeading')}</p>
							<ul class="retry-skips">
								{#each retryResult.skip_reasons as reason (reason)}
									<li>{skipLabel(reason)}</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}
				{#if showRetry && confirmRetry}
					<!-- Announced the moment Retry arms: the next click sends money. -->
					<p class="footer-note armed-note" role="alert" data-testid="retry-armed-note">
						{m('paymentRuns.runDetail.retryArmedNote', { count: run?.retryable_failures ?? 0 })}
					</p>
				{/if}
				<div class="actions">
					<button class="btn-cancel" onclick={onclose}>{m('paymentRuns.runDetail.close')}</button>
					<!-- The server's verdict (`actions.retry_failed`), never a status
					     or role check here. Disabled with the reason when the reader
					     can act on it (a colleague must retry a run this reader
					     created; CFO sign-off is pending; every failure must be
					     reconciled first); hidden for a role refusal or a run that did
					     not finish with failures. -->
					{#if showRetry}
						{#if confirmRetry}
							<button
								class="btn-execute armed"
								disabled={retrying || !isAllowed(run?.actions?.retry_failed)}
								onclick={retryFailed}
							>
								{retrying
									? m('paymentRuns.runDetail.retrying')
									: m('paymentRuns.runDetail.confirmRetryFailed')}
							</button>
						{:else}
							<button
								class="btn-execute"
								disabled={retrying || !isAllowed(run?.actions?.retry_failed)}
								aria-describedby={run?.actions?.retry_failed ? 'run-retry-refusal' : undefined}
								onclick={() => (confirmRetry = true)}
							>
								{m('paymentRuns.runDetail.retryFailed')}
							</button>
						{/if}
					{/if}
				</div>
				{#if showRetry && run?.actions?.retry_failed}
					<p class="footer-note" id="run-retry-refusal" data-testid="run-retry-refusal">
						{refusalText(run.actions.retry_failed, m)}
					</p>
				{/if}
			{/if}
		</footer>
	</div>
</div>

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		background: var(--overlay);
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
	   `RUN_STATUS_TONES` in `#lib/types/payment` — shared with `/payments`,
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

	.footer-note p {
		margin: 0;
	}

	.retry-skips {
		margin: 4px 0 0;
		padding-left: 18px;
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
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger) 30%, transparent);
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
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		color: var(--danger);
	}

	.btn-discard:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* --- Paying the run outside FeohLedger --- */
	.outside-panel {
		margin-top: 18px;
		padding-top: 14px;
		border-top: 1px solid var(--border);
	}

	.panel-title-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	h3 {
		margin: 0;
		font-size: 0.95rem;
		font-weight: 600;
	}

	.panel-hint {
		margin: 6px 0 10px;
		font-size: 0.8rem;
		color: var(--text-muted);
		line-height: 1.4;
	}

	.panel-fields {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
	}

	.panel-fields label {
		flex: 1 1 200px;
		min-width: 0;
	}

	.panel-hint.warn {
		color: var(--danger);
	}

	.reject-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin-top: 8px;
	}

	.panel-error {
		margin: 8px 0 0;
		padding: 8px 10px;
		border: 1px solid var(--danger);
		border-radius: 4px;
		background: var(--danger-tint);
		color: var(--text);
		font-size: 0.82rem;
		line-height: 1.4;
	}

	.panel-actions {
		display: flex;
		justify-content: flex-end;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 10px;
	}

	.btn-secondary {
		padding: 8px 16px;
		border-radius: 4px;
		/* --accent for text on the surface (--accent-strong is a FILL token only,
		   and fails 4.5:1 as text on --surface — app.css). */
		border: 1px solid var(--accent);
		background: var(--surface);
		color: var(--accent);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--bg);
	}

	.btn-secondary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.footer-note.pending {
		color: var(--warning);
	}

	.footer-note.approved {
		color: var(--success);
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
