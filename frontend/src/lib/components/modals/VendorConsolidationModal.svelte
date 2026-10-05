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
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import {
		getVendorConsolidationSuggestions,
		mergeVendorConsolidation
	} from '#lib/api/vendors.ts';
	import type { VendorCluster } from '#lib/types/vendor.ts';

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
			loadError = true;
			toast('Failed to load consolidation suggestions', 'error');
		} finally {
			loading = false;
		}
	}

	function canonicalOf(c: VendorCluster) {
		return c.members.find((m) => m.is_canonical) ?? c.members[0];
	}

	function duplicatesOf(c: VendorCluster) {
		return c.members.filter((m) => m.vendor_id !== c.canonical_vendor_id);
	}

	function errMsg(err: unknown, fallback: string): string {
		const e = err as { detail?: string; message?: string } | null;
		return e?.detail ?? e?.message ?? fallback;
	}

	async function doMerge(c: VendorCluster) {
		const dupes = duplicatesOf(c).map((m) => m.vendor_id);
		if (dupes.length === 0) return;
		mergingId = c.cluster_id;
		confirmId = null;
		try {
			const res = await mergeVendorConsolidation(c.canonical_vendor_id, dupes);
			const merged = res.deactivated_vendor_ids.length;
			toast(
				merged > 0
					? `Merged ${merged} duplicate${merged === 1 ? '' : 's'} (${res.total_reassigned} record${
							res.total_reassigned === 1 ? '' : 's'
						} re-homed)`
					: 'Already merged — nothing to do',
				'success'
			);
			// Drop the merged cluster from the list so it can't be acted on twice.
			clusters = clusters.filter((x) => x.cluster_id !== c.cluster_id);
			onmerged();
		} catch (err) {
			// Surface the backend's 4xx reason (self-merge / cross-entity / unknown).
			toast(errMsg(err, 'Merge failed'), 'error');
		} finally {
			mergingId = null;
		}
	}
</script>

<Modal open ariaLabel="Vendor consolidation" title="Merge duplicate vendors" width="lg" {onclose}>
	<p class="hint muted">
		These vendors look like duplicates (matched on tax ID, code, or similar names). Merging folds
		each duplicate's invoices, payments, and other records into the <strong>canonical</strong>
		vendor and retires the duplicates (sets them <em>inactive</em>) — it doesn't delete anything,
		but it can't be undone from here.
	</p>

	{#if loading}
		<p class="muted state">Loading suggestions…</p>
	{:else if loadError}
		<p class="muted state">Couldn't load suggestions. Close and try again.</p>
	{:else if clusters.length === 0}
		<p class="muted state">No likely-duplicate vendors found. Your vendor list looks clean.</p>
	{:else}
		{#if truncated}
			<p class="muted truncated-note">
				Showing the strongest clusters — some were capped for performance.
			</p>
		{/if}
		<ul class="clusters">
			{#each clusters as c (c.cluster_id)}
				{@const canon = canonicalOf(c)}
				{@const dupes = duplicatesOf(c)}
				<li class="cluster" data-cluster={c.cluster_id}>
					<div class="cluster-head">
						<div class="reasons">
							{#each c.reasons as r (r)}
								<span class="reason-pill">{r}</span>
							{/each}
						</div>
						{#if canMerge}
							{#if confirmId === c.cluster_id}
								<div class="confirm-row">
									<span class="confirm-q">Merge {dupes.length} into “{canon.name}”?</span>
									<RowAction
										variant="danger"
										armed
										onclick={() => doMerge(c)}
										disabled={mergingId !== null}
									>
										{mergingId === c.cluster_id ? 'Merging…' : 'Confirm merge'}
									</RowAction>
									<RowAction onclick={() => (confirmId = null)} disabled={mergingId !== null}>
										Cancel
									</RowAction>
								</div>
							{:else}
								<RowAction
									onclick={() => (confirmId = c.cluster_id)}
									disabled={mergingId !== null || dupes.length === 0}
									ariaLabel={`Merge cluster into ${canon.name}`}
								>
									Merge into canonical
								</RowAction>
							{/if}
						{/if}
					</div>

					<table class="cluster-table">
						<thead>
							<tr>
								<th>Vendor</th>
								<th>Code</th>
								<th>Tax ID</th>
								<th>Status</th>
								<th class="num">Invoices</th>
								<th>Role</th>
							</tr>
						</thead>
						<tbody>
							{#each c.members as m (m.vendor_id)}
								<tr class:canonical-row={m.is_canonical}>
									<td class="m-name">{m.name}</td>
									<td class="mono muted">{m.code ?? '—'}</td>
									<td class="mono muted">{m.tax_id_masked ?? '—'}</td>
									<td class="muted">{m.status ?? '—'}</td>
									<td class="num mono">{m.invoice_count}</td>
									<td>
										{#if m.is_canonical}
											<span class="role-badge canonical">Canonical</span>
										{:else}
											<span class="role-badge duplicate">Duplicate</span>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</li>
			{/each}
		</ul>

		{#if !canMerge}
			<p class="muted state">Merging needs the “Manage vendors” permission.</p>
		{/if}
	{/if}

	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>Close</button>
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
		background: rgba(31, 168, 106, 0.05);
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
