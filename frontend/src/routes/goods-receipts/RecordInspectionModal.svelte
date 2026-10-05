<script lang="ts">
	/**
	 * Record a quality inspection against a goods receipt — the 4th leg of
	 * 4-way matching, and until now the one leg the app rendered the
	 * consequences of without offering any way to enter.
	 *
	 * Mutate-only (admin / ap_manager): `/goods-receipts` never renders the
	 * trigger for a clerk, and `POST /api/inspections`' `require_roles` refuses
	 * the write regardless.
	 *
	 * **Why a receipt is required here even though the API allows a bare row.**
	 * `POST /api/inspections` accepts a body with neither `gr_id` nor `po_id`,
	 * and the QMS sync writes exactly that when it can't resolve either number.
	 * But `po_matching` only ever reads an inspection through one of two
	 * queries — the matched receipt's `gr_id`, else a PO-level row whose
	 * `gr_id` IS NULL — so an unlinked row is invisible to the match it was
	 * recorded for. Offering that as a form option would let someone record a
	 * failed inspection, see it listed, and watch the invoice pay anyway. The
	 * receipt carries the PO, so both ids go up together.
	 */
	import type { InspectionCreateBody, InspectionResult } from '#lib/api/inspections.ts';
	import { createInspection, INSPECTION_RESULTS } from '#lib/api/inspections.ts';
	import type { Inspection } from '#lib/api/inspections.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';

	/** The minimum a receipt must expose for this form to link an inspection. */
	export type InspectableReceipt = {
		id: string;
		gr_number: string;
		po_id: string | null;
		po_number: string | null;
	};

	let {
		receipts,
		fixedReceipt = null,
		onclose,
		onrecorded
	}: {
		/** Receipts the user may pick from. Ignored when `fixedReceipt` is set. */
		receipts: InspectableReceipt[];
		/** Opened from a receipt's detail — the subject is already decided. */
		fixedReceipt?: InspectableReceipt | null;
		onclose: () => void;
		onrecorded: (inspection: Inspection) => void;
	} = $props();

	/** The receipt the user PICKED, or `null` for "whatever the default is".
	 *  Kept apart from the resolved selection below so neither the props nor the
	 *  default have to be copied into state at construction — reading a prop into
	 *  a `$state` initializer freezes it at its first value. */
	let pickedId = $state<string | null>(null);
	let inspectionNumber = $state('');
	/** True once the user edits the number, so the suggestion stops overwriting
	 *  what they typed when they change the selected receipt. */
	let numberTouched = $state(false);
	let result = $state<InspectionResult>('pass');
	let acceptedQuantity = $state('');
	let rejectedQuantity = $state('');
	let inspectedDate = $state('');
	let inspector = $state('');
	let deviationNotes = $state('');
	let saving = $state(false);
	/**
	 * The backend's own refusal (a bad `result`, a quantity outside
	 * `Numeric(12, 4)`, a 403 from `require_roles`) is the reason the write did
	 * not land, so it goes in a persistent inline region rather than a toast
	 * that fades off a form the user is still looking at.
	 */
	let submitError = $state<string | null>(null);

	const selectedReceipt = $derived(
		fixedReceipt ??
			(pickedId ? receipts.find((r) => r.id === pickedId) : undefined) ??
			receipts[0] ??
			null
	);

	// Suggest an inspection number from the receipt, until the user types their
	// own. `inspection_number` has no uniqueness constraint on the create path
	// (the QMS sync's upsert key is the only place it is treated as a natural
	// key), so this is a convenience, not a guarantee.
	$effect(() => {
		const gr = selectedReceipt;
		if (!numberTouched) inspectionNumber = gr ? `QI-${gr.gr_number}` : '';
	});

	/** Accepted quantity is what the matcher renders into its partial-acceptance
	 *  issue, so `partial` without one produces "Partial acceptance: part of
	 *  ordered quantity accepted" — true, and useless to whoever reads it. */
	const needsAcceptedQuantity = $derived(result === 'partial');
	const quantitiesShown = $derived(result !== 'pass');

	/**
	 * A quantity is kept as the RAW TEXT the inspector typed and validated by
	 * shape, never by round-tripping through `Number` — the column is
	 * `Numeric(12, 4)` and the API takes the string, so parsing it here would
	 * introduce a float in the one place the digits are supposed to survive
	 * untouched. (This is also why the fields are `type="text"`: Svelte's
	 * `bind:value` on `type="number"` hands back a NUMBER, which both defeats
	 * that and breaks a `string`-typed handler outright.)
	 *
	 * Empty is valid — a quantity is optional on every result except `partial`,
	 * which `canSubmit` requires separately.
	 */
	const QUANTITY_SHAPE = /^\d{1,8}(\.\d{1,4})?$/;

	function quantityValid(raw: string): boolean {
		return raw.trim() === '' || QUANTITY_SHAPE.test(raw.trim());
	}

	const canSubmit = $derived(
		selectedReceipt !== null &&
			inspectionNumber.trim() !== '' &&
			quantityValid(acceptedQuantity) &&
			quantityValid(rejectedQuantity) &&
			(!needsAcceptedQuantity || acceptedQuantity.trim() !== '')
	);

	const RESULT_LABELS: Record<InspectionResult, MessageKey> = {
		pass: 'goodsReceipts.inspections.result.pass',
		fail: 'goodsReceipts.inspections.result.fail',
		partial: 'goodsReceipts.inspections.result.partial'
	};
	const RESULT_HINTS: Record<InspectionResult, MessageKey> = {
		pass: 'goodsReceipts.inspections.record.passHint',
		fail: 'goodsReceipts.inspections.record.failHint',
		partial: 'goodsReceipts.inspections.record.partialHint'
	};

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (!canSubmit || saving) return;
		const gr = selectedReceipt;
		if (!gr) return;
		saving = true;
		submitError = null;
		try {
			const body: InspectionCreateBody = {
				inspection_number: inspectionNumber.trim(),
				gr_id: gr.id,
				result
			};
			if (gr.po_id) body.po_id = gr.po_id;
			if (inspectedDate) body.inspected_date = inspectedDate;
			if (inspector.trim()) body.inspector = inspector.trim();
			if (quantitiesShown && acceptedQuantity.trim())
				body.accepted_quantity = acceptedQuantity.trim();
			if (quantitiesShown && rejectedQuantity.trim())
				body.rejected_quantity = rejectedQuantity.trim();
			if (deviationNotes.trim()) body.deviation_notes = deviationNotes.trim();
			const created = await createInspection(body);
			onrecorded(created);
		} catch (err) {
			submitError =
				err instanceof Error ? err.message : m('goodsReceipts.inspections.toast.recordFailed');
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	ariaLabel={m('goodsReceipts.inspections.record.aria')}
	title={m('goodsReceipts.inspections.record.title')}
	{onclose}
>
	<form onsubmit={submit} data-testid="record-inspection-form">
		<label class="field">
			<span>{m('goodsReceipts.inspections.record.receipt')}</span>
			{#if fixedReceipt}
				<input
					type="text"
					value={fixedReceipt.po_number
						? `${fixedReceipt.gr_number} → ${fixedReceipt.po_number}`
						: fixedReceipt.gr_number}
					readonly
					data-testid="inspection-receipt-fixed"
				/>
			{:else}
				<select
					value={selectedReceipt?.id ?? ''}
					onchange={(e) => (pickedId = e.currentTarget.value)}
					required
					data-testid="inspection-receipt"
				>
					{#each receipts as gr (gr.id)}
						<option value={gr.id}>
							{gr.po_number ? `${gr.gr_number} → ${gr.po_number}` : gr.gr_number}
						</option>
					{/each}
				</select>
			{/if}
			<small class="hint">{m('goodsReceipts.inspections.record.receiptHint')}</small>
		</label>

		<label class="field">
			<span>{m('goodsReceipts.inspections.record.number')}</span>
			<input
				type="text"
				bind:value={inspectionNumber}
				oninput={() => (numberTouched = true)}
				maxlength="100"
				required
				data-testid="inspection-number"
			/>
		</label>

		<fieldset class="field result-field">
			<legend>{m('goodsReceipts.inspections.record.result')}</legend>
			{#each INSPECTION_RESULTS as option (option)}
				<label class="radio-row">
					<input
						type="radio"
						name="inspection-result"
						value={option}
						checked={result === option}
						onchange={() => (result = option)}
						data-testid={`inspection-result-${option}`}
					/>
					<span class="radio-body">
						<span class="radio-label">{m(RESULT_LABELS[option])}</span>
						<small class="hint">{m(RESULT_HINTS[option])}</small>
					</span>
				</label>
			{/each}
		</fieldset>

		{#if quantitiesShown}
			<div class="field-row">
				<label class="field">
					<span>
						{m('goodsReceipts.inspections.record.acceptedQuantity')}
						{#if needsAcceptedQuantity}<abbr
								class="req"
								title={m('goodsReceipts.inspections.record.requiredForPartial')}>*</abbr
							>{/if}
					</span>
					<input
						type="text"
						inputmode="decimal"
						bind:value={acceptedQuantity}
						aria-invalid={quantityValid(acceptedQuantity) ? undefined : 'true'}
						required={needsAcceptedQuantity}
						data-testid="inspection-accepted-quantity"
					/>
				</label>
				<label class="field">
					<span>{m('goodsReceipts.inspections.record.rejectedQuantity')}</span>
					<input
						type="text"
						inputmode="decimal"
						bind:value={rejectedQuantity}
						aria-invalid={quantityValid(rejectedQuantity) ? undefined : 'true'}
						data-testid="inspection-rejected-quantity"
					/>
				</label>
			</div>
		{/if}

		<div class="field-row">
			<label class="field">
				<span>{m('goodsReceipts.inspections.record.inspectedDate')}</span>
				<input type="date" bind:value={inspectedDate} data-testid="inspection-date" />
			</label>
			<label class="field">
				<span>{m('goodsReceipts.inspections.record.inspector')}</span>
				<input type="text" bind:value={inspector} maxlength="255" data-testid="inspection-inspector" />
			</label>
		</div>

		<label class="field">
			<span>{m('goodsReceipts.inspections.record.notes')}</span>
			<textarea rows="2" bind:value={deviationNotes} data-testid="inspection-notes"></textarea>
			<!-- The matcher quotes these notes verbatim into the invoice's
			     "Failed quality inspection: …" issue, so they are read by whoever
			     works the resulting quality-hold exception. -->
			<small class="hint">{m('goodsReceipts.inspections.record.notesHint')}</small>
		</label>

		{#if submitError}
			<!-- `role="alert"` so the refusal reaches a screen reader without a
			     focus move (WCAG 4.1.3). -->
			<p class="submit-error" role="alert" data-testid="inspection-error">{submitError}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={!canSubmit || saving}>
				{saving
					? m('goodsReceipts.inspections.record.saving')
					: m('goodsReceipts.inspections.record.submit')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 14px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.field-row {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
	}
	.field-row .field {
		flex: 1 1 140px;
	}
	.hint {
		font-size: 0.75rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
	/* The three outcomes are a single choice, so they are a radio group inside a
	   fieldset — the legend names the group for a screen reader (WCAG 1.3.1),
	   which a bare row of radios would leave unlabelled. */
	.result-field {
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 10px 12px;
		margin-bottom: 14px;
	}
	.result-field legend {
		padding: 0 4px;
		font-size: 0.85rem;
	}
	.radio-row {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 4px 0;
		cursor: pointer;
	}
	.radio-row input {
		margin-top: 2px;
	}
	.radio-body {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.radio-label {
		color: var(--text);
		font-size: 0.85rem;
	}
	.req {
		color: var(--danger-on-tint);
		text-decoration: none;
		margin-left: 2px;
	}
	.submit-error {
		margin: 0 0 14px;
		padding: 10px 12px;
		border: 1px solid var(--danger-strong);
		border-radius: 6px;
		color: var(--danger-on-tint);
		font-size: 0.85rem;
		line-height: 1.45;
	}
	input[readonly] {
		color: var(--text-muted);
	}
</style>
