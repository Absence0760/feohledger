<script lang="ts">
	import { goto } from '$app/navigation';
	import { formatDate } from '#lib/utils/time.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { getAccessReview, acknowledgeAccessReview } from '#lib/api/accessReviews.ts';
	import type { AccessReviewResponse } from '#lib/types/accessReview.ts';

	// RBAC: the backend gates both /api/access-reviews routes to admin | cfo
	// (`require_roles(ROLE_ADMIN, ROLE_CFO)`) — the reviewer privilege. Wait for
	// `auth.user` to resolve before redirecting so we don't bounce before /me
	// lands (mirrors /admin/api-keys, /admin/webhooks).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isCfo); // hasAnyRole('admin', 'cfo') — same gate as the backend

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	const COLUMNS = [
		{ label: 'User' },
		{ label: 'Roles' },
		{ label: 'Last privileged action' },
		{ label: 'Status' }
	];

	let review = $state<AccessReviewResponse | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let acknowledging = $state(false);
	let lastAcknowledged = $state<{ at: string; byMe: boolean } | null>(null);

	// Narrowed away from null so the table `body()` snippet (a closure, which
	// loses the `{:else if review}` narrowing — same shape as `usageDays` in
	// `/admin/api-keys`) can read it without a possibly-null error.
	const reviewUsers = $derived(review ? review.users : []);

	async function load() {
		loading = true;
		error = null;
		try {
			review = await getAccessReview();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load the access review.';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (userLoaded && allowed) load();
	});

	async function handleAcknowledge() {
		acknowledging = true;
		try {
			const res = await acknowledgeAccessReview();
			lastAcknowledged = { at: res.last_completed_at, byMe: true };
			toast('Access review acknowledged for this period.', 'success');
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to acknowledge the review.', 'error');
		} finally {
			acknowledging = false;
		}
	}
</script>

<PageHeader title="Access Review">
	{#snippet actions()}
		<button
			class="btn-primary"
			onclick={handleAcknowledge}
			disabled={acknowledging || loading || review === null}
		>
			{acknowledging ? 'Acknowledging…' : 'Acknowledge review'}
		</button>
	{/snippet}

	<p class="page-hint">
		Every active user holding an elevated role (admin / ap_manager / cfo, or a
		custom role granting a fraud-sensitive permission) whose last <em>mutating</em>
		privileged action is stale or absent — the periodic SOX access-control review.
		A read (viewing a record) doesn't reset the clock; only a write does.
	</p>

	{#if lastAcknowledged}
		<p class="ack-note" data-testid="access-review-ack-note">
			Acknowledged for this period at {formatDate(lastAcknowledged.at, undefined, {
				hour: 'numeric',
				minute: 'numeric'
			})}.
		</p>
	{/if}

	<!-- KPI row — rendered on EVERY state, never gated on the response.
	     `docs/decisions.md` §125: an absent figure is a dash, and whether it is
	     absent because it is still arriving is ANNOUNCED, not drawn. It used to
	     sit inside `{:else if review}` and collapse while the review was being
	     computed.

	     The Dormant card is why this one matters beyond consistency: its red
	     tint is a VERDICT on a SOX access control, and an untinted "0 dormant"
	     drawn before anyone had counted is the reassuring answer asserted while
	     still asking — the `/bank-reconciliation` case §125 calls the sharpest
	     one. `KpiCard` withholds the tint while there is no figure. -->
	<div class="kpi-row">
		<KpiCard
			value={review ? String(review.total) : null}
			label="Elevated users"
			pending={loading}
		/>
		<KpiCard
			value={review ? String(review.dormant_count) : null}
			label="Dormant"
			highlight={review && review.dormant_count > 0 ? 'red' : null}
			pending={loading}
		/>
		<KpiCard
			value={review ? `${review.dormant_after_days}d` : null}
			label="Dormancy window"
			pending={loading}
		/>
		<KpiCard
			value={review
				? formatDate(review.generated_at, undefined, { hour: 'numeric', minute: 'numeric' })
				: null}
			label="Generated"
			pending={loading}
		/>
	</div>

	{#if loading}
		<p class="state" data-testid="access-review-loading">Loading…</p>
	{:else if error}
		<div class="state error" data-testid="access-review-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={load}>Retry</button>
		</div>
	{:else if review}
		<DataTable columns={COLUMNS} isEmpty={reviewUsers.length === 0} empty="No elevated users.">
			{#snippet body()}
				{#each reviewUsers as u (u.user_id)}
					<tr class:dormant-row={u.dormant}>
						<td>
							<div class="user-cell">
								<span class="user-name">{u.full_name}</span>
								<span class="user-email">{u.email}</span>
							</div>
						</td>
						<td>{u.roles.join(', ')}</td>
						<td>
							{#if u.last_privileged_action_at}
								{formatDate(u.last_privileged_action_at)}
								{#if u.days_since !== null}
									<span class="days-ago">({u.days_since}d ago)</span>
								{/if}
							{:else}
								<span class="never">Never</span>
							{/if}
						</td>
						<td>
							{#if u.dormant}
								<Badge tone="danger">Dormant</Badge>
							{:else}
								<Badge tone="success">Active</Badge>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 760px;
	}

	.ack-note {
		margin: 0;
		color: var(--success);
		font-size: 0.85rem;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.kpi-row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 1rem;
		margin-bottom: 0.5rem;
	}

	.user-cell {
		display: flex;
		flex-direction: column;
	}

	.user-name {
		font-weight: 600;
	}

	.user-email {
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	.days-ago {
		color: var(--text-muted);
		font-size: 0.8rem;
		margin-left: 4px;
	}

	.never {
		color: var(--text-muted);
		font-style: italic;
	}

	tr.dormant-row td {
		background: rgba(248, 113, 113, 0.04);
	}
</style>
