<script lang="ts">
	import { goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { getRetentionPolicy, updateRetentionPolicy } from '$lib/api/retention';
	import {
		RETENTION_RECORD_CLASSES,
		type RetentionRecordClass
	} from '$lib/types/retention';

	// RBAC: the backend gates GET/PUT /api/retention-policy to admin only and
	// 403s the rest. Wait for `auth.user` to resolve before redirecting so we
	// don't bounce before /me lands (mirrors /admin/api-keys, /admin/webhooks).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	const CLASS_LABELS: Record<RetentionRecordClass, string> = {
		invoices: 'Invoices',
		audit_log: 'Audit log'
	};

	const CLASS_HINTS: Record<RetentionRecordClass, string> = {
		invoices:
			'Terminal (done / paid) invoices older than this window are soft-archived — a marker is stamped on the row, nothing is deleted.',
		audit_log:
			'Rows older than this window are verified as WORM-shipped, never deleted — an immutable-audit-log guard rejects any delete outright.'
	};

	let policy = $state<Record<string, number>>({});
	let savedPolicy = $state<Record<string, number>>({});
	let defaultMonths = $state<number | null>(null);
	let sweepEnabled = $state(false);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let saving = $state(false);

	const dirty = $derived(
		RETENTION_RECORD_CLASSES.some((cls) => policy[cls] !== savedPolicy[cls])
	);

	async function load() {
		loading = true;
		error = null;
		try {
			const res = await getRetentionPolicy();
			policy = { ...res.policy };
			savedPolicy = { ...res.policy };
			defaultMonths = res.default_months;
			sweepEnabled = res.enabled;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load the retention policy.';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (userLoaded && allowed) load();
	});

	async function save() {
		// Only send classes that actually changed — the backend leaves omitted
		// classes untouched, so this keeps the PUT minimal and its audit row
		// (`retention_policy.updated`) scoped to what the admin actually edited.
		const changed: Record<string, number> = {};
		for (const cls of RETENTION_RECORD_CLASSES) {
			if (policy[cls] !== savedPolicy[cls]) changed[cls] = policy[cls];
		}
		if (Object.keys(changed).length === 0) return;

		saving = true;
		try {
			const res = await updateRetentionPolicy(changed);
			policy = { ...res.policy };
			savedPolicy = { ...res.policy };
			defaultMonths = res.default_months;
			sweepEnabled = res.enabled;
			toast('Retention policy saved.', 'success');
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to save the retention policy.', 'error');
		} finally {
			saving = false;
		}
	}

	function reset() {
		policy = { ...savedPolicy };
	}
</script>

<PageHeader title="Retention Policy">
	<p class="page-hint">
		SOX records-management windows, per record class. A background sweep
		enforces these — it soft-archives terminal invoices past their window and
		verifies the audit log has been WORM-shipped, and it never deletes an
		audit row. Enforcement sweep:
		<Badge tone={sweepEnabled ? 'success' : 'muted'}>
			{sweepEnabled ? 'Enabled' : 'Disabled'}
		</Badge>
		<span class="sweep-hint">
			(the platform operator's <code>FEOH_RETENTION_ENABLED</code> switch — not
			editable here)
		</span>
	</p>

	{#if loading}
		<p class="state" data-testid="retention-loading">Loading…</p>
	{:else if error}
		<div class="state error" data-testid="retention-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={load}>Retry</button>
		</div>
	{:else}
		<div class="policy-card">
			{#each RETENTION_RECORD_CLASSES as cls (cls)}
				<div class="policy-row">
					<div class="policy-row-label">
						<label for={`retention-${cls}`}>{CLASS_LABELS[cls]}</label>
						<p class="policy-row-hint">{CLASS_HINTS[cls]}</p>
					</div>
					<div class="policy-row-input">
						<input
							id={`retention-${cls}`}
							type="number"
							min="1"
							step="1"
							data-testid={`retention-input-${cls}`}
							value={policy[cls] ?? ''}
							oninput={(e) => {
								const v = parseInt((e.target as HTMLInputElement).value, 10);
								policy = { ...policy, [cls]: Number.isFinite(v) ? v : 0 };
							}}
						/>
						<span class="unit">months</span>
					</div>
					{#if defaultMonths !== null}
						<p class="default-hint">Platform default: {defaultMonths} months</p>
					{/if}
				</div>
			{/each}
		</div>

		<div class="policy-actions">
			<button type="button" class="btn-cancel" onclick={reset} disabled={!dirty || saving}>
				Reset
			</button>
			<button type="button" class="btn-primary" onclick={save} disabled={!dirty || saving}>
				{saving ? 'Saving…' : 'Save changes'}
			</button>
		</div>
	{/if}
</PageHeader>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 760px;
	}

	.page-hint code {
		background: var(--surface-2);
		color: var(--text);
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.8em;
	}

	.sweep-hint {
		display: block;
		margin-top: 2px;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.policy-card {
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 1.25rem 1.5rem;
		max-width: 640px;
	}

	.policy-row {
		display: grid;
		/* Both tracks are allowed to shrink below their content. A bare
		   `1fr auto` floors each track at its own min-content, so the label
		   column plus a 90px number input and its unit could not fit a 320px
		   viewport and pushed the document sideways — WCAG 1.4.10. */
		grid-template-columns: minmax(0, 1fr) minmax(0, auto);
		gap: 0.25rem 1rem;
		align-items: start;
		padding-bottom: 1rem;
		border-bottom: 1px solid var(--border);
	}

	.policy-row:last-child {
		padding-bottom: 0;
		border-bottom: none;
	}

	.policy-row-label label {
		font-weight: 600;
	}

	.policy-row-hint {
		margin: 0.2rem 0 0;
		color: var(--text-muted);
		font-size: 0.8rem;
		max-width: 440px;
	}

	.policy-row-input {
		display: flex;
		align-items: center;
		/* The unit drops under the field instead of widening the row when the
		   grid track is squeezed (WCAG 1.4.10). */
		flex-wrap: wrap;
		gap: 0.5rem;
	}

	.policy-row-input input {
		width: 90px;
		max-width: 100%;
	}

	.unit {
		color: var(--text-muted);
		font-size: 0.85rem;
	}

	.default-hint {
		grid-column: 1 / -1;
		margin: 0;
		color: var(--text-muted);
		font-size: 0.75rem;
	}

	.policy-actions {
		display: flex;
		gap: 0.5rem;
		max-width: 640px;
	}

	/* Plain page-body inputs (not inside `.modal`, which app.css styles
	   globally) need the same recipe locally — see `/organization`'s scoped
	   `input, select, textarea` rule for the reference copy. */
	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		box-sizing: border-box;
	}

	input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
</style>
