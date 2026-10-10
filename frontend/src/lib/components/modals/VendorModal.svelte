<script lang="ts">
	// Vendor detail modal — "Screening & Risk" panel for the Sanctions & Vendor
	// Risk Screening feature. Shows the current screening status, last-screened
	// time, payment-block state + reason, risk level/score, and the screening
	// history timeline. Mutating actions (re-screen, recompute risk, block,
	// unblock) default to admin / ap_manager and emit the updated vendor (or
	// refreshed risk) back to the parent list.
	import { auth } from '#lib/stores/auth.svelte.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { PERM_VENDOR_BLOCK, PERM_VENDOR_MANAGE } from '#lib/types/admin.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import ScreeningBadge from '#lib/components/ui/ScreeningBadge.svelte';
	import {
		screenVendor,
		getScreeningHistory,
		blockVendor,
		cancelVendorCards,
		unblockVendor,
		recomputeVendorRisk,
		enrichVendor,
		applyVendorEnrichment
	} from '#lib/api/vendors.ts';
	import {
		riskLevelLabelKey,
		ENRICHABLE_FIELD_LABEL_KEYS,
		type Vendor,
		type SanctionsCheck,
		type ScreeningStatus,
		type EnrichmentFieldSuggestion
	} from '#lib/types/vendor.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { toastCardRevocations } from '#lib/utils/cardRevocationToast.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { getVendorScore, type VendorScoreResponse } from '#lib/api/enrichment.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';

	const SCREENING_RESULTS: ScreeningStatus[] = ['unscreened', 'clear', 'review', 'match'];

	// A history `result` may be a recognised screening status (render as a badge)
	// or a free-form provider string (render as plain text).
	function asScreeningStatus(result: string): ScreeningStatus | undefined {
		return (SCREENING_RESULTS as string[]).includes(result)
			? (result as ScreeningStatus)
			: undefined;
	}

	let {
		vendor,
		onclose,
		onupdated
	}: {
		vendor: Vendor;
		onclose: () => void;
		onupdated: (v: Vendor) => void;
	} = $props();

	// Re-screen (`POST /vendors/{id}/screen`) moved to the granular
	// `vendor.manage` permission — same duty as create/edit/verify/reject.
	// Recompute-risk (`POST /vendors/{id}/risk/recompute`) is a DIFFERENT
	// backend route that stayed on plain `require_roles(ADMIN, AP_MANAGER)`
	// (it's not vendor create/edit/verify/reject, just a scoring refresh), so
	// it keeps the role check rather than sharing `vendor.manage` — a custom
	// role holding only `vendor.manage` sees Re-screen but not Recompute Risk.
	const canReScreen = $derived(auth.can(PERM_VENDOR_MANAGE));
	const canRecomputeRisk = $derived(auth.isManager); // admin | ap_manager
	// Block/unblock moved to the granular permission so an org can split it from
	// the rest of vendor management. Defaults to admin/ap_manager (unchanged).
	const canBlock = $derived(auth.can(PERM_VENDOR_BLOCK));
	// External enrichment + apply is admin | ap_manager | cfo (backend
	// `_ENRICH_ROLES`). isManager = admin|ap_manager, isCfo = admin|cfo.
	const canEnrich = $derived(auth.isManager || auth.isCfo);

	// The performance score is gated admin / ap_manager / cfo on the backend
	// (`_SCORE_ROLES`) — the same audience as enrichment, clerk excluded.
	const canSeeScore = $derived(auth.isManager || auth.isCfo);

	// The risk level is a message key, not an English literal — a level this
	// build doesn't know renders raw rather than blank.
	const riskLabel = (level: string) => {
		const key = riskLevelLabelKey(level);
		return key ? m(key) : level;
	};

	let history = $state<SanctionsCheck[]>([]);
	let loadingHistory = $state(true);
	let busy = $state(''); // which action is in flight ('' = none)

	// --- Performance score (advisory, compute-on-read) ------------------------
	// `GET /api/enrichment/vendors/{id}/score`. Nothing is persisted and the
	// read changes nothing, so it loads on open like the screening history
	// rather than sitting behind a button. `scoreError` is a distinct state from
	// "no data": a failed read must not render as a vendor with no history.
	let score = $state<VendorScoreResponse | null>(null);
	let loadingScore = $state(true);
	let scoreError = $state(false);

	/** Localized label per sub-score. An unrecognised name (a sub-score added
	 *  backend-side before this map catches up) falls back to the raw key rather
	 *  than rendering blank. */
	const SUB_SCORE_LABEL_KEYS: Record<string, MessageKey> = {
		accuracy: 'vendors.modal.score.subAccuracy',
		dispute: 'vendors.modal.score.subDispute',
		on_time: 'vendors.modal.score.subOnTime'
	};
	function subScoreLabel(name: string): string {
		const key = SUB_SCORE_LABEL_KEYS[name];
		return key ? m(key) : name;
	}

	$effect(() => {
		const id = vendor.id;
		if (!canSeeScore) return;
		loadingScore = true;
		scoreError = false;
		getVendorScore(id)
			.then((res) => {
				score = res;
			})
			.catch(() => {
				score = null;
				scoreError = true;
			})
			.finally(() => {
				loadingScore = false;
			});
	});

	// Load history whenever the open vendor changes.
	$effect(() => {
		const id = vendor.id;
		loadingHistory = true;
		getScreeningHistory(id)
			.then((rows) => {
				history = rows;
			})
			.catch(() => {
				toast(m('vendors.modal.historyLoadFailed'), 'error');
			})
			.finally(() => {
				loadingHistory = false;
			});
	});

	function fmt(iso: string | null): string {
		return formatDate(iso, '—', {
			month: 'short',
			day: 'numeric',
			year: 'numeric',
			hour: 'numeric',
			minute: '2-digit'
		});
	}

	function errMsg(err: unknown, fallback: string): string {
		const e = err as { detail?: string; message?: string } | null;
		return e?.detail ?? e?.message ?? fallback;
	}

	async function reScreen() {
		busy = 'screen';
		try {
			const updated = await screenVendor(vendor.id);
			onupdated(updated);
			history = await getScreeningHistory(vendor.id);
			toast(m('vendors.modal.rescreenedToast'), 'success');
			toastCardRevocations([updated.card_revocation]);
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.screeningFailed')), 'error');
		} finally {
			busy = '';
		}
	}

	async function recompute() {
		busy = 'risk';
		try {
			const risk = await recomputeVendorRisk(vendor.id);
			onupdated({ ...vendor, risk_score: risk.risk_score, risk_level: risk.risk_level });
			toast(m('vendors.modal.riskRecomputedToast'), 'success');
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.recomputeFailed')), 'error');
		} finally {
			busy = '';
		}
	}

	async function toggleBlock() {
		busy = 'block';
		try {
			const updated = vendor.payments_blocked
				? await unblockVendor(vendor.id)
				: await blockVendor(vendor.id, 'Blocked from vendor screening review');
			onupdated(updated);
			toast(updated.payments_blocked ? m('vendors.modal.paymentsBlockedToast') : m('vendors.modal.paymentsUnblockedToast'), 'success');
			toastCardRevocations([updated.card_revocation]);
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.updateFailed')), 'error');
		} finally {
			busy = '';
		}
	}

	// A vendor that can no longer be paid should hold no spendable card. The
	// status write cancels them itself; this is the retry for a card the
	// provider did not confirm closed (`POST /api/vendors/{id}/cancel-cards`).
	const canCancelCards = $derived(
		(canReScreen || canBlock) && (vendor.status !== 'active' || vendor.payments_blocked)
	);

	async function cancelLiveCards() {
		busy = 'cards';
		try {
			const rev = await cancelVendorCards(vendor.id);
			toast(m('vendors.cards.cancelled', { n: rev.cancelled }), 'success');
			toastCardRevocations([rev]);
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.cancelCardsFailed')), 'error');
		} finally {
			busy = '';
		}
	}

	// --- External enrichment (advisory firmographics + steward apply) ---------
	// `enrichRun` holds the last enrich result; `selected` is the steward's
	// per-field pick (defaults to all suggested fields checked). `enriched`
	// tracks that an enrich call has returned (so we can show an explicit
	// "no suggestions" empty state distinct from "not yet run").
	let enrichSuggestions = $state<EnrichmentFieldSuggestion[]>([]);
	let enriched = $state(false);
	let selected = $state<Set<string>>(new Set());

	function truncate(v: string | null): string {
		if (!v) return '—';
		return v.length > 80 ? v.slice(0, 79) + '…' : v;
	}

	async function runEnrich() {
		busy = 'enrich';
		try {
			const res = await enrichVendor(vendor.id);
			enrichSuggestions = res.suggestions;
			// Pre-check every suggested field — the steward unchecks what they
			// don't want. Nothing is applied until they hit "Apply selected".
			selected = new Set(res.suggestions.map((s) => s.field));
			enriched = true;
			if (!res.firmographics.matched) {
				toast(m('vendors.modal.enrichNoMatch'), 'info');
			} else if (res.suggestions.length === 0) {
				toast(m('vendors.modal.enrichAlreadyMatches'), 'info');
			}
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.enrichmentFailed')), 'error');
		} finally {
			busy = '';
		}
	}

	function toggleField(field: string) {
		const next = new Set(selected);
		if (next.has(field)) next.delete(field);
		else next.add(field);
		selected = next;
	}

	async function applySelected() {
		const fields = enrichSuggestions
			.filter((s) => selected.has(s.field))
			.map((s) => ({ field: s.field, value: s.suggested_value }));
		if (fields.length === 0) return;
		busy = 'apply';
		try {
			const res = await applyVendorEnrichment(vendor.id, fields);
			onupdated(res.vendor);
			const count = Object.keys(res.applied).length;
			toast(
				count > 0
					? m('vendors.modal.appliedFields', { count })
					: m('vendors.modal.noChangesToApply'),
				'success'
			);
			// Clear the diff — the applied values are now the vendor's current
			// values, so the previous suggestions are stale.
			enrichSuggestions = [];
			selected = new Set();
			enriched = false;
		} catch (err) {
			toast(errMsg(err, m('vendors.modal.applyFailed')), 'error');
		} finally {
			busy = '';
		}
	}
</script>

<Modal open ariaLabel="Vendor screening and risk" width="lg" {onclose}>
	<h2>{vendor.name}</h2>

	<section class="panel">
		<h3>{m('vendors.modal.screeningRiskHeading')}</h3>

		<div class="kv-grid">
			<div class="kv">
				<span class="kv-label">{m('vendors.modal.screeningStatus')} <HelpTip term="sanctions-screening" /></span>
				<span class="kv-value">
					<ScreeningBadge screening={vendor.screening_status} blocked={vendor.payments_blocked} />
				</span>
			</div>
			<div class="kv">
				<span class="kv-label">{m('vendors.modal.lastScreened')}</span>
				<span class="kv-value">{fmt(vendor.last_screened_at)}</span>
			</div>
			<div class="kv">
				<span class="kv-label">{m('vendors.modal.riskLevel')} <HelpTip term="vendor-risk" /></span>
				<span class="kv-value">
					{riskLabel(vendor.risk_level)}{#if vendor.risk_score}
						<span class="muted"> · {vendor.risk_score}</span>
					{/if}
				</span>
			</div>
			<div class="kv">
				<span class="kv-label">{m('vendors.modal.payments')}</span>
				<span class="kv-value">
					{#if vendor.payments_blocked}
						<span class="blocked-text">{m('vendors.modal.blocked')}</span>
						{#if vendor.payments_blocked_reason}
							<span class="muted"> — {vendor.payments_blocked_reason}</span>
						{/if}
					{:else}
						{m('vendors.modal.allowed')}
					{/if}
				</span>
			</div>
		</div>

		<div class="actions-row">
			{#if canReScreen}
				<RowAction onclick={reScreen} disabled={busy !== ''}>
					{busy === 'screen' ? m('vendors.modal.screening') : m('vendors.modal.rescreenNow')}
				</RowAction>
			{/if}
			{#if canRecomputeRisk}
				<RowAction onclick={recompute} disabled={busy !== ''}>
					{busy === 'risk' ? m('vendors.modal.recomputing') : m('vendors.modal.recomputeRisk')}
				</RowAction>
			{/if}
			{#if canBlock}
				<RowAction
					variant="danger"
					onclick={toggleBlock}
					disabled={busy !== ''}
				>
					{#if busy === 'block'}
						{m('vendors.modal.working')}
					{:else}
						{vendor.payments_blocked ? m('vendors.modal.unblockPayments') : m('vendors.modal.blockPayments')}
					{/if}
				</RowAction>
			{/if}
			{#if canCancelCards}
				<RowAction variant="danger" onclick={cancelLiveCards} disabled={busy !== ''}>
					{busy === 'cards' ? m('vendors.modal.working') : m('vendors.modal.cancelLiveCards')}
				</RowAction>
			{/if}
			{#if !canReScreen && !canRecomputeRisk && !canBlock}
				<span class="muted">{m('vendors.modal.noPermission')}</span>
			{/if}
		</div>
	</section>

	{#if canSeeScore}
		<section class="panel" data-testid="vendor-score">
			<h3>{m('vendors.modal.score.heading')}</h3>
			<p class="hint muted">{m('vendors.modal.score.hint')}</p>

			{#if loadingScore}
				<p class="muted">{m('common.loading')}</p>
			{:else if scoreError}
				<!-- A failed read is its own state. Falling through to the
				     "no history yet" copy below would report a vendor with a
				     clean record when we simply could not compute one. -->
				<p class="muted" data-testid="vendor-score-error">{m('vendors.modal.score.loadFailed')}</p>
			{:else if score}
				<div class="score-headline">
					<span class="score-composite" data-testid="vendor-score-composite">
						{score.composite ?? m('vendors.modal.score.notAvailable')}
					</span>
					<span class="score-composite-label">
						{score.composite
							? m('vendors.modal.score.compositeLabel')
							: m('vendors.modal.score.noHistory')}
					</span>
				</div>

				<!-- The inputs, not just the number: a score attached to a
				     business relationship that cannot be explained is worse than
				     no score. Each row carries the sample it was computed over
				     and the backend's own evidence sentence. -->
				<table class="score-table">
					<thead>
						<tr>
							<th scope="col">{m('vendors.modal.score.colSignal')}</th>
							<th scope="col" class="score-num">{m('vendors.modal.score.colScore')}</th>
							<th scope="col" class="score-num">{m('vendors.modal.score.colSample')}</th>
							<th scope="col">{m('vendors.modal.score.colBasis')}</th>
						</tr>
					</thead>
					<tbody>
						{#each score.sub_scores as s (s.name)}
							<tr data-testid="vendor-score-row-{s.name}">
								<th scope="row" class="score-signal">{subScoreLabel(s.name)}</th>
								<td class="score-num">
									{#if s.score === null}
										<span class="muted">{m('vendors.modal.score.notAvailable')}</span>
									{:else}
										{s.score}
									{/if}
								</td>
								<td class="score-num muted">{s.sample_size}</td>
								<!-- Plain-text binding, never {@html}. The detail is the
								     backend's own PII-free sentence (counts only). -->
								<td class="score-basis muted">{s.detail}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</section>
	{/if}

	{#if canEnrich}
		<section class="panel">
			<h3>{m('vendors.modal.externalEnrichment')}</h3>
			<p class="hint muted">
				{m('vendors.modal.enrichmentHint')}
			</p>

			<div class="actions-row">
				<RowAction onclick={runEnrich} disabled={busy !== ''}>
					{busy === 'enrich' ? m('vendors.modal.lookingUp') : m('vendors.modal.enrichFromSource')}
				</RowAction>
			</div>

			{#if enriched}
				{#if enrichSuggestions.length === 0}
					<p class="muted enrich-empty">{m('vendors.modal.noSuggestedChanges')}</p>
				{:else}
					<table class="enrich-diff">
						<thead>
							<tr>
								<th class="enrich-pick"><span class="visually-hidden">{m('vendors.modal.apply')}</span></th>
								<th>{m('vendors.modal.colField')}</th>
								<th>{m('vendors.modal.colCurrent')}</th>
								<th>{m('vendors.modal.colSuggested')}</th>
							</tr>
						</thead>
						<tbody>
							{#each enrichSuggestions as s (s.field)}
								<tr>
									<td class="enrich-pick">
										<input
											type="checkbox"
											checked={selected.has(s.field)}
											onchange={() => toggleField(s.field)}
											aria-label={m('vendors.modal.applyFieldAria', {
												field: m(ENRICHABLE_FIELD_LABEL_KEYS[s.field])
											})}
										/>
									</td>
									<td>{m(ENRICHABLE_FIELD_LABEL_KEYS[s.field])}</td>
									<td class="enrich-current muted" title={s.current_value ?? ''}>
										{truncate(s.current_value)}
									</td>
									<td class="enrich-suggested" title={s.suggested_value ?? ''}>
										{truncate(s.suggested_value)}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>

					<div class="actions-row">
						<RowAction onclick={applySelected} disabled={busy !== '' || selected.size === 0}>
							{busy === 'apply' ? m('vendors.modal.applying') : m('vendors.modal.applySelected', { count: selected.size })}
						</RowAction>
					</div>
				{/if}
			{/if}
		</section>
	{/if}

	<section class="panel">
		<h3>{m('vendors.modal.screeningHistory')}</h3>
		{#if loadingHistory}
			<p class="muted">{m('common.loading')}</p>
		{:else if history.length === 0}
			<p class="muted">{m('vendors.modal.noChecks')}</p>
		{:else}
			<ul class="timeline">
				{#each history as check (check.id)}
					{@const screened = asScreeningStatus(check.result)}
					<li class="timeline-item">
						<span class="timeline-when">{fmt(check.checked_at)}</span>
						<span class="timeline-body">
							{#if screened}
								<ScreeningBadge screening={screened} />
							{:else}
								<span class="result-text">{check.result}</span>
							{/if}
							<span class="muted">{check.provider} · {check.check_type}</span>
							{#if check.matched_list}
								<span class="matched-list">{m('vendors.modal.matchedLabel', { list: check.matched_list })}</span>
							{/if}
							{#if check.risk_score}
								<span class="muted">{m('vendors.modal.scoreLabel', { score: check.risk_score })}</span>
							{/if}
						</span>
					</li>
				{/each}
			</ul>
		{/if}
	</section>

	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('vendors.modal.close')}</button>
	</div>
</Modal>

<style>
	.panel {
		margin-top: 16px;
	}
	.panel h3 {
		font-size: 0.85rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin: 0 0 10px;
	}
	.kv-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px 24px;
	}
	.kv {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.kv-label {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.kv-value {
		font-size: 0.9rem;
	}
	.muted {
		color: var(--text-muted);
	}
	.blocked-text {
		color: var(--danger);
		font-weight: 500;
	}
	.actions-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		margin-top: 14px;
	}
	.hint {
		font-size: 0.8rem;
		line-height: 1.4;
		margin: 0;
	}
	.score-headline {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-top: 4px;
	}
	.score-composite {
		font-size: 1.6rem;
		font-weight: 600;
		line-height: 1;
	}
	.score-composite-label {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.score-table {
		width: 100%;
		border-collapse: collapse;
		margin-top: 12px;
		font-size: 0.82rem;
	}
	.score-table th[scope='col'] {
		text-align: left;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 4px 10px 6px;
		border-bottom: 1px solid var(--border);
	}
	.score-table td,
	.score-table th[scope='row'] {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
		text-align: left;
	}
	.score-table tr:last-child td,
	.score-table tr:last-child th[scope='row'] {
		border-bottom: none;
	}
	.score-signal {
		font-weight: 500;
		white-space: nowrap;
	}
	.score-table .score-num {
		text-align: right;
		white-space: nowrap;
	}
	.score-basis {
		line-height: 1.4;
	}
	.enrich-empty {
		margin-top: 12px;
		font-size: 0.85rem;
	}
	.enrich-diff {
		width: 100%;
		border-collapse: collapse;
		margin-top: 12px;
		font-size: 0.82rem;
	}
	.enrich-diff th {
		text-align: left;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 4px 10px 6px;
		border-bottom: 1px solid var(--border);
	}
	.enrich-diff td {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
	}
	.enrich-diff tr:last-child td {
		border-bottom: none;
	}
	.enrich-pick {
		width: 36px;
		text-align: center;
	}
	.enrich-current {
		max-width: 220px;
		word-break: break-word;
	}
	.enrich-suggested {
		max-width: 240px;
		word-break: break-word;
		font-weight: 500;
	}
	/* `.visually-hidden` (used on the enrich table's pick column header) is the
	   shared recipe in app.css — this component's private copy was the second
	   of two identical ones. */
	.timeline {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.timeline-item {
		display: grid;
		grid-template-columns: 150px 1fr;
		gap: 12px;
		align-items: baseline;
		font-size: 0.82rem;
		padding-bottom: 8px;
		border-bottom: 1px solid var(--border);
	}
	.timeline-item:last-child {
		border-bottom: none;
	}
	.timeline-when {
		color: var(--text-muted);
		white-space: nowrap;
	}
	.timeline-body {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	.result-text {
		font-weight: 500;
	}
	.matched-list {
		color: var(--danger);
		font-weight: 500;
	}
</style>
