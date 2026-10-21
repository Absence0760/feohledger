<script lang="ts">
	// Vendor consolidation — "Merge into canonical" UI.
	//
	// Surfaces the advisory duplicate/similar-vendor clusters from
	// `GET /api/enrichment/vendors/consolidation-suggestions` and lets an
	// authorized steward fold a cluster's duplicates into its canonical vendor
	// via `POST /api/enrichment/vendors/consolidation/merge`. The merge is
	// soft-retire-irreversible (duplicates flip to status=inactive and their FKs
	// re-home), so the action is behind an explicit two-step confirm.
	//
	// Gating: the mutate action is gated on the granular `vendor.manage`
	// permission (`auth.can`), NOT a role check — mirrors the backend
	// `require_permission(PERM_VENDOR_MANAGE)`. The modal is only opened from a
	// header action the page already gates the same way, but the per-cluster
	// Merge button re-checks so the control and the API gate can't drift.
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { PERM_VENDOR_MANAGE } from '#lib/types/admin.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { toastCardRevocations } from '#lib/utils/cardRevocationToast.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import {
		getVendorConsolidationSuggestions,
		mergeVendorConsolidation
	} from '#lib/api/vendors.ts';
	import { vendorStatusLabelKey, type VendorCluster } from '#lib/types/vendor.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
	import { consolidationReasonLabel } from '#lib/utils/vendorConsolidation.ts';

	let {
		onclose,
		onmerged
	}: {
		onclose: () => void;
		// Fired after a successful merge so the parent can refresh the vendor list.
		onmerged: () => void;
	} = $props();

	const canMerge = $derived(auth.can(PERM_VENDOR_MANAGE));

	let clusters = $state<VendorCluster[]>([]);
	let loading = $state(true);
	let loadError = $state(false);
	let truncated = $state(false);
	// The cluster id currently armed for a confirm, or in-flight ('' = none).
	let confirmId = $state<number | null>(null);
	let mergingId = $state<number | null>(null);

	$effect(() => {
		load();
	});

	// The ONE call site is the mount `$effect` above, and that is what makes
	// this the one list surface in the app with no `createRequestSequencer()`
	// (`frontend/CLAUDE.md` § Sequencing list fetches). `doMerge` drops a
	// cluster from `clusters` in place with no fetch of its own — the exact
	// shape that gets clobbered elsewhere — but it is only reachable from a
	// button rendered off `clusters`, so no GET can still be in flight when it
	// runs, and there is no create path to race the first load.
	//
	// Adding a second trigger breaks that immediately: a `load()` after the
	// merge on line 96 to re-list, or a Retry control on the `loadError` state.
	// Wire the sequencer in the same change if you add either.
	async function load() {
		loading = true;
		loadError = false;
		try {
			const res = await getVendorConsolidationSuggestions();
			clusters = res.clusters;
			truncated = res.truncated;
		} catch {
			// Stated in place below — a toast is never the only sign of a failed
			// load, and a persistent error toast repeating it would be noise
			// (decisions §307).
			loadError = true;
		} finally {
			loading = false;
		}
	}

	function canonicalOf(c: VendorCluster) {
		return c.members.find((member) => member.is_canonical) ?? c.members[0];
	}

	function duplicatesOf(c: VendorCluster) {
		return c.members.filter((member) => member.vendor_id !== c.canonical_vendor_id);
	}

	function errMsg(err: unknown, fallback: string): string {
		const e = err as { detail?: string; message?: string } | null;
		return e?.detail ?? e?.message ?? fallback;
	}

	async function doMerge(c: VendorCluster) {
		const dupes = duplicatesOf(c).map((member) => member.vendor_id);
		if (dupes.length === 0) return;
		mergingId = c.cluster_id;
		confirmId = null;
		try {
			const res = await mergeVendorConsolidation(c.canonical_vendor_id, dupes);
			const merged = res.deactivated_vendor_ids.length;
			toast(
				merged > 0
					? m('vendors.consolidation.toast.merged', {
							n: merged,
							records: res.total_reassigned
						})
					: m('vendors.consolidation.toast.alreadyMerged'),
				'success'
			);
			toastCardRevocations(res.card_revocations);
			// Drop the merged cluster from the list so it can't be acted on twice.
			clusters = clusters.filter((x) => x.cluster_id !== c.cluster_id);
			onmerged();
		} catch (err) {
			// Surface the backend's 4xx reason (self-merge / cross-entity / unknown).
			toast(errMsg(err, m('vendors.consolidation.toast.mergeFailed')), 'error');
		} finally {
			mergingId = null;
		}
	}
</script>

<Modal
	open
	ariaLabel={m('vendors.consolidation.dialogAria')}
	title={m('vendors.consolidation.title')}
	width="lg"
	{onclose}
>
	<p class="hint muted">{m('vendors.consolidation.hint')}</p>

	{#if loading}
		<p class="muted state">{m('vendors.consolidation.loading')}</p>
	{:else if loadError}
		<p class="muted state" role="alert">{m('vendors.consolidation.loadFailed')}</p>
	{:else if clusters.length === 0}
		<p class="muted state">{m('vendors.consolidation.empty')}</p>
	{:else}
		{#if truncated}
			<p class="muted truncated-note">{m('vendors.consolidation.truncated')}</p>
		{/if}
		<ul class="clusters">
			{#each clusters as c (c.cluster_id)}
				{@const canon = canonicalOf(c)}
				{@const dupes = duplicatesOf(c)}
				<li class="cluster" data-cluster={c.cluster_id}>
					<div class="cluster-head">
						<div class="reasons">
							{#each c.reasons as r (r)}
								<span class="reason-pill"
									>{consolidationReasonLabel(r, m, getActiveFormatLocale())}</span
								>
							{/each}
						</div>
						{#if canMerge}
							{#if confirmId === c.cluster_id}
								<div class="confirm-row">
									<span class="confirm-q"
										>{m('vendors.consolidation.confirmQuestion', {
											n: dupes.length,
											name: canon.name
										})}</span
									>
									<RowAction
										variant="danger"
										armed
										onclick={() => doMerge(c)}
										disabled={mergingId !== null}
									>
										{mergingId === c.cluster_id
											? m('vendors.consolidation.merging')
											: m('vendors.consolidation.confirm')}
									</RowAction>
									<RowAction onclick={() => (confirmId = null)} disabled={mergingId !== null}>
										{m('common.cancel')}
									</RowAction>
								</div>
							{:else}
								<RowAction
									onclick={() => (confirmId = c.cluster_id)}
									disabled={mergingId !== null || dupes.length === 0}
									ariaLabel={m('vendors.consolidation.mergeAria', { name: canon.name })}
								>
									{m('vendors.consolidation.mergeIntoCanonical')}
								</RowAction>
							{/if}
						{/if}
					</div>

					<!-- Six columns do not reflow at 320px: the table scrolls inside
					     this named, focusable region instead of pushing the dialog
					     sideways (WCAG 1.4.10, 2.1.1 — same rule as ui/DataTable). -->
					<div
						class="cluster-scroll"
						role="region"
						aria-label={m('vendors.consolidation.clusterRegion', { name: canon.name })}
						tabindex="0"
					>
					<table class="cluster-table">
						<thead>
							<tr>
								<th>{m('vendors.col.vendor')}</th>
								<th>{m('vendors.col.code')}</th>
								<th>{m('vendors.consolidation.col.taxId')}</th>
								<th>{m('vendors.col.status')}</th>
								<th class="num">{m('vendors.col.invoices')}</th>
								<th>{m('vendors.consolidation.col.role')}</th>
							</tr>
						</thead>
						<tbody>
							{#each c.members as member (member.vendor_id)}
								{@const statusKey = member.status ? vendorStatusLabelKey(member.status) : null}
								<tr class:canonical-row={member.is_canonical}>
									<td class="m-name">{member.name}</td>
									<td class="mono muted">{member.code ?? '—'}</td>
									<td class="mono muted">{member.tax_id_masked ?? '—'}</td>
									<td class="muted">{statusKey ? m(statusKey) : (member.status ?? '—')}</td>
									<td class="num mono">{member.invoice_count}</td>
									<td>
										{#if member.is_canonical}
											<span class="role-badge canonical"
												>{m('vendors.consolidation.role.canonical')}</span
											>
										{:else}
											<span class="role-badge duplicate"
												>{m('vendors.consolidation.role.duplicate')}</span
											>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
					</div>
				</li>
			{/each}
		</ul>

		{#if !canMerge}
			<p class="muted state">{m('vendors.consolidation.needsPermission')}</p>
		{/if}
	{/if}

	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('vendors.consolidation.close')}</button>
	</div>
</Modal>

<style>
	.hint {
		font-size: 0.82rem;
		line-height: 1.45;
		margin: 0 0 12px;
	}
	.muted {
		color: var(--text-muted);
	}
	.state {
		font-size: 0.85rem;
		padding: 14px 0;
	}
	.truncated-note {
		font-size: 0.78rem;
		margin: 0 0 8px;
	}
	.clusters {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	.cluster {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 12px 14px;
	}
	.cluster-head {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 10px;
	}
	.reasons {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.reason-pill {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.72rem;
		font-weight: 500;
		background: var(--bg);
		color: var(--text-muted);
	}
	.confirm-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	.confirm-q {
		font-size: 0.82rem;
		font-weight: 500;
	}
	.cluster-scroll {
		overflow-x: auto;
	}
	.cluster-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.cluster-table th {
		text-align: left;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		padding: 4px 10px 6px;
		border-bottom: 1px solid var(--border);
	}
	.cluster-table th.num,
	.cluster-table td.num {
		text-align: right;
	}
	.cluster-table td {
		padding: 7px 10px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
	}
	.cluster-table tr:last-child td {
		border-bottom: none;
	}
	.canonical-row td {
		background: color-mix(in srgb, var(--success) 5%, transparent);
	}
	.m-name {
		font-weight: 500;
	}
	.role-badge {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 10px;
		font-size: 0.72rem;
		font-weight: 500;
	}
	/* Not `<Badge>`: this marks a member's ROLE inside a cluster, not a
	   lifecycle status, and it sits in a dense member table beside the vendor
	   name — the same reason `ui/ScreeningBadge` keeps its own quieter metrics
	   (0.72rem, weight 500, sentence case) rather than shouting in uppercase.
	   Only the colour literals are retired to the palette pair. */
	.role-badge.canonical {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}
	.role-badge.duplicate {
		background: var(--bg);
		color: var(--text-muted);
	}
</style>
