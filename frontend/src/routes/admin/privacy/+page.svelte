<script lang="ts">
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { goto } from '$app/navigation';
	import { formatDate } from '#lib/utils/time.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import StepUpPrompt from '#lib/components/ui/StepUpPrompt.svelte';
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { submitDsar, submitErasure, listPrivacyRequests } from '#lib/api/privacy.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { PERM_VENDOR_BANK_CHANGE_APPROVE } from '#lib/types/admin.ts';
	import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
	import {
		SUBJECT_TYPES,
		SUBJECT_TYPE_LABELS,
		SUBJECT_IDENTIFIER_HINTS,
		type SubjectType
	} from '#lib/types/privacy.ts';
	import type { DataSubjectRequestSummary, DSARResponse } from '#lib/types/privacy.ts';

	// RBAC: the backend gates every /api/privacy endpoint to admin only (the
	// privacy-officer privilege) and 403s the rest.
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	const COLUMNS = [
		{ label: 'Type' },
		{ label: 'Subject' },
		{ label: 'Status' },
		{ label: 'Requested' },
		{ label: 'Completed' }
	];

	// ── Request form ─────────────────────────────────────────────────────
	let subjectType = $state<SubjectType>('user');
	let identifier = $state('');
	let dsarSubmitting = $state(false);

	// ── Past requests ────────────────────────────────────────────────────
	let requests = $state<DataSubjectRequestSummary[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function loadRequests() {
		loading = true;
		error = null;
		try {
			const res = await listPrivacyRequests();
			requests = res.requests;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Failed to load the request history.';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		if (userLoaded && allowed) loadRequests();
	});

	// ── DSAR export ──────────────────────────────────────────────────────
	let dsarResult = $state<DSARResponse | null>(null);
	let dsarCopied = $state(false);

	// ── Unmasked banking (vendor_contact only) ───────────────────────────
	// Bank details are masked unless the admin opts in, states a reason, holds
	// the bank-change-approve permission AND proves a second factor — the server
	// enforces all four (`api/privacy.py`, `api/auth.require_sensitive_step_up`).
	// The option is offered only where the first three can hold, so it is never
	// a control the server is guaranteed to refuse.
	const canUnmask = $derived(
		subjectType === 'vendor_contact' && auth.can(PERM_VENDOR_BANK_CHANGE_APPROVE)
	);
	let includeBanking = $state(false);
	let bankingJustification = $state('');
	const unmasking = $derived(canUnmask && includeBanking);
	// The server's verdict on the second factor drives the prompt: it answers
	// `sensitive_step_up_required` when no proof was sent, which is the cue to
	// collect one. Asking the server rather than guessing keeps a local stack
	// with the MFA master switch off working without a prompt it would ignore.
	let stepUpOpen = $state(false);
	let stepUpError = $state<string | null>(null);
	let noFactor = $state(false);

	function refusalCode(e: unknown): string | null {
		const code = (e as { code?: unknown } | null)?.code;
		return typeof code === 'string' ? code : null;
	}

	async function handleExport(stepUp?: StepUpProof) {
		const id = identifier.trim();
		if (!id) return;
		if (unmasking && !bankingJustification.trim()) return;
		dsarSubmitting = true;
		noFactor = false;
		try {
			const res = await submitDsar({
				subject_type: subjectType,
				identifier: id,
				...(unmasking
					? {
							include_banking: true,
							banking_justification: bankingJustification.trim(),
							...(stepUp ? { step_up: stepUp } : {})
						}
					: {})
			});
			stepUpOpen = false;
			stepUpError = null;
			dsarResult = res;
			dsarCopied = false;
			await loadRequests();
		} catch (e) {
			const code = refusalCode(e);
			if (code === 'sensitive_step_up_required') {
				stepUpError = null;
				stepUpOpen = true;
			} else if (code === 'sensitive_step_up_failed' && stepUpOpen) {
				stepUpError = authErrorMessage(e, m, 'stepUpPrompt.failed');
			} else if (code === 'sensitive_step_up_no_factor') {
				stepUpOpen = false;
				noFactor = true;
			} else {
				stepUpOpen = false;
				// A coded refusal this build can state renders in the reader's
				// language (`sensitive_step_up_unavailable`); anything else is the
				// error's own message, exactly as before.
				toast(
					e instanceof Error
						? authErrorMessage(e, m, 'stepUpPrompt.failed')
						: 'Failed to export the subject’s data.',
					'error'
				);
			}
		} finally {
			dsarSubmitting = false;
		}
	}

	async function copyDsarJson() {
		if (!dsarResult) return;
		try {
			await navigator.clipboard.writeText(JSON.stringify(dsarResult.data, null, 2));
			dsarCopied = true;
			toast('Copied to clipboard.', 'success');
		} catch {
			toast('Could not copy — the value is still selectable above.', 'error');
		}
	}

	// ── Erasure (irreversible — confirm-then-act) ───────────────────────
	let erasing = $state(false); // the confirm modal is open, targeting the current form values
	let erasureNote = $state('');
	let erasureAcknowledged = $state(false);
	let erasureArmed = $state(false); // second-click arm on the modal's own confirm button
	let erasureSubmitting = $state(false);

	function openErasureConfirm() {
		const id = identifier.trim();
		if (!id) return;
		erasureNote = '';
		erasureAcknowledged = false;
		erasureArmed = false;
		erasing = true;
	}

	function closeErasureConfirm() {
		if (erasureSubmitting) return; // don't let a stray Esc/backdrop click abandon an in-flight erasure
		erasing = false;
	}

	async function handleErasureConfirm() {
		if (!erasureAcknowledged) return;
		if (!erasureArmed) {
			erasureArmed = true;
			return;
		}
		erasureSubmitting = true;
		try {
			const res = await submitErasure({
				subject_type: subjectType,
				identifier: identifier.trim(),
				confirm: true,
				note: erasureNote.trim() || undefined
			});
			erasing = false;
			if (res.already_erased) {
				toast('This subject was already erased — no further change was made.', 'info');
			} else {
				toast(`Erased. ${res.fields_redacted} field(s) redacted.`, 'success');
			}
			identifier = '';
			await loadRequests();
		} catch (e) {
			toast(e instanceof Error ? e.message : 'Failed to erase the subject’s data.', 'error');
		} finally {
			erasureSubmitting = false;
			erasureArmed = false;
		}
	}

	function requestTypeLabel(t: string): string {
		return t === 'erasure' ? 'Erasure' : 'DSAR export';
	}
</script>

<PageHeader title="Privacy & DSAR">
	<p class="page-hint">
		GDPR / CCPA data-subject rights. <HelpTip term="dsar" /> <strong>Export</strong> assembles a portable
		bundle of everything held about a subject (safe to re-run).
		<strong>Erase</strong> irreversibly redacts their PII while preserving the
		money trail and the append-only audit log — it cannot be undone.
	</p>

	<div class="request-card">
		<form
			onsubmit={(e) => {
				e.preventDefault();
				handleExport();
			}}
		>
			<div class="form-row">
				<label for="privacy-subject-type">Subject type</label>
				<select id="privacy-subject-type" bind:value={subjectType}>
					{#each SUBJECT_TYPES as t (t)}
						<option value={t}>{SUBJECT_TYPE_LABELS[t]}</option>
					{/each}
				</select>
			</div>
			<div class="form-row">
				<label for="privacy-identifier">Identifier</label>
				<input
					id="privacy-identifier"
					type="text"
					bind:value={identifier}
					placeholder={subjectType === 'vendor_contact' ? 'Vendor UUID' : 'name@example.com'}
					required
				/>
				<p class="field-hint">{SUBJECT_IDENTIFIER_HINTS[subjectType]}</p>
			</div>
			{#if canUnmask}
				<div class="form-row unmask-row">
					<label class="checkbox-line">
						<input
							type="checkbox"
							bind:checked={includeBanking}
							data-testid="dsar-include-banking"
						/>
						<span>{m('privacyDsar.includeBanking')}</span>
					</label>
					<p class="field-hint">{m('privacyDsar.includeBankingHint')} <HelpTip term="step-up" /></p>
					{#if includeBanking}
						<label for="privacy-banking-justification" class="justification-label">
							{m('privacyDsar.justificationLabel')}
						</label>
						<textarea
							id="privacy-banking-justification"
							bind:value={bankingJustification}
							rows="2"
							maxlength="500"
							required
						></textarea>
						<p class="field-hint">{m('privacyDsar.justificationHint')}</p>
					{/if}
					{#if noFactor}
						<p class="unmask-refusal" role="alert" data-testid="dsar-no-factor">
							<LinkedMessage
								text={m('privacyDsar.noFactor')}
								links={{
									profile: { href: '/profile?section=mfa', label: m('privacyDsar.noFactorLink') }
								}}
							/>
						</p>
					{/if}
				</div>
			{/if}
			<div class="form-actions">
				<button
					type="submit"
					class="btn-primary"
					disabled={dsarSubmitting ||
						!identifier.trim() ||
						(unmasking && !bankingJustification.trim())}
				>
					{dsarSubmitting ? 'Exporting…' : 'Export data (DSAR)'}
				</button>
				<button
					type="button"
					class="btn-danger"
					onclick={openErasureConfirm}
					disabled={!identifier.trim()}
				>
					Erase data…
				</button>
			</div>
		</form>
	</div>

	<h2 class="section-heading">Request history</h2>
	{#if loading}
		<p class="state" data-testid="privacy-loading">Loading…</p>
	{:else if error}
		<div class="state error" data-testid="privacy-error" role="alert">
			<p>{error}</p>
			<button type="button" class="btn-cancel" onclick={loadRequests}>Retry</button>
		</div>
	{:else}
		<DataTable columns={COLUMNS} isEmpty={requests.length === 0} empty="No requests yet.">
			{#snippet body()}
				{#each requests as r (r.id)}
					<tr>
						<td>
							<Badge tone={r.request_type === 'erasure' ? 'danger' : 'accent'}>
								{requestTypeLabel(r.request_type)}
							</Badge>
						</td>
						<td>
							<div class="subject-cell">
								<span>{SUBJECT_TYPE_LABELS[r.subject_type as SubjectType] ?? r.subject_type}</span>
								{#if r.subject_id}
									<span class="mono subject-id" title={r.subject_id}
										>{r.subject_id.slice(0, 8)}…</span
									>
								{/if}
							</div>
						</td>
						<td>
							{#if r.status === 'completed'}
								<Badge tone="success">Completed</Badge>
							{:else if r.status === 'noop'}
								<Badge tone="muted">Already erased</Badge>
							{:else}
								<Badge tone="danger">Failed</Badge>
							{/if}
						</td>
						<td>{formatDate(r.created_at)}</td>
						<td>{formatDate(r.completed_at)}</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<!-- DSAR export result -->
<Modal
	open={dsarResult !== null}
	ariaLabel="Data export"
	width="lg"
	onclose={() => (dsarResult = null)}
>
	{#if dsarResult}
		<h2>Data export</h2>
		<p class="modal-hint">
			{SUBJECT_TYPE_LABELS[dsarResult.subject_type as SubjectType] ?? dsarResult.subject_type} —
			generated {formatDate(dsarResult.generated_at, undefined, {
				hour: 'numeric',
				minute: 'numeric'
			})}. This bundle is not stored — copy it now if you need it.
		</p>
		{#if dsarResult.banking_disclosure === 'unmasked'}
			<p class="unmask-refusal" data-testid="dsar-unmasked-notice">
				{m('privacyDsar.unmaskedNotice')}
			</p>
		{/if}
		<div class="json-view-wrap">
			<pre class="json-view" data-testid="dsar-bundle">{JSON.stringify(dsarResult.data, null, 2)}</pre>
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (dsarResult = null)}>Close</button>
			<button type="button" class="btn-primary" onclick={copyDsarJson}>
				{dsarCopied ? 'Copied' : 'Copy JSON'}
			</button>
		</div>
	{/if}
</Modal>

<!-- Second-factor proof for an unmasked export -->
<StepUpPrompt
	open={stepUpOpen}
	operation="dsar_unmasked_export"
	hint={m('privacyDsar.stepUpHint')}
	busy={dsarSubmitting}
	error={stepUpError}
	onproof={(proof) => handleExport(proof)}
	oncancel={() => {
		stepUpOpen = false;
		stepUpError = null;
	}}
/>

<!-- Erasure confirm-then-act (irreversible) -->
<Modal open={erasing} ariaLabel="Erase subject data" width="md" onclose={closeErasureConfirm}>
	<h2>Erase subject data</h2>
	<div class="erase-warning" role="alert">
		<strong>This is permanent.</strong> PII text fields are redacted in place —
		email, name, bank details, tax id, address — while every invoice / payment
		amount and the append-only audit log are preserved untouched. This cannot
		be undone.
	</div>
	<dl class="erase-meta">
		<div>
			<dt>Subject type</dt>
			<dd>{SUBJECT_TYPE_LABELS[subjectType]}</dd>
		</div>
		<div>
			<dt>Identifier</dt>
			<dd class="mono">{identifier}</dd>
		</div>
	</dl>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleErasureConfirm();
		}}
	>
		<label class="note-label" for="erasure-note">
			Note <span class="optional">(optional — legal basis / ticket reference, kept PII-free)</span>
		</label>
		<textarea
			id="erasure-note"
			bind:value={erasureNote}
			rows="2"
			maxlength="500"
			disabled={erasureSubmitting}
		></textarea>
		<label class="checkbox-line ack-line">
			<input
				type="checkbox"
				bind:checked={erasureAcknowledged}
				onchange={() => (erasureArmed = false)}
				disabled={erasureSubmitting}
			/>
			<span>I understand this action is permanent and cannot be undone.</span>
		</label>
		<div class="modal-footer">
			<button
				type="button"
				class="btn-cancel"
				onclick={closeErasureConfirm}
				disabled={erasureSubmitting}
			>
				Cancel
			</button>
			<RowAction
				variant="danger"
				armed={erasureArmed}
				disabled={!erasureAcknowledged || erasureSubmitting}
				type="submit"
			>
				{erasureSubmitting ? 'Erasing…' : erasureArmed ? 'Click again to confirm' : 'Erase permanently'}
			</RowAction>
		</div>
	</form>
</Modal>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 760px;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.request-card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 1.25rem 1.5rem;
		max-width: 640px;
	}

	.form-row {
		margin-bottom: 1rem;
	}

	.form-row label {
		display: block;
		font-weight: 600;
		margin-bottom: 0.35rem;
	}

	.form-row select,
	.form-row input {
		width: 100%;
	}

	.field-hint {
		margin: 0.35rem 0 0;
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	.form-actions {
		display: flex;
		gap: 0.5rem;
		margin-top: 0.25rem;
	}

	.btn-danger {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--danger);
		background: transparent;
		color: var(--danger);
		font-weight: 600;
		cursor: pointer;
	}

	.btn-danger:hover:not(:disabled) {
		background: rgba(248, 113, 113, 0.1);
	}

	.btn-danger:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.section-heading {
		font-size: 1rem;
		margin: 0.5rem 0 0;
	}

	.subject-cell {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}

	.subject-id {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}

	.modal-hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0 0 1rem;
	}

	.json-view-wrap {
		max-height: 50vh;
		overflow: auto;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-2);
	}

	.json-view {
		margin: 0;
		padding: 0.85rem 1rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.8rem;
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--text);
	}

	.erase-warning {
		background: rgba(248, 113, 113, 0.1);
		border: 1px solid rgba(248, 113, 113, 0.35);
		color: var(--danger);
		border-radius: 8px;
		padding: 0.75rem 1rem;
		font-size: 0.85rem;
		margin-bottom: 1rem;
	}

	.erase-meta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 0.75rem;
		margin: 0 0 1rem;
	}

	.erase-meta dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.erase-meta dd {
		margin: 0.15rem 0 0;
		font-weight: 600;
		word-break: break-all;
	}

	.note-label {
		display: block;
		font-weight: 600;
		margin-bottom: 0.35rem;
	}

	.optional {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	textarea {
		width: 100%;
		resize: vertical;
		margin-bottom: 1rem;
	}

	.checkbox-line {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: 0.85rem;
	}

	.ack-line {
		margin-bottom: 0.25rem;
	}

	.unmask-row textarea {
		margin-bottom: 0;
	}

	.justification-label {
		margin-top: 0.75rem;
	}

	.unmask-refusal {
		margin: 0.5rem 0 0;
		color: var(--danger);
		font-size: 0.85rem;
	}

	/* The request-form fields live in the page body, outside `.modal` (which
	   app.css styles globally) — same local recipe as `/organization` and
	   `/admin/retention`. This also reaches the erasure modal's textarea
	   (harmless: identical values to the global `.modal textarea` rule).
	   Excludes checkbox/radio explicitly — app.css draws those globally
	   (custom appearance:none marks) and a bare `input` selector at equal
	   specificity would fight that recipe on background/padding/width. */
	input:not([type='checkbox']):not([type='radio']),
	select,
	textarea {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		width: 100%;
		box-sizing: border-box;
	}

	textarea {
		resize: vertical;
	}

	input:not([type='checkbox']):not([type='radio']):focus,
	select:focus,
	textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
</style>
