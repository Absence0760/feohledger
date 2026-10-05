<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { INVOICE_STATUSES, INVOICE_STATUS_LABEL_KEYS } from '#lib/types/invoice.ts';
	import type { AdvancedSearchFilters, InvoiceStatus } from '#lib/types/invoice.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let {
		filters,
		onclose,
		onapply,
	}: {
		filters: AdvancedSearchFilters;
		onclose: () => void;
		onapply: (filters: AdvancedSearchFilters) => void;
	} = $props();

	let vendor = $state(filters.vendor);
	let invoice_number = $state(filters.invoice_number);
	let po_number = $state(filters.po_number);
	let description = $state(filters.description);
	let amount_min = $state(filters.amount_min);
	let amount_max = $state(filters.amount_max);
	let due_date_from = $state(filters.due_date_from);
	let due_date_to = $state(filters.due_date_to);
	let statuses = $state<InvoiceStatus[]>([...filters.statuses]);

	function toggleStatus(s: InvoiceStatus) {
		if (statuses.includes(s)) {
			statuses = statuses.filter((v) => v !== s);
		} else {
			statuses = [...statuses, s];
		}
	}

	function apply() {
		onapply({
			vendor,
			invoice_number,
			po_number,
			description,
			amount_min,
			amount_max,
			due_date_from,
			due_date_to,
			statuses,
		});
		onclose();
	}

	function clear() {
		vendor = '';
		invoice_number = '';
		po_number = '';
		description = '';
		amount_min = '';
		amount_max = '';
		due_date_from = '';
		due_date_to = '';
		statuses = [];
	}
</script>

<Modal ariaLabel="Advanced search" title={m('advancedSearch.title')} onclose={onclose}>
	<form onsubmit={(e) => { e.preventDefault(); apply(); }}>
		<div class="form-grid">
			<label>
				<span>{m('advancedSearch.vendor')}</span>
				<input type="text" placeholder={m('advancedSearch.vendorPlaceholder')} bind:value={vendor} />
			</label>
			<label>
				<span>{m('advancedSearch.invoiceNumber')}</span>
				<input type="text" placeholder={m('advancedSearch.invoiceNumberPlaceholder')} bind:value={invoice_number} />
			</label>
			<label>
				<span>{m('advancedSearch.poNumber')}</span>
				<input type="text" placeholder={m('advancedSearch.poNumberPlaceholder')} bind:value={po_number} />
			</label>
			<label>
				<span>{m('advancedSearch.description')}</span>
				<input type="text" placeholder={m('advancedSearch.descriptionPlaceholder')} bind:value={description} />
			</label>
			<label>
				<span>{m('advancedSearch.amountMin')}</span>
				<input type="number" step="0.01" placeholder="0.00" bind:value={amount_min} />
			</label>
			<label>
				<span>{m('advancedSearch.amountMax')}</span>
				<input type="number" step="0.01" placeholder={m('advancedSearch.amountMaxPlaceholder')} bind:value={amount_max} />
			</label>
			<label>
				<span>{m('advancedSearch.dueDateFrom')}</span>
				<input type="date" bind:value={due_date_from} />
			</label>
			<label>
				<span>{m('advancedSearch.dueDateTo')}</span>
				<input type="date" bind:value={due_date_to} />
			</label>
		</div>

		<fieldset>
			<legend>{m('advancedSearch.status')}</legend>
			<div class="status-chips">
				{#each INVOICE_STATUSES as s}
					<button
						type="button"
						class="status-chip"
						class:selected={statuses.includes(s)}
						onclick={() => toggleStatus(s)}
					>
						{m(INVOICE_STATUS_LABEL_KEYS[s])}
					</button>
				{/each}
			</div>
		</fieldset>

		<footer>
			<button type="button" class="btn-clear" onclick={clear}>{m('advancedSearch.clearAll')}</button>
			<div class="footer-right">
				<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
				<button type="submit" class="btn-apply">{m('advancedSearch.applyFilters')}</button>
			</div>
		</footer>
	</form>
</Modal>

<style>
	/* Shell, header, and base input styling come from the shared Modal
	   component + global `.modal*` CSS in app.css. Only the bits unique to
	   this dialog — the two-column field grid, the status chip picker, and
	   the Clear-All footer — are scoped here. */
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
	}

	/* Number/date inputs carry a UA intrinsic min-width; without these the
	   right column overflows the dialog and adds a horizontal scrollbar. */
	.form-grid label {
		min-width: 0;
	}

	.form-grid input {
		width: 100%;
		min-width: 0;
	}

	fieldset {
		border: none;
		margin: 0;
		padding: 0;
	}

	legend {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		margin-bottom: 8px;
	}

	.status-chips {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}

	.status-chip {
		padding: 5px 12px;
		border-radius: 16px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text-muted);
		font-size: 0.8rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s;
		font-family: inherit;
	}

	.status-chip:hover {
		border-color: var(--accent);
		color: var(--text);
	}

	.status-chip.selected {
		background: var(--accent-strong);
		color: #fff;
		border-color: var(--accent-strong);
	}

	footer {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding-top: 18px;
		border-top: 1px solid var(--border);
		margin-top: 6px;
	}

	.footer-right {
		display: flex;
		gap: 10px;
	}

	.btn-clear,
	.btn-cancel,
	.btn-apply {
		padding: 8px 18px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		border: 1px solid var(--border);
		font-family: inherit;
	}

	.btn-clear {
		background: none;
		color: var(--text-muted);
		border-color: transparent;
	}

	.btn-clear:hover {
		color: var(--text);
		text-decoration: underline;
	}

	.btn-cancel {
		background: var(--surface);
		color: var(--text-muted);
	}

	.btn-cancel:hover {
		background: var(--bg);
	}

	.btn-apply {
		background: var(--accent-strong);
		color: #fff;
		border-color: var(--accent-strong);
	}

	.btn-apply:hover {
		opacity: 0.9;
	}
</style>
