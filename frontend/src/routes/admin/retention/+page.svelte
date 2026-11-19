<script lang="ts">
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { goto } from '$app/navigation';
	import { guardUnsavedChanges } from '#lib/stores/unsavedChanges.svelte.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { getRetentionPolicy, updateRetentionPolicy } from '#lib/api/retention.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import {
		RETENTION_RECORD_CLASSES,
		type RetentionRecordClass
	} from '#lib/types/retention.ts';

	// RBAC: the backend gates GET/PUT /api/retention-policy to admin only and
	// 403s the rest. Wait for `auth.user` to resolve before redirecting so we
	// don't bounce before /me lands (mirrors /admin/api-keys, /admin/webhooks).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	const CLASS_LABELS: Record<RetentionRecordClass, MessageKey> = {
		invoices: 'admin.retention.class.invoices',
		audit_log: 'admin.retention.class.audit_log'
	};

	const CLASS_HINTS: Record<RetentionRecordClass, MessageKey> = {
		invoices: 'admin.retention.classHint.invoices',
		audit_log: 'admin.retention.classHint.audit_log'
	};

	// An env var name, not copy: it reads the same in every locale.
	const RETENTION_FLAG = 'FEOH_RETENTION_ENABLED';

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

	// The PUT refuses anything but a positive whole number of months
	// (`api/retention.py`). A cleared field parses to 0, which used to enable
	// Save and only fail as a toast after the round trip; say so at the field
	// and keep Save off instead.
	function isValidMonths(v: number | undefined): boolean {
		return typeof v === 'number' && Number.isInteger(v) && v > 0;
	}
	const invalidClasses = $derived(
		RETENTION_RECORD_CLASSES.filter((cls) => !isValidMonths(policy[cls]))
	);
	// Leaving with an edited window asks first (the app-wide dialog).
	guardUnsavedChanges(() => dirty && !saving);

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
			error = e instanceof Error ? e.message : m('admin.retention.loadFailed');
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
		if (Object.keys(changed).length === 0 || invalidClasses.length > 0) return;

		saving = true;
		try {
			const res = await updateRetentionPolicy(changed);
			policy = { ...res.policy };
			savedPolicy = { ...res.policy };
			defaultMonths = res.default_months;
			sweepEnabled = res.enabled;
			toast(m('admin.retention.saved'), 'success');
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.retention.saveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	function reset() {
		policy = { ...savedPolicy };
	}
</script>

<PageHeader title={m('admin.retention.title')}>
	<!-- Save sits in the header, as on the Report Builder: below the long hint
	     and both rows it fell off a phone's first screen. Shown once the policy
	     has loaded, so it never offers to save a form that is not there. -->
	{#snippet actions()}
		{#if !loading && !error}
			<button type="button" class="btn-cancel" onclick={reset} disabled={!dirty || saving}>
				{m('admin.retention.reset')}
			</button>
			<button
				type="button"
				class="btn-primary"
				onclick={save}
				disabled={!dirty || saving || invalidClasses.length > 0}
			>
				{saving ? m('common.saving') : m('admin.retention.save')}
			</button>
		{/if}
	{/snippet}

	<p class="page-hint">
		{m('admin.retention.introPre')} <HelpTip term="retention-policy" />
		{m('admin.retention.introPost')} <HelpTip term="audit-trail" />
		{m('admin.retention.sweepLabel')}
		<Badge tone={sweepEnabled ? 'success' : 'muted'}>
			{sweepEnabled ? m('admin.retention.sweepEnabled') : m('admin.retention.sweepDisabled')}
		</Badge>
		<span class="sweep-hint">
			{m('admin.retention.sweepHintPre')} <code>{RETENTION_FLAG}</code>
			{m('admin.retention.sweepHintPost')}
		</span>
	</p>

	{#if loading}
		<p class="state" data-testid="retention-loading">{m('common.loading')}</p>
	{:else if error}
		<LoadError message={error} onretry={load} testId="retention-error" />
	{:else}
		<div class="policy-card">
			{#each RETENTION_RECORD_CLASSES as cls (cls)}
				<div class="policy-row">
					<div class="policy-row-label">
						<label for={`retention-${cls}`}>{m(CLASS_LABELS[cls])}</label>
						<p class="policy-row-hint" id={`retention-${cls}-hint`}>{m(CLASS_HINTS[cls])}</p>
					</div>
					<div class="policy-row-input">
						<input
							id={`retention-${cls}`}
							type="number"
							min="1"
							step="1"
							data-testid={`retention-input-${cls}`}
							aria-invalid={invalidClasses.includes(cls) ? 'true' : undefined}
							aria-describedby={invalidClasses.includes(cls)
								? `retention-${cls}-hint retention-${cls}-error`
								: `retention-${cls}-hint`}
							value={policy[cls] ?? ''}
							oninput={(e) => {
								const v = parseInt((e.target as HTMLInputElement).value, 10);
								policy = { ...policy, [cls]: Number.isFinite(v) ? v : 0 };
							}}
						/>
						<span class="unit">{m('admin.retention.unit')}</span>
					</div>
					{#if invalidClasses.includes(cls)}
						<p
							class="field-error"
							id={`retention-${cls}-error`}
							data-testid={`retention-error-${cls}`}
						>
							{m('admin.retention.invalid')}
						</p>
					{/if}
					{#if defaultMonths !== null}
						<p class="default-hint">{m('admin.retention.platformDefault', { n: defaultMonths })}</p>
					{/if}
				</div>
			{/each}
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

	.field-error {
		grid-column: 1 / -1;
		margin: 0;
		color: var(--danger);
		font-size: 0.8rem;
	}

	.default-hint {
		grid-column: 1 / -1;
		margin: 0;
		color: var(--text-muted);
		font-size: 0.75rem;
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
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}
</style>
