<script lang="ts">
	import type { Invoice } from '$lib/types/invoice';
	import { api } from '$lib/api';
	import { listGlAccounts, listInvoiceChart } from '$lib/api/glAccounts';
	import { glAccountOptionLabel, type GlAccountOption } from '$lib/types/glAccount';
	import Modal from '$lib/components/ui/Modal.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import { DEFAULT_CURRENCY } from '$lib/utils/money';

	let { onclose, onsaved }: { onclose: () => void; onsaved: (invoice: Invoice) => void } =
		$props();

	let vendor = $state('');
	let invoice_number = $state('');
	let amount = $state<number | null>(null);
	// Defaults to the org's resolved currency (`orgCurrency`) rather than
	// hardcoding USD — a UK/non-US tenant's manual entries should start in their
	// own currency, not silently assume the platform default. The store answers
	// `null` when nothing resolves, so the platform default is named here, where
	// a form genuinely needs a value (decisions §200).
	let currency = $state(orgCurrency.currency ?? DEFAULT_CURRENCY);
	let invoice_date = $state('');
	let due_date = $state('');
	let po_number = $state('');
	let payment_terms = $state('');
	let gl_account = $state('');
	let cost_center = $state('');
	let file = $state<File | null>(null);

	let glAccounts = $state<GlAccountOption[]>([]);
	let saving = $state(false);

	$effect(() => {
		(async () => {
			// The entity list first: it names the entity this invoice will be
			// filed under (the selection, else the default — the backend's write
			// rule), and the picker offers THAT entity's chart. In the
			// consolidated view the header scope is every subsidiary's chart at
			// once, which is how another entity's code used to be offered here.
			// It also names each option's `entity_id` for the scope suffix
			// `glAccountOptionLabel` appends.
			await entityStore.ensureLoaded();
			try {
				const target = entityStore.writeEntityId;
				// Unknown only when the entity list failed to load: fall back to
				// the header-scoped list and let the backend's check decide.
				glAccounts = target ? await listInvoiceChart(target) : await listGlAccounts();
			} catch {
				// GL catalog is a convenience dropdown — fall back to free text.
			}
		})();
	});

	const canSubmit = $derived(vendor.trim() !== '' && invoice_number.trim() !== '' && !!amount && amount > 0);

	function handleFileChange(e: Event) {
		const input = e.target as HTMLInputElement;
		file = input.files?.[0] ?? null;
	}

	async function handleSubmit() {
		if (!canSubmit) return;
		saving = true;
		try {
			const invoice = await api.post<Invoice>('/api/invoices', {
				vendor: vendor.trim(),
				invoice_number: invoice_number.trim(),
				amount,
				currency: currency.trim() || DEFAULT_CURRENCY,
				invoice_date: invoice_date || null,
				due_date: due_date || null,
				po_number: po_number.trim() || null,
				payment_terms: payment_terms.trim() || null,
				gl_account: gl_account.trim() || null,
				cost_center: cost_center.trim() || null
			});

			if (file) {
				try {
					await api.upload<Invoice>(`/api/invoices/${invoice.id}/file`, file);
				} catch (err) {
					// The invoice itself was created successfully — a rejected file
					// (bad type / oversized) shouldn't look like the whole action failed.
					toast(
						err instanceof Error ? err.message : m('invoices.createModal.toast.fileFailed'),
						'error'
					);
				}
			}

			toast(m('invoices.createModal.toast.created'), 'success');
			onsaved(invoice);
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.createModal.toast.createFailed'), 'error');
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('invoices.createModal.aria')}
	title={m('invoices.createModal.title')}
	width="lg"
	{onclose}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<div class="form-grid">
			<label>
				<span>{m('invoices.modal.field.vendor')} <em class="required">*</em></span>
				<input type="text" bind:value={vendor} required />
			</label>
			<label>
				<span>{m('invoices.modal.field.invoiceNumber')} <em class="required">*</em></span>
				<input type="text" bind:value={invoice_number} required />
			</label>
			<label>
				<span>{m('invoices.modal.field.amount')} <em class="required">*</em></span>
				<input
					type="number"
					step="0.01"
					min="0"
					value={amount ?? ''}
					oninput={(e) => {
						const v = e.currentTarget.value;
						amount = v === '' ? null : parseFloat(v);
					}}
					required
				/>
			</label>
			<label>
				<span>{m('invoices.modal.field.currency')}</span>
				<input type="text" bind:value={currency} maxlength="3" />
			</label>
			<label>
				<span>{m('invoices.modal.field.invoiceDate')}</span>
				<input type="date" bind:value={invoice_date} />
			</label>
			<label>
				<span>{m('invoices.modal.field.dueDate')}</span>
				<input type="date" bind:value={due_date} />
			</label>
			<label>
				<span>{m('invoices.modal.field.poNumber')}</span>
				<input type="text" bind:value={po_number} />
			</label>
			<label>
				<span>{m('invoices.modal.field.paymentTerms')}</span>
				<input
					type="text"
					bind:value={payment_terms}
					placeholder={m('invoices.modal.field.paymentTermsPlaceholder')}
				/>
			</label>
			<label>
				<span>{m('invoices.modal.field.glAccount')}</span>
				{#if glAccounts.length > 0}
					<select bind:value={gl_account}>
						<option value="">{m('invoices.modal.field.glSelect')}</option>
						<!-- Keyed by `id`, not `code`: the chart offered is shared ∪
						     the target entity's own, and an entity may override a
						     shared `6000` with its own — a `code` key would collide.
						     The bound VALUE stays the code, which is what
						     `Invoice.gl_account` (a String(100)) records; see
						     `types/glAccount.ts`. -->
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
					<input
						type="text"
						bind:value={gl_account}
						placeholder={m('invoices.modal.field.glPlaceholder')}
					/>
				{/if}
			</label>
			<label>
				<span>{m('invoices.modal.field.costCenter')}</span>
				<input
					type="text"
					bind:value={cost_center}
					placeholder={m('invoices.modal.field.costCenterPlaceholder')}
				/>
			</label>
			<label class="full-width">
				<span>{m('invoices.createModal.file')}</span>
				<input
					type="file"
					accept=".pdf,.png,.jpg,.jpeg,.tiff"
					aria-label={m('invoices.createModal.file')}
					onchange={handleFileChange}
				/>
			</label>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
				{saving ? m('invoices.createModal.saving') : m('invoices.createModal.create')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px 16px;
	}
	.full-width {
		grid-column: 1 / -1;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 13px;
	}
</style>
