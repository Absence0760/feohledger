<script lang="ts">
	/**
	 * Organization → Close workspace (`docs/decisions.md` §306).
	 *
	 * An administrator deletes an UNUSED workspace — typically an abandoned
	 * trial — which also frees its users' email addresses, so they can be
	 * added to another workspace (one address is one account in one workspace,
	 * §291). The server refuses, fail-closed, any workspace holding records
	 * that must be kept or other people's accounts; this panel reads that
	 * verdict first (`GET /api/organization/close`) and, when there are
	 * blockers, says what they are and that support closes it instead — it
	 * never offers a control that would be refused.
	 *
	 * When nothing blocks: the admin types the slug back, presses the button,
	 * and — where MFA is on — proves a second factor in `ui/StepUpPrompt`
	 * (a code or a passkey; the server answers `sensitive_step_up_required`
	 * until one is sent). On success the session is gone along with the
	 * account, so the token is cleared and the browser goes to this host's
	 * `/login?reason=workspace_closed`, which says what happened.
	 *
	 * Admin-only end to end: `readOnly` renders the notice and makes no read.
	 */
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import StepUpPrompt from '#lib/components/ui/StepUpPrompt.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { ApiError, clearToken } from '#lib/api.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { WORKSPACE_CLOSED_LOGIN_PATH } from '#lib/api/sessionRejection.ts';
	import { closeWorkspace, getClosurePreflight } from '#lib/api/workspaceClosure.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
	import {
		blockerLabelKey,
		confirmationMatches,
		inventoryLines,
		refusalBlockers,
		type ClosurePreflight
	} from '#lib/types/workspaceClosure.ts';

	let {
		readOnly,
		userLoaded
	}: {
		/** The caller is not an admin: render the notice, read nothing. */
		readOnly: boolean;
		/** `/auth/me` has resolved, so `readOnly` is final. */
		userLoaded: boolean;
	} = $props();

	let preflight = $state<ClosurePreflight | null>(null);
	let loading = $state(false);
	let loadFailed = $state(false);
	let requested = false;

	let typed = $state('');
	let submitting = $state(false);
	let stepUpOpen = $state(false);
	let stepUpError = $state<string | null>(null);
	let noFactor = $state(false);

	const blockers = $derived(preflight?.blockers ?? []);
	const willDelete = $derived(inventoryLines(preflight?.will_delete ?? {}));
	const canSubmit = $derived(
		!!preflight &&
			preflight.closable &&
			confirmationMatches(typed, preflight.slug) &&
			!submitting
	);

	async function load() {
		loading = true;
		loadFailed = false;
		try {
			preflight = await getClosurePreflight();
		} catch {
			loadFailed = true;
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (requested || !userLoaded || readOnly) return;
		requested = true;
		void load();
	});

	function blockerText(code: string): string {
		const key = blockerLabelKey(code);
		return key ? m(key) : code;
	}

	async function submit(stepUp?: StepUpProof) {
		if (!preflight || !confirmationMatches(typed, preflight.slug)) return;
		submitting = true;
		noFactor = false;
		try {
			await closeWorkspace(typed, stepUp);
			stepUpOpen = false;
			// The account this token belonged to no longer exists.
			clearToken();
			window.location.assign(WORKSPACE_CLOSED_LOGIN_PATH);
		} catch (e) {
			const code = e instanceof ApiError ? e.code : null;
			if (code === 'sensitive_step_up_required') {
				stepUpError = null;
				stepUpOpen = true;
			} else if (code === 'sensitive_step_up_failed' && stepUpOpen) {
				stepUpError = authErrorMessage(e, m, 'stepUpPrompt.failed');
			} else if (code === 'sensitive_step_up_no_factor') {
				stepUpOpen = false;
				noFactor = true;
			} else if (code === 'workspace_close_refused' && e instanceof ApiError) {
				// Something changed since the page read the verdict: show the
				// server's current blockers in place of the form.
				stepUpOpen = false;
				const found = refusalBlockers(e.params);
				if (preflight && found) preflight = { ...preflight, closable: false, blockers: found };
				else toast(e.message, 'error');
			} else {
				stepUpOpen = false;
				toast(
					e instanceof Error ? authErrorMessage(e, m, 'org.close.loadFailed') : m('org.close.loadFailed'),
					'error'
				);
			}
		} finally {
			submitting = false;
		}
	}
</script>

<section class="card close-workspace" aria-labelledby="close-workspace-title">
	<h2 id="close-workspace-title">{m('org.close.title')}</h2>
	<p class="card-hint">{m('org.close.intro')}</p>

	{#if readOnly}
		<p class="card-hint" data-testid="close-workspace-admin-only">
			{m('org.readOnly.sectionAdminOnly')}
		</p>
	{:else if loading || (!preflight && !loadFailed)}
		<p class="card-hint" role="status">{m('org.close.checking')}</p>
	{:else if loadFailed || !preflight}
		<LoadError message={m('org.close.loadFailed')} onretry={load} testId="close-workspace-load-error" />
	{:else if !preflight.closable}
		<div class="blocked" data-testid="close-workspace-blocked">
			<h3>{m('org.close.blockedTitle')}</h3>
			<p>{m('org.close.blockedBody')}</p>
			<ul>
				{#each blockers as code (code)}
					<li data-blocker={code}>{blockerText(code)}</li>
				{/each}
			</ul>
			<p>{m('org.close.blockedSupport')}</p>
		</div>
	{:else}
		<div class="will-delete" data-testid="close-workspace-will-delete">
			{#if willDelete.length}
				<p>{m('org.close.willDelete.title')}</p>
				<ul>
					{#each willDelete as [key, n] (key)}
						<li>{m(key, { n })}</li>
					{/each}
				</ul>
			{:else}
				<p>{m('org.close.willDelete.none')}</p>
			{/if}
		</div>
		<form
			class="confirm"
			onsubmit={(e) => {
				e.preventDefault();
				if (canSubmit) void submit();
			}}
		>
			<label for="close-workspace-confirm">
				{m('org.close.confirmLabel', { slug: preflight.slug })}
			</label>
			<input
				id="close-workspace-confirm"
				type="text"
				autocomplete="off"
				autocapitalize="off"
				spellcheck="false"
				aria-describedby="close-workspace-hint"
				data-testid="close-workspace-confirm"
				bind:value={typed}
				disabled={submitting}
			/>
			<p id="close-workspace-hint" class="field-hint">{m('org.close.confirmHint')}</p>
			{#if noFactor}
				<p class="close-error" role="alert" data-testid="close-workspace-no-factor">
					<LinkedMessage
						text={m('org.close.noFactor')}
						links={{
							profile: { href: '/profile?section=mfa', label: m('org.close.noFactorLink') }
						}}
					/>
				</p>
			{/if}
			<div class="form-actions">
				<button
					type="submit"
					class="btn-close-workspace"
					data-testid="close-workspace-submit"
					disabled={!canSubmit}
				>
					{submitting ? m('org.close.submitting') : m('org.close.submit')}
				</button>
			</div>
		</form>
	{/if}
</section>

<StepUpPrompt
	open={stepUpOpen}
	operation="workspace_close"
	hint={m('org.close.stepUpHint')}
	busy={submitting}
	error={stepUpError}
	onproof={(proof) => submit(proof)}
	oncancel={() => {
		stepUpOpen = false;
		stepUpError = null;
	}}
/>

<style>
	/* The panel frame the sibling organization panels use
	   (`CommerceConnectionPanel.svelte`). */
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 20px 24px;
	}

	.card h2 {
		font-size: 1rem;
		font-weight: 600;
		margin: 0 0 4px;
	}

	.card-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 14px;
	}

	label {
		font-size: 0.85rem;
		font-weight: 500;
	}

	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		width: 100%;
		box-sizing: border-box;
	}

	.field-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 0;
	}

	.confirm {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-width: 28rem;
	}

	.confirm input {
		font-family: var(--font-mono);
	}

	.blocked h3 {
		margin: 0 0 6px;
		font-size: 1rem;
	}

	.will-delete p {
		margin: 0 0 4px;
		font-size: 0.85rem;
	}

	.will-delete ul {
		margin: 0 0 14px;
		padding-left: 1.25rem;
		font-size: 0.85rem;
	}

	.blocked ul {
		margin: 8px 0;
		padding-left: 1.25rem;
	}

	.close-error {
		margin: 0;
		color: var(--danger);
		font-size: 0.85rem;
	}

	/* --danger-strong, not --danger: a fill carrying white text (5.37:1 dark,
	   see app.css). The one destructive primary on this page. */
	.btn-close-workspace {
		padding: 9px 18px;
		border-radius: var(--radius-sm);
		border: none;
		background: var(--danger-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
		min-height: 24px;
	}

	.btn-close-workspace:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
