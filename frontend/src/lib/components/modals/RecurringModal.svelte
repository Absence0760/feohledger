<script lang="ts">
	import type {
		RecurringTemplate,
		RecurringCadence,
		UpcomingSchedule,
		RecurringHistory
	} from '#lib/types/recurring.ts';
	import {
		RECURRING_CADENCES,
		CADENCE_LABEL_KEYS,
		recurringStatusLabelKey,
		STATUS_TONES
	} from '#lib/types/recurring.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import StatusBadge from '#lib/components/ui/StatusBadge.svelte';
	import VendorPicker from '#lib/components/ui/VendorPicker.svelte';
	import type { InvoiceStatus } from '#lib/types/invoice.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { normalizeMoneyInput } from '#lib/utils/moneyInput.ts';
	import { listGlAccounts, listInvoiceChart } from '#lib/api/glAccounts.ts';
	import { glAccountOptionLabel, type GlAccountOption } from '#lib/types/glAccount.ts';
	import { entityStore } from '#lib/stores/entity.svelte.ts';
	import {
		createRecurring,
		updateRecurring,
		pauseRecurring,
		resumeRecurring,
		endRecurring,
		generateRecurringNow,
		getUpcomingSchedule,
		getGeneratedHistory
	} from '#lib/api/recurring.ts';

	let {
		template,
		onclose,
		onsaved
	}: {
		// null → create mode; a RecurringTemplate → detail/edit mode.
		template: RecurringTemplate | null;
		onclose: () => void;
		onsaved: (t: RecurringTemplate) => void;
	} = $props();

	const isCreate = $derived(template === null);
	// create/update/lifecycle = admin/ap_manager.
	const canEdit = $derived(auth.isManager);

	// --- Editable fields (seeded from the template snapshot in edit mode) ---
	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let name = $state(template?.name ?? '');
	let vendor_id = $state(template?.vendor_id ?? '');
	let description = $state(template?.description ?? '');
	// The RAW text the user typed. The template amount reaches the backend as
	// the exact decimal string it was typed as (see `types/recurring.ts`), and
	// it is what every generated invoice is pre-coded with — a rounded figure
	// here is a rounded figure on every future invoice.
	let amount = $state(String(template?.amount ?? ''));
	let currency = $state(template?.currency ?? 'USD');
	let gl_account = $state(template?.gl_account ?? '');
	let cost_center = $state(template?.cost_center ?? '');
	let department = $state(template?.department ?? '');
	let project = $state(template?.project ?? '');
	let po_number = $state(template?.po_number ?? '');
	let payment_terms = $state(template?.payment_terms ?? '');
	let cadence = $state<RecurringCadence>(template?.cadence ?? 'monthly');
	let day_of_period = $state<number>(template?.day_of_period ?? 1);
	let start_date = $state(template?.start_date ?? '');
	let end_date = $state(template?.end_date ?? '');
	let variance_tolerance_pct = $state<number | null>(template?.variance_tolerance_pct ?? null);
	let notes = $state(template?.notes ?? '');
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);
	let busy = $state(false);

	// The GL chart this template may be coded against. Every invoice it raises
	// is coded with this string and lands under the template's entity, and the
	// backend refuses — on save, once — any code that is not an active account
	// of THAT entity's chart (`services/gl_chart`, decisions §194/§199). So the
	// picker offers exactly that chart: the template's own entity on edit, the
	// entity a create lands under (`writeEntityId` — the selection, else the
	// default) on create. Same arrangement as `CreateInvoiceModal` /
	// `InvoiceModal`; empty → free text, so a tenant with no synced chart can
	// still type a code.
	let glAccounts = $state<GlAccountOption[]>([]);

	$effect(() => {
		(async () => {
			await entityStore.ensureLoaded();
			try {
				if (template) {
					glAccounts = await listInvoiceChart(template.entity_id);
				} else {
					const target = entityStore.writeEntityId;
					// Unknown only when the entity list failed to load: fall back to
					// the header-scoped list and let the backend's check decide.
					glAccounts = target ? await listInvoiceChart(target) : await listGlAccounts();
				}
			} catch {
				// The chart is a convenience dropdown — fall back to free text.
			}
		})();
	});

	const status = $derived(template?.status ?? 'active');

	// Lifecycle gating mirrors the backend's legal transitions.
	const canPause = $derived(status === 'active');
	const canResume = $derived(status === 'paused');
	const canEnd = $derived(status === 'active' || status === 'paused');
	const canGenerate = $derived(status === 'active');

	function numOrNull(v: unknown): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = parseFloat(String(v));
		return Number.isFinite(n) ? n : null;
	}

	function buildPayload() {
		return {
			name: name.trim(),
			vendor_id: vendor_id || null,
			description: description.trim() || null,
			amount: normalizeMoneyInput(amount),
			currency: currency.trim() || 'USD',
			gl_account: gl_account.trim() || null,
			cost_center: cost_center.trim() || null,
			department: department.trim() || null,
			project: project.trim() || null,
			po_number: po_number.trim() || null,
			payment_terms: payment_terms.trim() || null,
			cadence,
			day_of_period,
			start_date: start_date || '',
			end_date: end_date || null,
			variance_tolerance_pct,
			notes: notes.trim() || null
		};
	}

	// Surface the backend's 409 conflict text (e.g. "invoices already generated")
	// verbatim — it's user-actionable.
	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	async function handleSave() {
		if (!name.trim() || !start_date) return;
		// The amount is optional (a template can be created before the price is
		// known), so BLANK legitimately means "no amount". Text we could not
		// read does not: sending `null` for a typed amount would silently make
		// every generated invoice amount-less. `normalizeMoneyInput` decides
		// shape with a regex, never `Number`.
		const typedAmount = amount.trim();
		if (typedAmount && normalizeMoneyInput(typedAmount) === null) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			const saved = isCreate
				? await createRecurring(buildPayload())
				: await updateRecurring(template!.id, buildPayload());
			toast(isCreate ? m('recurring.modal.toast.created') : m('recurring.modal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			handleError(err, isCreate ? m('recurring.modal.toast.createFailed') : m('recurring.modal.toast.saveFailed'));
		} finally {
			saving = false;
		}
	}

	async function runLifecycle(
		fn: () => Promise<RecurringTemplate>,
		successMsg: string,
		fallback: string
	) {
		busy = true;
		try {
			const updated = await fn();
			toast(successMsg, 'success');
			onsaved(updated);
			await loadPreviews();
		} catch (err) {
			handleError(err, fallback);
		} finally {
			busy = false;
		}
	}

	async function handleGenerateNow() {
		if (!template) return;
		busy = true;
		try {
			await generateRecurringNow(template.id);
			toast(m('recurring.modal.toast.generated'), 'success');
			await loadPreviews();
		} catch (err) {
			handleError(err, m('recurring.modal.toast.generateFailed'));
		} finally {
			busy = false;
		}
	}

	// --- Read-only previews (edit mode) ---
	let schedule = $state<UpcomingSchedule | null>(null);
	let history = $state<RecurringHistory | null>(null);

	async function loadPreviews() {
		if (!template) return;
		try {
			schedule = await getUpcomingSchedule(template.id, 6);
		} catch {
			schedule = null;
		}
		try {
			history = await getGeneratedHistory(template.id);
		} catch {
			history = null;
		}
	}

	$effect(() => {
		if (template) loadPreviews();
	});

	const modalTitle = $derived(
		isCreate
			? m('recurring.modal.title.new')
			: canEdit
				? m('recurring.modal.title.edit', { name: template!.name })
				: m('recurring.modal.title.view', { name: template!.name })
	);
	const ariaLabel = $derived(isCreate ? m('recurring.modal.aria.new') : m('recurring.modal.aria.detail'));
	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = $derived.by(() => {
		const key = recurringStatusLabelKey(status);
		return key ? m(key) : status;
	});
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		{#if !isCreate}
			<div class="status-row">
				<Badge tone={STATUS_TONES[status]} variant={status}>{statusLabel}</Badge>
				<span class="meta-pill">{m('recurring.modal.generatedCount', { count: template!.generated_count })}</span>
				{#if template!.next_run_on && status === 'active'}
					<span class="meta-pill">{m('recurring.modal.nextRun', { date: formatDate(template!.next_run_on) })}</span>
				{/if}
			</div>
		{/if}

		<div class="form-grid">
			<label class="full-width">
				<span>{m('recurring.modal.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={name} required disabled={!canEdit} />
			</label>
			<VendorPicker
				bind:value={vendor_id}
				label={m('recurring.modal.field.vendor')}
				placeholder={m('recurring.modal.field.noVendor')}
				selectedLabel={template?.vendor_name ?? null}
				disabled={!canEdit}
			/>
			<label>
				<span>{m('recurring.modal.field.amount')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={amount}
					oninput={(e) => (amount = e.currentTarget.value)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('recurring.modal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.cadence')}</span>
				<select bind:value={cadence} disabled={!canEdit}>
					{#each RECURRING_CADENCES as c}
						<option value={c}>{m(CADENCE_LABEL_KEYS[c])}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('recurring.modal.field.dayOfPeriod')}</span>
				<input type="number" min="1" max="28" bind:value={day_of_period} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.varianceTolerance')}</span>
				<input
					type="number"
					step="0.1"
					min="0"
					value={variance_tolerance_pct ?? ''}
					oninput={(e) => (variance_tolerance_pct = numOrNull(e.currentTarget.value))}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('recurring.modal.field.startDate')} <em class="required">*</em></span>
				<input type="date" bind:value={start_date} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.endDate')}</span>
				<input type="date" bind:value={end_date} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.glAccount')}</span>
				{#if glAccounts.length > 0}
					<select bind:value={gl_account} disabled={!canEdit} data-testid="recurring-gl-select">
						<option value="">{m('invoices.modal.field.glSelect')}</option>
						<!-- A stored code the chart no longer offers (retired, or coded
						     before the rule existed) keeps its own option, or the select
						     would render blank while the form still holds the value. The
						     backend accepts it back unchanged. -->
						{#if gl_account && !glAccounts.some((a) => a.code === gl_account)}
							<option value={gl_account}>{gl_account}</option>
						{/if}
						<!-- Keyed by `id`, not `code`: shared ∪ the entity's own may hold
						     two rows with one code. The bound value stays the code — the
						     template's `gl_account` is a String, like the invoice's. -->
						{#each glAccounts as acct (acct.id)}
							<option value={acct.code}>
								{glAccountOptionLabel(acct, entityStore, {
									withName: true,
									unknownEntity: m('glAccounts.scope.unknownEntity')
								})}
							</option>
						{/each}
					</select>
				{:else}
					<input type="text" bind:value={gl_account} disabled={!canEdit} />
				{/if}
			</label>
			<label>
				<span>{m('recurring.modal.field.costCenter')}</span>
				<input type="text" bind:value={cost_center} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.department')}</span>
				<input type="text" bind:value={department} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.project')}</span>
				<input type="text" bind:value={project} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.poNumber')}</span>
				<input type="text" bind:value={po_number} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('recurring.modal.field.paymentTerms')}</span>
				<input type="text" bind:value={payment_terms} placeholder={m('recurring.modal.field.paymentTermsPlaceholder')} disabled={!canEdit} />
			</label>
			<label class="full-width">
				<span>{m('recurring.modal.field.description')}</span>
				<input type="text" bind:value={description} disabled={!canEdit} />
			</label>
			<label class="full-width">
				<span>{m('recurring.modal.field.notes')}</span>
				<textarea bind:value={notes} rows="2" disabled={!canEdit}></textarea>
			</label>
		</div>

		<!-- Upcoming schedule (detail mode only) -->
		{#if !isCreate}
			<div class="preview-section">
				<div class="preview-title">{m('recurring.modal.upcoming.title')}</div>
				{#if schedule && schedule.occurrences.length > 0}
					<table class="preview-table">
						<thead>
							<tr>
								<th>{m('recurring.modal.upcoming.col.period')}</th>
								<th>{m('recurring.modal.upcoming.col.runsOn')}</th>
								<th class="right">{m('recurring.modal.upcoming.col.amount')}</th>
							</tr>
						</thead>
						<tbody>
							{#each schedule.occurrences as occ (occ.period_key)}
								<tr>
									<td class="mono">{occ.period_key}</td>
									<td class="muted">{formatDate(occ.run_on)}</td>
									<td class="right mono"><Money amount={occ.amount} currency={occ.currency} /></td>
								</tr>
							{/each}
						</tbody>
					</table>
				{:else}
					<p class="preview-empty">{m('recurring.modal.upcoming.empty')}</p>
				{/if}
			</div>

			<!-- Generated history -->
			<div class="preview-section">
				<div class="preview-title">
					{m('recurring.modal.history.title')}{#if history}<span class="preview-count"> · {history.total}</span>{/if}
				</div>
				{#if history && history.items.length > 0}
					<table class="preview-table">
						<thead>
							<tr>
								<th>{m('recurring.modal.history.col.invoice')}</th>
								<th>{m('recurring.modal.history.col.period')}</th>
								<th class="right">{m('recurring.modal.history.col.amount')}</th>
								<th>{m('recurring.modal.history.col.status')}</th>
								<th>{m('recurring.modal.history.col.created')}</th>
							</tr>
						</thead>
						<tbody>
							{#each history.items as item (item.invoice_id)}
								<tr>
									<td class="mono">{item.invoice_number ?? item.invoice_id.slice(0, 8)}</td>
									<td class="mono">{item.period_key}</td>
									<td class="right mono"><Money amount={item.amount} currency={item.currency} /></td>
									<td><StatusBadge status={item.status as InvoiceStatus} /></td>
									<td class="muted">{formatDate(item.created_at)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				{:else}
					<p class="preview-empty">{m('recurring.modal.history.empty')}</p>
				{/if}
			</div>
		{/if}

		<!-- Lifecycle actions (detail mode, admin/ap_manager only) -->
		{#if !isCreate && canEdit}
			<div class="lifecycle-actions">
				{#if canGenerate}
					<button
						type="button"
						class="btn-lifecycle"
						disabled={busy}
						onclick={handleGenerateNow}
						aria-label={m('recurring.modal.lifecycle.generateNowAria', { name: template!.name })}
					>
						{m('recurring.modal.lifecycle.generateNow')}
					</button>
				{/if}
				{#if canPause}
					<button
						type="button"
						class="btn-lifecycle"
						disabled={busy}
						onclick={() => runLifecycle(() => pauseRecurring(template!.id), m('recurring.modal.toast.paused'), m('recurring.modal.toast.pauseFailed'))}
						aria-label={m('recurring.modal.lifecycle.pauseAria', { name: template!.name })}
					>
						{m('recurring.modal.lifecycle.pause')}
					</button>
				{/if}
				{#if canResume}
					<button
						type="button"
						class="btn-lifecycle activate"
						disabled={busy}
						onclick={() => runLifecycle(() => resumeRecurring(template!.id), m('recurring.modal.toast.resumed'), m('recurring.modal.toast.resumeFailed'))}
						aria-label={m('recurring.modal.lifecycle.resumeAria', { name: template!.name })}
					>
						{m('recurring.modal.lifecycle.resume')}
					</button>
				{/if}
				{#if canEnd}
					<button
						type="button"
						class="btn-lifecycle end"
						disabled={busy}
						onclick={() => runLifecycle(() => endRecurring(template!.id), m('recurring.modal.toast.ended'), m('recurring.modal.toast.endFailed'))}
						aria-label={m('recurring.modal.lifecycle.endAria', { name: template!.name })}
					>
						{m('recurring.modal.lifecycle.end')}
					</button>
				{/if}
			</div>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('recurring.modal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving}>
					{saving ? m('recurring.modal.saving') : isCreate ? m('recurring.modal.create') : m('recurring.modal.save')}
				</button>
			{/if}
		</div>
	</form>
</Modal>

<style>
	.status-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 12px;
		flex-wrap: wrap;
	}

	/* The status pill is `<Badge>` now; the tone per status lives beside the
	   labels in `types/recurring`, so this modal and the list page can't drift
	   apart again. */

	.meta-pill {
		font-size: 0.72rem;
		padding: 2px 8px;
		border-radius: 8px;
		background: var(--bg);
		color: var(--text-muted);
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.form-grid label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.form-grid label.full-width {
		grid-column: 1 / -1;
	}

	.form-grid input,
	.form-grid select,
	.form-grid textarea {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	.form-grid input:disabled,
	.form-grid select:disabled,
	.form-grid textarea:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	/* --- Preview sections (schedule / history) --- */
	.preview-section {
		margin-top: 16px;
	}
	.preview-title {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 8px;
	}
	.preview-count {
		color: var(--text-muted);
		font-weight: 500;
	}
	.preview-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.preview-table th {
		text-align: left;
		padding: 4px 6px;
		color: var(--text-muted);
		font-weight: 500;
		border-bottom: 1px solid var(--border);
	}
	.preview-table th.right { text-align: right; }
	.preview-table td {
		padding: 4px 6px;
		border-bottom: 1px solid var(--border);
	}
	.preview-table td.right { text-align: right; }
	.preview-table td.muted { color: var(--text-muted); }
	.mono {
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
	.preview-empty {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0;
	}

	/* --- Lifecycle actions --- */
	.lifecycle-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 16px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}
	.btn-lifecycle {
		padding: 6px 14px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		transition: all 0.15s;
	}
	.btn-lifecycle:hover { border-color: var(--accent); color: var(--accent); }
	.btn-lifecycle:disabled { opacity: 0.6; cursor: not-allowed; }
	.btn-lifecycle.activate:hover { border-color: #1fa86a; color: #1fa86a; }
	.btn-lifecycle.end:hover { border-color: var(--danger); color: var(--danger); }
</style>
