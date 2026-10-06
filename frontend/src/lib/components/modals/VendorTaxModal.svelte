<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { isPositiveAmount } from '#lib/utils/money.ts';
	import {
		downloadVendor1099Pdf,
		updateVendorTaxFields,
		uploadVendorW9,
		verifyVendorTin
	} from '#lib/api/tax.ts';
	import type { TinValidationResult, Vendor1099Row } from '#lib/types/tax.ts';

	// W-9 box-3 entity classifications. Plain English option labels — not run
	// through `m()` — mirroring the established precedent in
	// `PositivePayModal.svelte` for domain-technical enum values.
	const TAX_CLASSIFICATIONS = [
		{ value: 'individual', label: 'Individual' },
		{ value: 'sole_proprietor', label: 'Sole proprietor' },
		{ value: 'llc_s_corp', label: 'LLC — taxed as S-corp' },
		{ value: 'llc_c_corp', label: 'LLC — taxed as C-corp' },
		{ value: 'llc_partnership', label: 'LLC — taxed as partnership' },
		{ value: 'c_corp', label: 'C-corporation' },
		{ value: 's_corp', label: 'S-corporation' },
		{ value: 'partnership', label: 'Partnership' },
		{ value: 'trust', label: 'Trust / estate' },
		{ value: 'other', label: 'Other' }
	];

	let {
		row,
		year,
		currency,
		onclose,
		onupdated
	}: {
		row: Vendor1099Row;
		year: number;
		currency: string;
		onclose: () => void;
		/** Fired after any successful mutation so the caller can patch the row
		 *  in the report table without a full re-fetch. */
		onupdated: (patch: Partial<Vendor1099Row>) => void;
	} = $props();

	let taxId = $state(row.tax_id ?? '');
	let classification = $state(row.tax_classification ?? '');
	let eligible = $state(row.is_1099_eligible);
	let w9OnFile = $state(row.w9_on_file);
	let w9ReceivedDate = $state(row.w9_received_date);
	let tinVerified = $state(row.tin_verified);

	let fileInput = $state<HTMLInputElement | undefined>(undefined);
	let pendingFileName = $state<string | null>(null);

	let saving = $state(false);
	let uploading = $state(false);
	let verifying = $state(false);
	let downloading = $state(false);
	let tinResult = $state<TinValidationResult | null>(null);

	// A vendor's reportable total is split across boxes by GL account, and the
	// PDF route 400s when the requested form has no box on this row. So offer the
	// forms this row actually has money on, rather than assuming 1099-NEC. A row
	// with no allocations at all (a tenant with no mapping configured) keeps the
	// prior behaviour: NEC, gated on the reportable total.
	let availableForms = $derived.by(() => {
		const withMoney = (row.box_allocations ?? []).filter((a) => isPositiveAmount(a.amount));
		if (withMoney.length === 0) return isPositiveAmount(row.ytd_paid) ? ['1099-NEC'] : [];
		return [...new Set(withMoney.map((a) => a.form_type))].sort();
	});
	let selectedForm = $state('1099-NEC');
	$effect(() => {
		if (availableForms.length > 0 && !availableForms.includes(selectedForm)) {
			selectedForm = availableForms[0];
		}
	});
	let canDownloadPdf = $derived(availableForms.length > 0);

	function pickFile() {
		fileInput?.click();
	}

	function onFileChosen(e: Event) {
		const f = (e.target as HTMLInputElement).files?.[0];
		pendingFileName = f?.name ?? null;
	}

	async function saveFields() {
		saving = true;
		try {
			const profile = await updateVendorTaxFields(row.vendor_id, {
				tax_classification: classification || null,
				is_1099_eligible: eligible,
				tax_id: taxId || null
			});
			w9OnFile = profile.w9_on_file;
			w9ReceivedDate = profile.w9_received_date;
			tinVerified = !!profile.tin_verified_at;
			toast(m('tax.vendorModal.toast.fieldsSaved'), 'success');
			onupdated({
				tax_id: profile.tax_id,
				tax_classification: profile.tax_classification,
				is_1099_eligible: profile.is_1099_eligible,
				w9_received_date: profile.w9_received_date,
				w9_on_file: profile.w9_on_file,
				tin_verified: !!profile.tin_verified_at
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('tax.vendorModal.toast.fieldsSaveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function uploadW9() {
		const file = fileInput?.files?.[0];
		if (!file) return;
		uploading = true;
		try {
			const profile = await uploadVendorW9(row.vendor_id, file, {
				tax_classification: classification || undefined,
				is_1099_eligible: eligible
			});
			taxId = profile.tax_id ?? '';
			classification = profile.tax_classification ?? '';
			eligible = profile.is_1099_eligible;
			w9OnFile = profile.w9_on_file;
			w9ReceivedDate = profile.w9_received_date;
			tinVerified = !!profile.tin_verified_at;
			pendingFileName = null;
			if (fileInput) fileInput.value = '';
			toast(m('tax.vendorModal.toast.w9Uploaded'), 'success');
			onupdated({
				tax_id: profile.tax_id,
				tax_classification: profile.tax_classification,
				is_1099_eligible: profile.is_1099_eligible,
				w9_received_date: profile.w9_received_date,
				w9_on_file: profile.w9_on_file,
				tin_verified: !!profile.tin_verified_at
			});
		} catch (err) {
			toast(err instanceof Error ? err.message : m('tax.vendorModal.toast.w9UploadFailed'), 'error');
		} finally {
			uploading = false;
		}
	}

	async function verifyTin() {
		verifying = true;
		tinResult = null;
		try {
			const res = await verifyVendorTin(row.vendor_id, taxId || undefined);
			taxId = res.tax_id ?? taxId;
			tinVerified = !!res.tin_verified_at;
			tinResult = res.tin_validation;
			onupdated({ tax_id: res.tax_id, tin_verified: !!res.tin_verified_at });
			if (res.tin_validation.verdict !== 'valid') {
				toast(m('tax.vendorModal.toast.tinInvalid'), 'error');
			} else {
				toast(m('tax.vendorModal.toast.tinValid'), 'success');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : m('tax.vendorModal.toast.tinVerifyFailed'), 'error');
		} finally {
			verifying = false;
		}
	}

	function triggerDownload(blob: Blob, filename: string) {
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.click();
		URL.revokeObjectURL(url);
	}

	async function downloadPdf() {
		downloading = true;
		try {
			const blob = await downloadVendor1099Pdf(row.vendor_id, year, selectedForm);
			triggerDownload(blob, `${selectedForm}-${year}-${row.vendor_name}.pdf`);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('tax.vendorModal.toast.pdfFailed'), 'error');
		} finally {
			downloading = false;
		}
	}
</script>

<Modal
	open={true}
	ariaLabel={m('tax.vendorModal.aria', { vendor: row.vendor_name })}
	title={m('tax.vendorModal.title', { vendor: row.vendor_name })}
	width="md"
	{onclose}
>
	<p class="ytd-line">
		{m('tax.vendorModal.ytdLine', { year })}
		<strong><Money amount={row.ytd_paid} {currency} /></strong>
		<HelpTip term="form-1099" />
	</p>

	<form onsubmit={(e) => { e.preventDefault(); saveFields(); }}>
		<div class="form-grid">
			<label>
				<span>{m('tax.vendorModal.field.taxId')}</span>
				<input type="text" bind:value={taxId} maxlength="50" placeholder="XX-XXXXXXX" />
			</label>
			<label>
				<span>{m('tax.vendorModal.field.classification')}</span>
				<select bind:value={classification}>
					<option value="">{m('tax.vendorModal.field.classificationPlaceholder')}</option>
					{#each TAX_CLASSIFICATIONS as c (c.value)}
						<option value={c.value}>{c.label}</option>
					{/each}
				</select>
			</label>
			<label class="full-width checkbox-label">
				<input type="checkbox" bind:checked={eligible} />
				<span>{m('tax.vendorModal.field.eligible')}</span>
			</label>
		</div>

		<div class="status-row">
			<span class="status-item" class:on={w9OnFile}>
				{w9OnFile
					? m('tax.vendorModal.status.w9On', { date: formatDate(w9ReceivedDate) })
					: m('tax.vendorModal.status.w9Missing')}
			</span>
			<span class="status-item" class:on={tinVerified}>
				{tinVerified ? m('tax.vendorModal.status.tinVerified') : m('tax.vendorModal.status.tinUnverified')}
			</span>
		</div>

		<div class="w9-section">
			<span class="section-title">{m('tax.vendorModal.section.w9')}</span>
			<div class="w9-row">
				<input
					type="file"
					accept=".pdf,.png,.jpg,.jpeg,.tiff"
					bind:this={fileInput}
					onchange={onFileChosen}
					hidden
				/>
				<button type="button" class="btn-outline" onclick={pickFile}>
					{pendingFileName ?? m('tax.vendorModal.chooseFile')}
				</button>
				<button
					type="button"
					class="btn-outline"
					disabled={!pendingFileName || uploading}
					onclick={uploadW9}
				>
					{uploading ? m('tax.vendorModal.uploading') : m('tax.vendorModal.uploadW9')}
				</button>
			</div>
		</div>

		<div class="tin-section">
			<span class="section-title">{m('tax.vendorModal.section.tin')}</span>
			<button type="button" class="btn-outline" disabled={verifying || !taxId.trim()} onclick={verifyTin}>
				{verifying ? m('tax.vendorModal.verifying') : m('tax.vendorModal.verifyTin')}
			</button>
			{#if tinResult}
				<p class="tin-result" class:invalid={tinResult.verdict !== 'valid'}>
					{tinResult.verdict === 'valid'
						? m('tax.vendorModal.tinResult.valid')
						: m('tax.vendorModal.tinResult.invalid', {
								reason: tinResult.reason_code ?? m('tax.vendorModal.tinResult.unknownReason')
							})}
				</p>
			{/if}
		</div>

		<div class="pdf-section">
			<span class="section-title">{m('tax.vendorModal.section.pdf')}</span>
			{#if availableForms.length > 1}
				<select
					class="pdf-form-select"
					bind:value={selectedForm}
					aria-label={m('tax.fileModal.formType')}
				>
					{#each availableForms as form (form)}
						<option value={form}>{form}</option>
					{/each}
				</select>
			{/if}
			<button
				type="button"
				class="btn-outline"
				disabled={!canDownloadPdf || downloading}
				title={canDownloadPdf ? undefined : m('tax.vendorModal.noReportablePayments', { year })}
				onclick={downloadPdf}
			>
				{downloading ? m('tax.vendorModal.downloading') : m('tax.vendorModal.downloadPdf', { year })}
			</button>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('tax.vendorModal.close')}</button>
			<button type="submit" class="btn-primary" disabled={saving}>
				{saving ? m('common.saving') : m('common.save')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.btn-outline {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		padding: 8px 18px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-outline:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-outline:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.ytd-line {
		font-size: 0.85rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
		margin-bottom: 12px;
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

	.checkbox-label {
		flex-direction: row !important;
		align-items: center;
		gap: 8px !important;
	}

	.form-grid input[type='text'],
	.form-grid select {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	.status-row {
		display: flex;
		gap: 10px;
		flex-wrap: wrap;
		margin-bottom: 14px;
	}

	.status-item {
		font-size: 0.76rem;
		padding: 3px 9px;
		border-radius: 10px;
		background: var(--danger-tint);
		color: var(--danger-on-tint);
		font-weight: 600;
	}

	.status-item.on {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.section-title {
		display: block;
		font-size: 0.78rem;
		font-weight: 600;
		color: var(--text-muted);
		margin-bottom: 6px;
	}

	.w9-section,
	.tin-section,
	.pdf-section {
		padding: 12px 0;
		border-top: 1px solid var(--border);
	}

	.pdf-form-select {
		margin-right: 8px;
		padding: 6px 8px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font-size: 13px;
	}

	.w9-row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}

	.tin-result {
		margin: 8px 0 0;
		font-size: 0.82rem;
		color: var(--success);
	}

	.tin-result.invalid {
		color: var(--danger);
	}
</style>
