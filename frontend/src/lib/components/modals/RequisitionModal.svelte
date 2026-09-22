<script lang="ts">
	import type {
		Requisition,
		RequisitionLineItemInput,
		RequisitionStatus
	} from '$lib/types/requisition';
	import {
		requisitionStatusLabelKey,
		REQUISITION_STATUS_TONES
	} from '$lib/types/requisition';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import {
		DEFAULT_CURRENCY,
		formatMoney,
		scaleMoney,
		sumMoney,
		type MoneyString
	} from '$lib/utils/money';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { createRequisition, updateRequisition } from '$lib/api/requisitions';
	import type { GlAccountOption } from '$lib/api/expenses';
	import { glAccountOptionLabel } from '$lib/types/glAccount';
	import { entityStore } from '$lib/stores/entity.svelte';

	let {
		requisition,
		glAccounts = [],
		onclose,
		onsaved
	}: {
		// null → create mode; a Requisition → detail/edit mode.
		requisition: Requisition | null;
		glAccounts?: GlAccountOption[];
		onclose: () => void;
		onsaved: (r: Requisition) => void;
	} = $props();

	const isCreate = $derived(requisition === null);
	const canEdit = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));
	const status = $derived(requisition?.status ?? 'draft');
	// `Requisition.status` is a bare string on the wire, so both lookups below
	// are `?? fallback` — a status this build doesn't know renders its raw
	// value in a flat chip rather than blank.
	const statusKey = $derived(status as RequisitionStatus);
	// The status name is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = $derived.by(() => {
		const key = requisitionStatusLabelKey(status);
		return key ? m(key) : status;
	});
	// Header fields + line items are only editable while the requisition is a
	// draft (or being created) — a submitted/approved requisition is locked.
	const editable = $derived(canEdit && (isCreate || status === 'draft'));

	interface LineRow {
		description: string;
		quantity: number | null;
		// Raw decimal TEXT, not a float. parseFloat-ing on every keystroke and
		// sending the result is how an exact price reaches the wire rounded;
		// it is validated once at save and refused, never repaired.
		unit_price: string;
		gl_account_id: string;
		uom: string;
	}

	function toRow(li: RequisitionLineItemInput): LineRow {
		return {
			description: li.description ?? '',
			quantity: li.quantity ?? null,
			unit_price: li.unit_price == null ? '' : String(li.unit_price),
			gl_account_id: li.gl_account_id ?? '',
			uom: li.uom ?? ''
		};
	}

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let requisition_number = $state(requisition?.requisition_number ?? '');
	let title = $state(requisition?.title ?? '');
	let department = $state(requisition?.department ?? '');
	let needed_by = $state(requisition?.needed_by ?? '');
	let justification = $state(requisition?.justification ?? '');
	let currency = $state(requisition?.currency ?? orgCurrency.currency ?? DEFAULT_CURRENCY);
	let notes = $state(requisition?.notes ?? '');
	let lines = $state<LineRow[]>(
		(requisition?.line_items ?? []).map(toRow)
	);
	/* eslint-enable svelte/state-referenced-locally */

	// Seed a create modal with one empty line so the user has somewhere to type.
	if (isCreate && lines.length === 0) {
		lines = [{ description: '', quantity: null, unit_price: '', gl_account_id: '', uom: '' }];
	}

	let saving = $state(false);

	function numOrNull(v: unknown): number | null {
		if (v === '' || v === null || v === undefined) return null;
		const n = parseFloat(String(v));
		return Number.isFinite(n) ? n : null;
	}

	/** Exact `quantity * unit_price`, or `null` when either side is missing or
	 *  unreadable — the cell then shows a dash rather than a wrong figure. The
	 *  server recomputes this; the preview must not disagree with it. */
	function lineTotal(l: LineRow): MoneyString | null {
		if (l.quantity == null) return null;
		return scaleMoney(l.unit_price, l.quantity);
	}

	const computedTotal = $derived(sumMoney(lines.map(lineTotal)));

	function addLine() {
		lines = [...lines, { description: '', quantity: null, unit_price: '', gl_account_id: '', uom: '' }];
	}

	function removeLine(idx: number) {
		lines = lines.filter((_, i) => i !== idx);
	}

	/**
	 * A line's GL account, by the uuid it stores
	 * (`RequisitionLineItem.gl_account_id` is a real FK). Routed through the
	 * shared label helper so the read-only view names the owning subsidiary on
	 * a multi-entity tenant exactly as the editable `<select>` beside it does.
	 */
	function glLabel(id: string): string {
		const g = glAccounts.find((a) => a.id === id);
		return g ? optionLabel(g, true) : '—';
	}

	function optionLabel(g: GlAccountOption, withName = false): string {
		return glAccountOptionLabel(g, entityStore, {
			withName,
			unknownEntity: m('glAccounts.scope.unknownEntity')
		});
	}

	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	async function handleSave() {
		if (!requisition_number.trim()) return;
		saving = true;
		try {
			const lineItems: RequisitionLineItemInput[] = lines
				.filter((l) => l.description.trim() || l.quantity != null || l.unit_price != null)
				.map((l, i) => ({
					line_number: i + 1,
					description: l.description.trim() || null,
					quantity: l.quantity,
					unit_price: l.unit_price.trim() || null,
					gl_account_id: l.gl_account_id || null,
					uom: l.uom.trim() || null
				}));
			// Refuse, don't repair: a price the exact parser can't read must not
			// be silently coerced or dropped on its way to the wire.
			const badLine = lineItems.findIndex(
				(li) => li.unit_price != null && scaleMoney(li.unit_price, 1) === null
			);
			if (badLine !== -1) {
				toast(m('requisitions.modal.line.unitPriceInvalid', { n: badLine + 1 }), 'error');
				saving = false;
				return;
			}
			let saved: Requisition;
			if (isCreate) {
				saved = await createRequisition({
					requisition_number: requisition_number.trim(),
					title: title.trim() || null,
					department: department.trim() || null,
					needed_by: needed_by || null,
					justification: justification.trim() || null,
					currency: currency.trim() || DEFAULT_CURRENCY,
					notes: notes.trim() || null,
					line_items: lineItems
				});
			} else {
				saved = await updateRequisition(requisition!.id, {
					requisition_number: requisition_number.trim(),
					title: title.trim() || null,
					department: department.trim() || null,
					needed_by: needed_by || null,
					justification: justification.trim() || null,
					currency: currency.trim() || DEFAULT_CURRENCY,
					notes: notes.trim() || null,
					line_items: lineItems
				});
			}
			toast(isCreate ? m('requisitions.modal.toast.created') : m('requisitions.modal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			handleError(err, isCreate ? m('requisitions.modal.toast.createFailed') : m('requisitions.modal.toast.saveFailed'));
		} finally {
			saving = false;
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('requisitions.modal.title.new')
			: editable
				? m('requisitions.modal.title.edit', { number: requisition!.requisition_number })
				: m('requisitions.modal.title.view', { number: requisition!.requisition_number })
	);
	const ariaLabel = $derived(isCreate ? m('requisitions.modal.aria.new') : m('requisitions.modal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		{#if !isCreate}
			<div class="status-row">
				<Badge tone={REQUISITION_STATUS_TONES[statusKey] ?? 'neutral'} variant={status}>
					{statusLabel}
				</Badge>
				{#if requisition?.converted_po_id}
					<span class="muted">{m('requisitions.modal.poCreatedNote')}</span>
				{/if}
				{#if requisition?.rejection_reason}
					<span class="muted">{m('requisitions.modal.rejectedNote', { reason: requisition.rejection_reason })}</span>
				{/if}
			</div>
		{/if}

		<div class="form-grid">
			<label>
				<span>{m('requisitions.modal.field.requisitionNumber')} <em class="required">*</em></span>
				<input type="text" bind:value={requisition_number} required disabled={!editable} />
			</label>
			<label>
				<span>{m('requisitions.modal.field.title')}</span>
				<input type="text" bind:value={title} disabled={!editable} />
			</label>
			<label>
				<span>{m('requisitions.modal.field.department')}</span>
				<input type="text" bind:value={department} disabled={!editable} />
			</label>
			<label>
				<span>{m('requisitions.modal.field.neededBy')}</span>
				<input type="date" bind:value={needed_by} disabled={!editable} />
			</label>
			<label>
				<span>{m('requisitions.modal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" disabled={!editable} />
			</label>
			<label class="full-width">
				<span>{m('requisitions.modal.field.justification')}</span>
				<textarea bind:value={justification} rows="2" disabled={!editable}></textarea>
			</label>
			<label class="full-width">
				<span>{m('requisitions.modal.field.notes')}</span>
				<textarea bind:value={notes} rows="2" disabled={!editable}></textarea>
			</label>
		</div>

		<!-- Line items -->
		<div class="lines-section">
			<div class="lines-head">
				<span class="lines-title">{m('requisitions.modal.lineItems')}</span>
				{#if editable}
					<button type="button" class="btn-add-line" onclick={addLine}>{m('requisitions.modal.addLine')}</button>
				{/if}
			</div>

			{#if lines.length === 0}
				<p class="muted">{m('requisitions.modal.noLineItems')}</p>
			{:else}
				<table class="lines-table">
					<thead>
						<tr>
							<th>{m('requisitions.modal.col.description')}</th>
							<th class="right">{m('requisitions.modal.col.qty')}</th>
							<th class="right">{m('requisitions.modal.col.unitPrice')}</th>
							<th>{m('requisitions.modal.col.uom')}</th>
							<th>{m('requisitions.modal.col.gl')}</th>
							<th class="right">{m('requisitions.modal.col.total')}</th>
							{#if editable}<th></th>{/if}
						</tr>
					</thead>
					<tbody>
						{#each lines as l, i (i)}
							<tr>
								<td>
									{#if editable}
										<input
											type="text"
											bind:value={l.description}
											placeholder={m('requisitions.modal.line.descriptionPlaceholder')}
											aria-label={m('requisitions.modal.line.descriptionAria', { n: i + 1 })}
										/>
									{:else}
										{l.description || '—'}
									{/if}
								</td>
								<td class="right">
									{#if editable}
										<input
											type="number"
											step="0.0001"
											min="0"
											class="num"
											aria-label={m('requisitions.modal.line.quantityAria', { n: i + 1 })}
											value={l.quantity ?? ''}
											oninput={(e) => (l.quantity = numOrNull(e.currentTarget.value))}
										/>
									{:else}
										{l.quantity ?? '—'}
									{/if}
								</td>
								<td class="right">
									{#if editable}
										<input
											type="number"
											step="0.01"
											min="0"
											class="num"
											aria-label={m('requisitions.modal.line.unitPriceAria', { n: i + 1 })}
											bind:value={l.unit_price}
										/>
									{:else}
										{l.unit_price || '—'}
									{/if}
								</td>
								<td>
									{#if editable}
										<input
											type="text"
											class="uom"
											bind:value={l.uom}
											placeholder={m('requisitions.modal.line.uomPlaceholder')}
											aria-label={m('requisitions.modal.line.uomAria', { n: i + 1 })}
										/>
									{:else}
										{l.uom || '—'}
									{/if}
								</td>
								<td>
									{#if editable}
										<select bind:value={l.gl_account_id} aria-label={m('requisitions.modal.line.glAria', { n: i + 1 })}>
											<option value="">—</option>
											{#each glAccounts as g (g.id)}
												<option value={g.id}>{optionLabel(g)}</option>
											{/each}
										</select>
									{:else}
										{glLabel(l.gl_account_id)}
									{/if}
								</td>
								<td class="right mono">{lineTotal(l) === null
										? '—'
										: formatMoney(lineTotal(l), { currency })}</td>
								{#if editable}
									<td>
										<button type="button" class="btn-remove-line" onclick={() => removeLine(i)} aria-label={m('requisitions.modal.line.removeAria')}>×</button>
									</td>
								{/if}
							</tr>
						{/each}
					</tbody>
					<tfoot>
						<tr>
							<td colspan={editable ? 5 : 5} class="right total-label">{m('requisitions.modal.totalLabel')}</td>
							<td class="right mono total-value"><Money amount={computedTotal} {currency} /></td>
							{#if editable}<td></td>{/if}
						</tr>
					</tfoot>
				</table>
			{/if}
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('requisitions.modal.close')}</button>
			{#if editable}
				<button type="submit" class="btn-primary" disabled={saving || !requisition_number.trim()}>
					{saving ? m('requisitions.modal.saving') : isCreate ? m('requisitions.modal.create') : m('requisitions.modal.save')}
				</button>
			{/if}
		</div>
	</form>
</Modal>

<style>
	.status-row {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-bottom: 12px;
	}
	.muted {
		font-size: 0.82rem;
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
	.form-grid textarea:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.lines-section {
		margin-top: 18px;
	}
	.lines-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 8px;
	}
	.lines-title {
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text);
	}
	.btn-add-line {
		padding: 4px 10px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-add-line:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.lines-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.lines-table th,
	.lines-table td {
		padding: 5px 6px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}
	.lines-table th.right,
	.lines-table td.right {
		text-align: right;
	}
	.lines-table input,
	.lines-table select {
		/* base look (border/radius/colour/font/chevron) from the global recipe */
		width: 100%;
		padding: 5px 30px 5px 6px;
		font-size: 0.82rem;
	}
	.lines-table input.num {
		text-align: right;
		max-width: 90px;
	}
	.lines-table input.uom {
		max-width: 60px;
	}
	.btn-remove-line {
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 1.1rem;
		line-height: 1;
		cursor: pointer;
	}
	.btn-remove-line:hover {
		color: var(--danger);
	}
	.total-label {
		font-weight: 600;
		color: var(--text-muted);
	}
	.total-value {
		font-weight: 600;
	}
	.mono {
		font-variant-numeric: tabular-nums;
	}
</style>
