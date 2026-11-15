<script lang="ts">
	// "Find this vendor in the ERP" (decisions §415): search the connected ERP's
	// whole contact list for a supplier no sync can link yet — a Xero customer
	// with no bill or PO from us carries neither flag the vendor walk reads —
	// and link the record the steward picks. `vendor.manage` only (the parent
	// mounts this only for it). The server searches by the typed name (the
	// vendor's own when blank) and by the vendor's stored tax number, which it
	// reads itself; a result says only whether the tax number matches. Link is
	// an armed two-click, like Unlink beside it, because it decides which ERP
	// record this vendor's bills post to.
	import Badge from '#lib/components/ui/Badge.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { linkVendorErpRecord, searchVendorErpRecords } from '#lib/api/vendors.ts';
	import {
		erpCandidateLinkable,
		type VendorErpCandidate,
		type VendorErpLinkResponse
	} from '#lib/types/vendor.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';

	let {
		vendorId,
		vendorName,
		disabled = false,
		onlinked
	}: {
		vendorId: string;
		vendorName: string;
		/** Another action in the dialog is running. */
		disabled?: boolean;
		onlinked: (res: VendorErpLinkResponse) => void;
	} = $props();

	const uid = $props.id();
	let query = $state('');
	let searching = $state(false);
	let failed = $state<string | null>(null);
	let candidates = $state<VendorErpCandidate[] | null>(null);
	let truncated = $state(false);
	let armed = $state<string | null>(null);
	let linking = $state<string | null>(null);

	// A different vendor starts a fresh search, prefilled with its name.
	$effect(() => {
		query = vendorName;
		void vendorId;
		candidates = null;
		failed = null;
		armed = null;
	});

	async function search(e: SubmitEvent) {
		e.preventDefault();
		// The vendor this search is for: an answer that arrives after the
		// dialog moved to another vendor is dropped, never shown under it.
		const id = vendorId;
		searching = true;
		failed = null;
		armed = null;
		try {
			const res = await searchVendorErpRecords(id, query.trim() || null);
			if (id !== vendorId) return;
			candidates = res.candidates;
			truncated = res.truncated;
		} catch (err) {
			if (id !== vendorId) return;
			candidates = null;
			failed = errorMessage(err, m('vendors.modal.erp.find.failed'));
		} finally {
			searching = false;
		}
	}

	async function link(c: VendorErpCandidate) {
		if (armed !== c.erp_vendor_id) {
			armed = c.erp_vendor_id;
			return;
		}
		armed = null;
		linking = c.erp_vendor_id;
		try {
			const res = await linkVendorErpRecord(vendorId, c.erp_vendor_id);
			candidates = null;
			toast(m('vendors.modal.erp.find.linkedToast', { name: c.name }), 'success');
			onlinked(res);
		} catch (err) {
			toast(errorMessage(err, m('vendors.modal.erp.find.linkFailed')), 'error');
		} finally {
			linking = null;
		}
	}
</script>

<div class="erp-find" data-testid="vendor-erp-find">
	<h4 id="{uid}-heading">{m('vendors.modal.erp.find.heading')}</h4>
	<p class="hint muted" id="{uid}-hint">{m('vendors.modal.erp.find.hint')}</p>
	<form class="find-row" onsubmit={search}>
		<label class="visually-hidden" for="{uid}-query">{m('vendors.modal.erp.find.label')}</label>
		<input
			id="{uid}-query"
			type="search"
			maxlength="100"
			bind:value={query}
			aria-describedby="{uid}-hint"
			data-testid="vendor-erp-find-query"
		/>
		<RowAction type="submit" disabled={searching || disabled}>
			{searching ? m('vendors.modal.erp.find.searching') : m('vendors.modal.erp.find.search')}
		</RowAction>
	</form>

	<div role="status" data-testid="vendor-erp-find-status">
		{#if failed}
			<p class="failure-text">{failed}</p>
		{:else if candidates && candidates.length === 0}
			<p class="muted">{m('vendors.modal.erp.find.none')}</p>
		{:else if candidates && truncated}
			<p class="muted">{m('vendors.modal.erp.find.truncated')}</p>
		{/if}
	</div>

	{#if candidates && candidates.length}
		<div
			class="table-scroll"
			role="region"
			aria-label={m('vendors.modal.erp.find.results')}
			tabindex="0"
		>
			<table class="find-table">
				<thead>
					<tr>
						<th scope="col">{m('vendors.modal.erp.find.colName')}</th>
						<th scope="col">{m('vendors.modal.erp.find.colEmail')}</th>
						<th scope="col">{m('vendors.modal.erp.find.colMatch')}</th>
						<th scope="col" class="actions-col">
							<span class="visually-hidden">{m('vendors.modal.erp.colAction')}</span>
						</th>
					</tr>
				</thead>
				<tbody>
					{#each candidates as c (c.erp_vendor_id)}
						<tr data-testid="vendor-erp-candidate">
							<th scope="row">{c.name}</th>
							<td class="muted">{c.email ?? ''}</td>
							<td>
								{#if c.tax_id_matches}
									<Badge tone="success" variant="tax-match">{m('vendors.modal.erp.find.taxMatch')}</Badge>
								{/if}
								{#if !c.active}
									<Badge tone="neutral">{m('vendors.modal.erp.find.inactive')}</Badge>
								{:else if c.linked_elsewhere}
									<Badge tone="warning">{m('vendors.modal.erp.find.linkedElsewhere')}</Badge>
								{/if}
							</td>
							<td class="actions">
								{#if erpCandidateLinkable(c)}
									<RowAction
										variant="accent"
										armed={armed === c.erp_vendor_id}
										disabled={linking !== null || disabled}
										ariaLabel={m('vendors.modal.erp.find.linkAria', { name: c.name })}
										onclick={() => link(c)}
									>
										{#if linking === c.erp_vendor_id}
											{m('vendors.modal.erp.find.linking')}
										{:else if armed === c.erp_vendor_id}
											{m('vendors.modal.erp.find.confirmLink')}
										{:else}
											{m('vendors.modal.erp.find.link')}
										{/if}
									</RowAction>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>

<style>
	.erp-find {
		margin-top: 16px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}
	.erp-find h4 {
		margin: 0 0 4px;
		font-size: 0.9rem;
	}
	.hint {
		font-size: 0.8rem;
		margin: 0 0 8px;
	}
	.muted {
		color: var(--text-muted);
	}
	.failure-text {
		color: var(--danger);
	}
	.find-row {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	.find-row input {
		flex: 1 1 12rem;
		min-width: 0;
	}
	.table-scroll {
		overflow-x: auto;
		max-width: 100%;
	}
	.find-table {
		width: 100%;
		min-width: 30rem;
		border-collapse: collapse;
		margin-top: 8px;
		font-size: 0.82rem;
	}
	.find-table th[scope='col'] {
		text-align: left;
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 4px 10px 6px;
		border-bottom: 1px solid var(--border);
	}
	.find-table td,
	.find-table th[scope='row'] {
		padding: 8px 10px;
		border-bottom: 1px solid var(--border);
		text-align: left;
		vertical-align: top;
		overflow-wrap: anywhere;
	}
	.find-table th[scope='row'] {
		font-weight: 500;
	}
	.actions {
		white-space: nowrap;
	}
</style>
