<script lang="ts">
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import KpiCard from '$lib/components/ui/KpiCard.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import SearchBox from '$lib/components/ui/SearchBox.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import VendorTaxModal from '$lib/components/modals/VendorTaxModal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { formatMoney, isPositiveAmount, sumMoney } from '$lib/utils/money';
	import { get1099Report, file1099Batch } from '$lib/api/tax';
	import { auth } from '$lib/stores/auth.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import type {
		Box1099Allocation,
		Filing1099Response,
		Report1099,
		Vendor1099Row
	} from '$lib/types/tax';
	import { formatDate } from '$lib/utils/time';

	// `POST /api/tax/vendors/{id}/{w9,tin-verify}` and `POST /api/tax/1099/file`
	// are all `require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)` — a CFO can read this
	// page (RBAC admits admin/ap_manager/cfo on the report/dashboard/PDF-download
	// endpoints) but not mutate compliance state. Mirrors the backend exactly.
	const canManage = $derived(auth.hasAnyRole('admin', 'ap_manager'));

	// Year selector — current year and the prior five (1099s are filed for
	// completed calendar years, so people mostly look back).
	const currentYear = new Date().getFullYear();
	const YEARS = Array.from({ length: 6 }, (_, i) => currentYear - i);

	let year = $state(currentYear);
	let report = $state<Report1099 | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	let search = $state('');
	// Chip keys: 'all' | 'reportable' | 'missing_w9' | 'over_threshold' |
	// 'tin_unverified' | 'card_excluded' | 'unmapped'. Typed as string to match
	// FilterChips' bindable `active`.
	let rowFilter = $state('all');

	// Row-level "manage this vendor's tax bookkeeping" modal (W-9 upload/edit,
	// TIN verify, PDF download) — admin/ap_manager only.
	let manageRow = $state<Vendor1099Row | null>(null);

	// E-filing is a confirm-then-act flow (arm, then confirm) mirroring
	// `RunDetailModal`'s execute button and `payments/+page.svelte`'s void
	// dialog — submitting to the IRS is not reversible from this app.
	let showFileModal = $state(false);
	// Which form this batch files. It matters now that a vendor's reportable
	// total is split across boxes: a 1099-MISC batch carries only that vendor's
	// MISC boxes, so a tenant with rent AND contractor spend files twice. The
	// backend takes `form_type` on every filing; this is the control for it.
	const FORM_TYPES = ['1099-NEC', '1099-MISC'] as const;
	let fileFormType = $state<string>('1099-NEC');
	let fileArmed = $state(false);
	let filing = $state(false);
	let fileResult = $state<Filing1099Response | null>(null);
	let fileError = $state<string | null>(null);

	// Sequences `load` (latest-issued wins). Stepping the year selector twice
	// quickly left two GETs in flight with nothing deciding which may write, so
	// the table — and the totals denominated via `report.currency` — could show
	// 2024's per-vendor YTD figures while the selector read 2023. This is the
	// surface people read to decide which vendors get a 1099 filed.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const loadSequence = createRequestSequencer();

	async function load() {
		const token = loadSequence.start();
		loading = true;
		error = null;
		try {
			const res = await get1099Report(year);
			if (!loadSequence.canCommit(token)) return;
			report = res;
		} catch (e) {
			if (loadSequence.isCurrentRequest(token)) {
				report = null;
				error = e instanceof Error ? e.message : m('tax.error.load');
			}
		} finally {
			// `isCurrentRequest`, never `canCommit` — a superseded response must
			// not clear the spinner the newest request owns.
			if (loadSequence.isCurrentRequest(token)) loading = false;
		}
	}

	$effect(() => {
		// Re-load whenever the year changes.
		void year;
		load();
	});

	// The report carries the org's reporting (home) currency the totals are
	// denominated in — authoritative for display. Fall back to USD only before
	// the first load resolves (no money renders until `report` exists anyway).
	let reportCurrency = $derived(report?.currency ?? 'USD');

	// A vendor is "reportable" when it's 1099-eligible and crossed the
	// threshold — that's the set that actually needs a form filed.
	function isReportable(r: Vendor1099Row): boolean {
		return r.is_1099_eligible && r.over_threshold;
	}

	// Did this vendor take any card-rail money? That leg is deliberately kept
	// OUT of `ytd_paid` (the card settlement entity files it on a 1099-K), and
	// is surfaced so the operator can tie our figures to the processor's.
	function hasCardExcluded(r: Vendor1099Row): boolean {
		return isPositiveAmount(r.card_paid);
	}

	// Reportable money sitting in the fallback box because no GL → box rule
	// matched it. It IS filed — but in whichever box the org nominated as the
	// catch-all, so this is the "go write a mapping rule" worklist.
	function hasUnmappedSpend(r: Vendor1099Row): boolean {
		return isPositiveAmount(r.unmapped_paid);
	}

	function boxesForForm(r: Vendor1099Row, formType: string): Box1099Allocation[] {
		return (r.box_allocations ?? []).filter((b) => b.form_type === formType);
	}

	// The amounts that would be filed for one vendor on one form. A row with no
	// allocation at all (a shape older than per-box filing) falls back to the
	// whole reportable total, mirroring the backend's `box_total_for_form`.
	function formAmounts(r: Vendor1099Row, formType: string): string[] {
		const boxes = r.box_allocations ?? [];
		if (boxes.length === 0) return [r.ytd_paid];
		return boxesForForm(r, formType).map((b) => b.amount);
	}

	// Whether this vendor must be filed on `formType` — the backend's own
	// per-box, per-year verdict (`required_forms`, mirroring
	// `tax_1099.row_requires_form`), never a client-side threshold. A vendor
	// over threshold on one form is not thereby required on the other.
	function requiresForm(r: Vendor1099Row, formType: string): boolean {
		if ((r.box_allocations ?? []).length === 0) return r.over_threshold;
		return (r.required_forms ?? []).includes(formType);
	}

	// Vendors that actually have something to file on the selected form —
	// `sumMoney` only decides membership + renders one display subtotal; every
	// per-box figure on screen is the backend's own string-Decimal.
	let filableForForm = $derived(
		(report?.rows ?? []).filter(
			(r) =>
				isReportable(r) &&
				requiresForm(r, fileFormType) &&
				sumMoney(formAmounts(r, fileFormType)) > 0
		)
	);
	let filableTotalForForm = $derived(
		sumMoney(filableForForm.flatMap((r) => formAmounts(r, fileFormType)))
	);

	let filteredRows = $derived.by(() => {
		const rows = report?.rows ?? [];
		const q = search.trim().toLowerCase();
		return rows.filter((r) => {
			if (q && !r.vendor_name.toLowerCase().includes(q)) return false;
			switch (rowFilter) {
				case 'reportable':
					return isReportable(r);
				case 'missing_w9':
					return isReportable(r) && !r.w9_on_file;
				case 'tin_unverified':
					return needsTinVerification(r);
				case 'over_threshold':
					return r.over_threshold;
				case 'card_excluded':
					return hasCardExcluded(r);
				case 'unmapped':
					return hasUnmappedSpend(r);
				default:
					return true;
			}
		});
	});

	// $derived so the column headers re-render when the locale changes.
	let COLUMNS = $derived([
		{ label: m('tax.col.vendor') },
		{ label: m('tax.col.classification') },
		{ label: m('tax.col.1099'), class: 'center' },
		{ label: m('tax.col.w9'), class: 'center' },
		{ label: m('tax.col.tin'), class: 'center' },
		{ label: m('tax.col.payments'), class: 'right' },
		{ label: m('tax.col.ytdPaid'), class: 'right' },
		{ label: m('tax.col.cardExcluded'), class: 'right' },
		...(canManage ? [{ label: m('tax.col.actions'), class: 'actions-col' }] : [])
	]);

	// A TIN is only "verified" once `POST .../tin-verify` has stamped
	// `tin_verified_at` — merely having a tax id captured is not proof it
	// matched the IRS (or even passed the offline format check).
	function isTinVerified(r: Vendor1099Row): boolean {
		return r.tin_verified;
	}

	// Reportable + over threshold but not yet TIN-verified — the other half
	// of the January chase list alongside "missing W-9".
	function needsTinVerification(r: Vendor1099Row): boolean {
		return isReportable(r) && !r.tin_verified;
	}

	// Display string for the IRS threshold, e.g. "$600". Derived so the
	// snippet body (where `report` isn't narrowed) can use it safely.
	let thresholdLabel = $derived(report ? `$${report.threshold_usd}` : '$600');

	// Patches one row in place after a `VendorTaxModal` mutation (W-9
	// upload/edit, TIN verify) — avoids a full report re-fetch for a
	// single-vendor change, and keeps the KPI/filter counts (which derive
	// from `report.rows`) in sync since they read the same array.
	function onVendorUpdated(vendorId: string, patch: Partial<Vendor1099Row>) {
		if (!report) return;
		const idx = report.rows.findIndex((r) => r.vendor_id === vendorId);
		if (idx === -1) return;
		report.rows[idx] = { ...report.rows[idx], ...patch };
	}

	function openFileModal() {
		fileFormType = '1099-NEC';
		fileArmed = false;
		fileResult = null;
		fileError = null;
		showFileModal = true;
	}

	function closeFileModal() {
		if (filing) return;
		showFileModal = false;
		fileArmed = false;
		fileResult = null;
		fileError = null;
	}

	// E-filing is genuinely irreversible once a real filing adapter (vs. the
	// local `mock`) is configured — a submitted 1099 batch reaches the IRS.
	// Arm-then-confirm mirrors `RunDetailModal`'s execute button: the first
	// click reveals the eligible-vendor count + a plain-language warning, the
	// second actually submits. The endpoint is itself idempotent (same
	// `idempotency_key` → the stored confirmation, never a duplicate filing),
	// so a page refresh mid-flight and a retry are both safe — but the UI
	// still shouldn't invite a stray click to trigger the first submit.
	async function submitFile() {
		if (!fileArmed) {
			fileArmed = true;
			return;
		}
		filing = true;
		fileError = null;
		try {
			fileResult = await file1099Batch(year, fileFormType);
			toast(
				fileResult.already_filed
					? m('tax.fileModal.toast.alreadyFiled')
					: m('tax.fileModal.toast.filed', { accepted: fileResult.accepted_count, submitted: fileResult.submitted_count }),
				'success'
			);
			await load();
		} catch (err) {
			fileError = err instanceof Error ? err.message : m('tax.fileModal.toast.failed');
			toast(fileError, 'error');
		} finally {
			filing = false;
		}
	}
</script>

<PageHeader title={m('tax.title')}>
	{#snippet actions()}
		<label class="year-select">
			<span class="year-label">{m('tax.taxYear')}</span>
			<select bind:value={year} aria-label={m('tax.taxYear')}>
				{#each YEARS as y (y)}
					<option value={y}>{y}</option>
				{/each}
			</select>
		</label>
		{#if canManage}
			<button
				class="btn-secondary"
				disabled={!report || report.vendor_count_eligible_over_threshold === 0}
				onclick={openFileModal}
			>
				{m('tax.fileButton')}
			</button>
		{/if}
	{/snippet}

	<!-- KPI row — rendered on EVERY state, never gated on the response.
	     `docs/decisions.md` §125: an absent figure is a dash, and whether it is
	     absent because it is still arriving is ANNOUNCED, not drawn. This row
	     used to sit inside `{:else if report}` and collapse to nothing while the
	     report was in flight — the same §34-satisfying-but-different mechanism
	     `/cfo` carried, so the one convention had three shapes across four
	     pages. `/discounts`, `/bank-reconciliation` and `/cfo` are the others.

	     `pending` is only consulted when the value is MISSING, so switching year
	     keeps the previous year's figures on screen rather than blanking the row
	     — the loading card below names the year being fetched — and a FAILED
	     load leaves them absent-but-not-loading with the retry card above saying
	     why. The two counts that carry a red verdict drop it while there is no
	     figure to have a verdict about, which is the point: "0 reportable
	     vendors without a W-9" is the reassuring answer, and it must not be
	     drawn before anyone has computed it. -->
	<div class="kpi-row">
		<KpiCard
			value={report ? String(report.vendor_count_total) : null}
			label={m('tax.kpi.vendorsWithPayments')}
			pending={loading}
		/>
		<KpiCard
			value={report ? String(report.vendor_count_eligible_over_threshold) : null}
			label={m('tax.kpi.reportableOver', { threshold: report?.threshold_usd ?? '' })}
			highlight="green"
			pending={loading}
		/>
		<KpiCard
			value={report ? String(report.vendor_count_over_threshold_without_w9) : null}
			label={m('tax.kpi.reportableWithoutW9')}
			highlight={report && report.vendor_count_over_threshold_without_w9 > 0 ? 'red' : null}
			pending={loading}
		/>
		<KpiCard
			value={report ? formatMoney(report.total_reportable, { currency: report.currency }) : null}
			label={m('tax.kpi.totalReportable')}
			sub={report && isPositiveAmount(report.total_card_excluded)
				? m('tax.kpi.cardExcludedSub', {
						amount: formatMoney(report.total_card_excluded, { currency: report.currency })
					})
				: null}
			pending={loading}
		/>
	</div>

	{#if error}
		<div class="state-card error" role="alert">
			<p>{error}</p>
			<button class="btn-primary" onclick={load}>{m('tax.retry')}</button>
		</div>
	{:else if loading && !report}
		<div class="state-card" aria-busy="true">{m('tax.loadingReport', { year })}</div>
	{:else if report}
		{#if report.box_allocations.length > 0}
			<section class="box-panel" aria-labelledby="box-panel-title">
				<h2 id="box-panel-title">{m('tax.boxes.title')}</h2>
				<p class="box-panel-sub">{m('tax.boxes.subtitle')}</p>
				<ul class="box-list">
					{#each report.box_allocations as b (b.box)}
						<li class="box-item">
							<span class="box-code">{b.box}</span>
							<span class="box-label">
								{b.label}
								{#if b.fallback}
									<span class="box-fallback">{m('tax.boxes.fallbackTag')}</span>
								{/if}
							</span>
							<span class="box-count">{m('tax.boxes.paymentCount', { n: b.payment_count })}</span>
							<span class="box-amount mono">
								<Money amount={b.amount} currency={reportCurrency} />
							</span>
						</li>
					{/each}
				</ul>
				{#if isPositiveAmount(report.total_unmapped)}
					<p class="box-note warn">
						{m('tax.boxes.unmapped', {
							amount: formatMoney(report.total_unmapped, { currency: report.currency }),
							box: report.box_allocations.find((b) => b.fallback)?.box ?? 'NEC-1'
						})}
					</p>
				{/if}
				{#if report.box_allocation_reconciled}
					<p class="box-note">{m('tax.boxes.reconciled')}</p>
				{:else}
					<p class="box-note warn" role="alert">
						{m('tax.boxes.notReconciled', {
							residual: formatMoney(report.box_unallocated, { currency: report.currency })
						})}
					</p>
				{/if}
			</section>
		{/if}

		<div class="toolbar-row">
			<FilterChips
				chips={[
					{ key: 'all', label: m('common.all'), count: report.rows.length },
					{
						key: 'reportable',
						label: m('tax.filter.reportable'),
						count: report.rows.filter(isReportable).length
					},
					{
						key: 'missing_w9',
						label: m('tax.filter.missingW9'),
						count: report.rows.filter((r) => isReportable(r) && !r.w9_on_file).length,
						alert: report.vendor_count_over_threshold_without_w9 > 0
					},
					{
						key: 'tin_unverified',
						label: m('tax.filter.tinUnverified'),
						count: report.rows.filter(needsTinVerification).length
					},
					{
						key: 'over_threshold',
						label: m('tax.filter.overThreshold', { threshold: report.threshold_usd }),
						count: report.rows.filter((r) => r.over_threshold).length
					},
					{
						key: 'card_excluded',
						label: m('tax.filter.cardExcluded'),
						count: report.rows.filter(hasCardExcluded).length
					},
					{
						key: 'unmapped',
						label: m('tax.filter.unmapped'),
						count: report.rows.filter(hasUnmappedSpend).length
					}
				]}
				bind:active={rowFilter}
			/>
			<SearchBox bind:value={search} placeholder={m('tax.searchPlaceholder')} ariaLabel={m('tax.searchAria')} />
		</div>

		<DataTable
			columns={COLUMNS}
			isEmpty={filteredRows.length === 0}
			empty={report.rows.length === 0
				? m('tax.empty.noVendors')
				: m('tax.empty.noMatch')}
		>
			{#snippet body()}
				{#each filteredRows as r (r.vendor_id)}
					<tr class:row-flag={isReportable(r) && !r.w9_on_file}>
						<td class="vendor">{r.vendor_name}</td>
						<td class="muted">{r.tax_classification ?? '—'}</td>
						<td class="center">
							{#if r.is_1099_eligible}
								<Badge tone="success" variant="chip-on">{m('tax.chip.eligible')}</Badge>
							{:else}
								<Badge tone="muted" variant="chip-off">{m('tax.chip.no')}</Badge>
							{/if}
						</td>
						<td class="center">
							{#if r.w9_on_file}
								<Badge tone="success" variant="chip-on" title={formatDate(r.w9_received_date)}>
									{m('tax.chip.onFile')}
								</Badge>
							{:else}
								<Badge tone="danger" variant="chip-warn">{m('tax.chip.missing')}</Badge>
							{/if}
						</td>
						<td class="center">
							{#if isTinVerified(r)}
								<Badge tone="success" variant="chip-on">{m('tax.chip.verified')}</Badge>
							{:else if r.tax_id}
								<Badge tone="danger" variant="chip-warn">{m('tax.chip.unverified')}</Badge>
							{:else}
								<Badge tone="danger" variant="chip-warn">{m('tax.chip.missing')}</Badge>
							{/if}
						</td>
						<td class="right mono">{r.payment_count}</td>
						<td class="right mono ytd-reportable">
							<span class:over={r.over_threshold}>
								<Money amount={r.ytd_paid} currency={reportCurrency} />
							</span>
							{#if r.over_threshold}
								<span
									class="threshold-flag"
									title={m('tax.thresholdFlag', { threshold: thresholdLabel })}>▲</span
								>
							{/if}
							{#if r.box_allocations.length > 0}
								<span class="box-split">
									{#each r.box_allocations as b (b.box)}
										<span
											class="box-split-item"
											class:fallback={b.fallback}
											title={m('tax.boxes.rowTitle', {
												label: b.label,
												amount: formatMoney(b.amount, { currency: reportCurrency })
											})}
										>
											{b.box}{#if r.box_allocations.length > 1}&nbsp;<Money
													amount={b.amount}
													currency={reportCurrency}
												/>{/if}
										</span>
									{/each}
								</span>
							{/if}
						</td>
						<td class="right mono card-excluded">
							{#if hasCardExcluded(r)}
								<Money amount={r.card_paid} currency={reportCurrency} />
								<span class="card-count"
									>{m('tax.cardPaymentCount', { n: r.card_payment_count })}</span
								>
							{:else}
								<span class="muted">—</span>
							{/if}
						</td>
						{#if canManage}
							<td class="actions-col">
								<RowAction onclick={() => (manageRow = r)}>
									{m('tax.action.manage')}
								</RowAction>
							</td>
						{/if}
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		<p class="report-meta">
			{m('tax.reportMeta', {
				generated: formatDate(report.generated_at),
				threshold: report.threshold_usd,
				year
			})}
			{m('tax.reportMetaCard')}
		</p>
	{/if}
</PageHeader>

{#if manageRow}
	{@const activeVendorId = manageRow.vendor_id}
	<VendorTaxModal
		row={manageRow}
		{year}
		currency={reportCurrency}
		onclose={() => (manageRow = null)}
		onupdated={(patch) => onVendorUpdated(activeVendorId, patch)}
	/>
{/if}

<Modal
	open={showFileModal}
	ariaLabel={m('tax.fileModal.title', { year })}
	title={m('tax.fileModal.title', { year })}
	onclose={closeFileModal}
>
	{#if fileResult}
		<p class="modal-hint">
			{fileResult.already_filed
				? m('tax.fileModal.resultAlready')
				: m('tax.fileModal.resultAccepted', {
						accepted: fileResult.accepted_count,
						submitted: fileResult.submitted_count
					})}
		</p>
		{#if fileResult.confirmation_number}
			<p class="modal-hint confirmation-number">
				{m('tax.fileModal.confirmationNumber')}: {fileResult.confirmation_number}
			</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-primary" onclick={closeFileModal}>
				{m('tax.fileModal.close')}
			</button>
		</div>
	{:else if report}
		<label class="form-type">
			<span>{m('tax.fileModal.formType')}</span>
			<select bind:value={fileFormType} disabled={filing || fileArmed}>
				{#each FORM_TYPES as ft (ft)}
					<option value={ft}>{ft}</option>
				{/each}
			</select>
		</label>
		<p class="modal-hint">
			{m('tax.fileModal.summary', {
				count: filableForForm.length,
				year,
				amount: formatMoney(filableTotalForForm, { currency: report.currency })
			})}
		</p>
		<p class="modal-hint">{m('tax.fileModal.formTypeHint')}</p>
		<p class="modal-warn">{m('tax.fileModal.warning')}</p>
		{#if fileArmed}
			<p class="modal-warn armed" role="alert">{m('tax.fileModal.armedNote')}</p>
		{/if}
		{#if fileError}
			<p class="modal-warn" role="alert">{fileError}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeFileModal}>{m('common.cancel')}</button>
			<button
				type="button"
				class="btn-danger"
				class:armed={fileArmed}
				disabled={filing || filableForForm.length === 0}
				onclick={submitFile}
			>
				{filing
					? m('tax.fileModal.filing')
					: fileArmed
						? m('tax.fileModal.confirmFinal')
						: m('tax.fileModal.confirmArm', {
								count: filableForForm.length,
								year
							})}
			</button>
		</div>
	{/if}
</Modal>

<style>
	.box-panel {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 16px 18px;
	}

	.box-panel h2 {
		margin: 0;
		font-size: 0.95rem;
		font-weight: 600;
	}

	.box-panel-sub {
		margin: 4px 0 12px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.box-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.box-item {
		display: grid;
		grid-template-columns: 5.5rem 1fr auto auto;
		align-items: baseline;
		gap: 12px;
		padding: 6px 0;
		border-top: 1px solid var(--border);
	}

	.box-item:first-child {
		border-top: none;
	}

	.box-code {
		font-family: var(--font-mono);
		font-size: 0.78rem;
		font-weight: 600;
	}

	.box-label {
		font-size: 0.85rem;
	}

	.box-fallback {
		margin-left: 6px;
		font-size: 0.68rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 1px 6px;
	}

	.box-count,
	.box-note {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.box-amount {
		font-variant-numeric: tabular-nums;
		font-weight: 600;
	}

	.box-note {
		margin: 10px 0 0;
	}

	.box-note.warn {
		color: var(--danger);
	}

	.box-split {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 4px 8px;
		margin-top: 3px;
	}

	.box-split-item {
		font-size: 0.7rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.box-split-item.fallback {
		text-decoration: underline dotted;
		text-underline-offset: 2px;
	}

	.form-type {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 10px;
		font-size: 0.85rem;
	}

	.year-select {
		display: inline-flex;
		align-items: center;
		gap: 8px;
	}

	.year-label {
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.year-select select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 6px 30px 6px 10px;
		border-radius: 6px;
		background-color: var(--surface);
	}

	.state-card {
		padding: 32px;
		text-align: center;
		color: var(--text-muted);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
	}

	.state-card.error {
		color: var(--danger);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 12px;
	}

	.toolbar-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		flex-wrap: wrap;
	}

	.center {
		text-align: center;
	}

	.muted {
		color: var(--text-muted);
	}

	.vendor {
		font-weight: 500;
	}

	.row-flag {
		background: rgba(240, 70, 70, 0.04);
	}

	.over {
		font-weight: 600;
	}

	/* Card-rail money EXCLUDED from the filed 1099 figure. Deliberately
	   quieter than the reportable YTD column beside it — the reportable
	   number is what gets filed and must read as primary. The column header
	   ("Card excluded (1099-K)") is what identifies it, not the styling, so
	   the distinction survives without colour (WCAG 1.4.1). */
	.card-excluded {
		color: var(--text-muted);
		font-weight: 400;
	}

	.card-count {
		display: block;
		font-size: 0.68rem;
		color: var(--text-muted);
	}

	.threshold-flag {
		color: #d4940a;
		margin-left: 4px;
		font-size: 0.7rem;
	}

	.report-meta {
		font-size: 0.8rem;
		color: var(--text-muted);
		margin: 4px 2px 0;
	}

	.actions-col {
		white-space: nowrap;
		width: 1%;
	}

	.confirmation-number {
		font-family: var(--font-mono);
	}

	.btn-secondary {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-secondary:hover:not(:disabled) {
		background: var(--bg);
	}

	.btn-secondary:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	/* --- File-1099s confirm modal --- */

	.modal-warn {
		font-size: 0.82rem;
		color: var(--text);
		margin: 0 0 14px;
		padding: 10px 12px;
		background: rgba(224, 64, 64, 0.08);
		border: 1px solid rgba(224, 64, 64, 0.3);
		border-radius: 4px;
	}

	.modal-warn.armed {
		font-weight: 600;
	}

	.btn-danger {
		padding: 8px 18px;
		border-radius: 4px;
		border: 1px solid var(--danger-strong);
		background: transparent;
		color: var(--danger-strong);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	/* Armed = the second, committing click — filled instead of outlined, the
	   same "unarmed vs armed" visual escalation `RunDetailModal`'s
	   `.btn-execute.armed` uses. */
	.btn-danger.armed {
		background: var(--danger-strong);
		color: #fff;
	}

	.btn-danger:hover:not(:disabled) {
		filter: brightness(1.1);
	}

	.btn-danger:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
</style>
