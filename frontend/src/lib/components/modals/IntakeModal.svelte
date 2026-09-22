<script lang="ts">
	import type { IntakeRequest, IntakeType } from '$lib/types/intake';
	import {
		INTAKE_TYPES,
		intakeTypeLabelKey,
		intakeStatusLabelKey,
		intakeStatusTone,
		INTAKE_FORM_FIELDS
	} from '$lib/types/intake';
	import { auth } from '$lib/stores/auth.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { normalizeMoneyInput } from '$lib/utils/moneyInput';
	import { DEFAULT_CURRENCY } from '$lib/utils/money';
	import { createIntake, updateIntake } from '$lib/api/intake';

	let {
		intake,
		onclose,
		onsaved
	}: {
		// null → create mode; an IntakeRequest → detail/edit mode.
		intake: IntakeRequest | null;
		onclose: () => void;
		onsaved: (i: IntakeRequest) => void;
	} = $props();

	const isCreate = $derived(intake === null);

	// Status / type names are message keys, not English literals — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = intakeStatusLabelKey(s);
		return key ? m(key) : s;
	};
	const typeLabel = (t: string) => {
		const key = intakeTypeLabelKey(t);
		return key ? m(key) : t;
	};
	// Anyone in the org can raise / edit their own open intake.
	const canCreate = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk', 'cfo'));
	// Edits are only allowed while the intake is `open`.
	const editable = $derived(isCreate || intake?.status === 'open');
	const canEdit = $derived(canCreate && editable);

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let title = $state(intake?.title ?? '');
	let request_type = $state<IntakeType>((intake?.request_type as IntakeType) ?? 'software');
	// The RAW text the user typed, not a parsed number: the estimate reaches the
	// backend as the exact decimal string it was typed as (see
	// `types/intake.ts` → request side). Seeded from the response amount, which
	// itself arrives as a JSON number.
	let estimated_amount = $state(String(intake?.estimated_amount ?? ''));
	// A form's initial value: the record's own, else the org's, else the
	// platform default — named explicitly, since the store answers `null` for
	// an org it could not resolve (decisions §200).
	let currency = $state(intake?.currency ?? orgCurrency.currency ?? DEFAULT_CURRENCY);
	let vendor_name = $state(intake?.vendor_name ?? '');
	let needed_by = $state(intake?.needed_by ?? '');
	let description = $state(intake?.description ?? '');
	let justification = $state(intake?.justification ?? '');
	// Questionnaire answers keyed by field key — seeded from the existing row.
	let formData = $state<Record<string, string>>(
		Object.fromEntries(
			Object.entries((intake?.form_data ?? {}) as Record<string, unknown>).map(([k, v]) => [
				k,
				v == null ? '' : String(v)
			])
		)
	);
	/* eslint-enable svelte/state-referenced-locally */

	let saving = $state(false);

	const status = $derived(intake?.status ?? 'open');
	// The dynamic questionnaire fields for the currently-selected type.
	const fields = $derived(INTAKE_FORM_FIELDS[request_type] ?? INTAKE_FORM_FIELDS.other);

	function collectFormData(): Record<string, unknown> | null {
		const out: Record<string, unknown> = {};
		// Preserve any existing non-field keys (e.g. review_reason stamped on reject).
		for (const [k, v] of Object.entries((intake?.form_data ?? {}) as Record<string, unknown>)) {
			if (!fields.some((f) => f.key === k)) out[k] = v;
		}
		for (const f of fields) {
			const v = formData[f.key]?.trim();
			if (v) out[f.key] = v;
		}
		return Object.keys(out).length ? out : null;
	}

	async function handleSave() {
		if (!title.trim()) return;
		// The estimate is optional, so a BLANK field legitimately means "no
		// estimate". Text we could not read does NOT: posting `null` for an
		// amount the user typed would drop it silently, so it is refused here.
		// `normalizeMoneyInput` decides shape with a regex, never `Number`.
		const typedAmount = estimated_amount.trim();
		const estimate = normalizeMoneyInput(typedAmount);
		if (typedAmount && estimate === null) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			const payload = {
				title: title.trim(),
				request_type,
				estimated_amount: estimate,
				currency: currency.trim() || DEFAULT_CURRENCY,
				vendor_name: vendor_name.trim() || null,
				needed_by: needed_by || null,
				description: description.trim() || null,
				justification: justification.trim() || null,
				form_data: collectFormData()
			};
			const saved = isCreate
				? await createIntake(payload)
				: await updateIntake(intake!.id, payload);
			toast(isCreate ? m('intake.modal.toast.created') : m('intake.modal.toast.saved'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			toast(
				err instanceof Error
					? err.message
					: isCreate
						? m('intake.modal.toast.createFailed')
						: m('intake.modal.toast.saveFailed'),
				'error'
			);
		} finally {
			saving = false;
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('intake.modal.title.new')
			: canEdit
				? m('intake.modal.title.edit', { title: intake!.title })
				: m('intake.modal.title.view', { title: intake!.title })
	);
	const ariaLabel = $derived(isCreate ? m('intake.modal.aria.new') : m('intake.modal.aria.detail'));
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	<form onsubmit={(e) => { e.preventDefault(); handleSave(); }}>
		{#if !isCreate}
			<div class="status-row">
				<span class="number">{intake!.request_number}</span>
				<Badge tone={intakeStatusTone(status)} variant={status}>
					{statusLabel(status)}
				</Badge>
				{#if intake!.converted_requisition_id}
					<span class="converted-note">{m('intake.modal.requisitionCreatedNote')}</span>
				{/if}
			</div>
		{/if}

		<div class="form-grid">
			<label class="full-width">
				<span>{m('intake.modal.field.title')} <em class="required">*</em></span>
				<input type="text" bind:value={title} required disabled={!canEdit} />
			</label>
			<label>
				<span>{m('intake.modal.field.type')}</span>
				<select bind:value={request_type} disabled={!canEdit}>
					{#each INTAKE_TYPES as t}
						<option value={t}>{typeLabel(t)}</option>
					{/each}
				</select>
			</label>
			<label>
				<span>{m('intake.modal.field.estimatedAmount')}</span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={estimated_amount}
					oninput={(e) => (estimated_amount = e.currentTarget.value)}
					disabled={!canEdit}
				/>
			</label>
			<label>
				<span>{m('intake.modal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" disabled={!canEdit} />
			</label>
			<label>
				<span>{m('intake.modal.field.vendor')}</span>
				<input type="text" bind:value={vendor_name} placeholder={m('intake.modal.field.vendorPlaceholder')} disabled={!canEdit} />
			</label>
			<label>
				<span>{m('intake.modal.field.neededBy')}</span>
				<input type="date" bind:value={needed_by} disabled={!canEdit} />
			</label>
			<label class="full-width">
				<span>{m('intake.modal.field.description')}</span>
				<textarea bind:value={description} rows="2" disabled={!canEdit}></textarea>
			</label>
			<label class="full-width">
				<span>{m('intake.modal.field.justification')}</span>
				<textarea bind:value={justification} rows="2" disabled={!canEdit}></textarea>
			</label>
		</div>

		<!-- Flexible questionnaire — fields vary by request type. -->
		<div class="questionnaire">
			<span class="section-title">{m('intake.modal.questionnaire', { type: typeLabel(request_type) })}</span>
			<div class="form-grid">
				{#each fields as f (f.key)}
					<label>
						<span>{m(f.labelKey)}</span>
						<input type="text" bind:value={formData[f.key]} disabled={!canEdit} />
					</label>
				{/each}
			</div>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('intake.modal.close')}</button>
			{#if canEdit}
				<button type="submit" class="btn-primary" disabled={saving || !title.trim()}>
					{saving ? m('intake.modal.saving') : isCreate ? m('intake.modal.create') : m('intake.modal.save')}
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
	.number {
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.converted-note {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	/* The status pill is `<Badge>` now; the tone per status lives beside the
	   labels in `types/intake`, so this modal and the list page can't drift
	   apart again. */

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

	.questionnaire {
		margin-top: 16px;
		border-top: 1px solid var(--border);
		padding-top: 14px;
	}
	.section-title {
		display: block;
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 10px;
		text-transform: capitalize;
	}
</style>
