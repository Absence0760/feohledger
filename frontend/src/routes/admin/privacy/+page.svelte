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
	import {
		submitDsar,
		submitErasure,
		listPrivacyRequests,
		downloadDsarBundle
	} from '#lib/api/privacy.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { PERM_VENDOR_BANK_CHANGE_APPROVE } from '#lib/types/admin.ts';
	import type { StepUpProof } from '#lib/stores/auth.svelte.ts';
	import { ApiError } from '#lib/api.ts';
	import {
		SUBJECT_TYPES,
		SUBJECT_TYPE_LABEL_KEYS,
		SUBJECT_IDENTIFIER_HINT_KEYS,
		dsarCountLines,
		subjectTypeLabelKey,
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

	const COLUMNS = $derived([
		{ label: m('admin.privacy.col.type') },
		{ label: m('admin.privacy.col.subject') },
		{ label: m('admin.privacy.col.status') },
		{ label: m('admin.privacy.col.requested') },
		{ label: m('admin.privacy.col.completed') }
	]);

	/** A stored subject type in the reader's language; one this build
	 *  predates shows verbatim (it is data, not copy). */
	function subjectLabel(t: string): string {
		const key = subjectTypeLabelKey(t);
		return key ? m(key) : t;
	}

	/** The server answers an unknown subject with a bare 404 sentence; say it
	 *  in the reader's language instead. Anything else keeps its own message. */
	function privacyError(e: unknown, fallback: string): string {
		if (e instanceof ApiError && e.status === 404) return m('admin.privacy.error.notFound');
		return e instanceof Error ? authErrorMessage(e, m, 'stepUpPrompt.failed') : fallback;
	}

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
			error = e instanceof Error ? e.message : m('admin.privacy.error.loadHistory');
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
				toast(privacyError(e, m('admin.privacy.error.export')), 'error');
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
			toast(m('admin.privacy.toast.copied'), 'success');
		} catch {
			toast(m('admin.privacy.toast.copyFailed'), 'error');
		}
	}

	// ── Complete bundle (every section uncapped) ─────────────────────────
	// The inline result caps each large section; when one was cut the page
	// says so and offers the whole set as a file, which the server streams
	// and audits per download (decisions §369).
	let bundleDownloading = $state(false);

	async function downloadFullBundle() {
		if (!dsarResult?.full_bundle_url) return;
		bundleDownloading = true;
		try {
			await downloadDsarBundle(dsarResult.full_bundle_url, `dsar-${dsarResult.request_id}.json`);
		} catch (e) {
			toast(privacyError(e, m('admin.privacy.error.bundle')), 'error');
		} finally {
			bundleDownloading = false;
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
			// Summarised from the response's typed counts — never a server sentence.
			if (res.already_erased) {
				toast(m('admin.privacy.toast.alreadyErased'), 'info');
			} else {
				toast(
					m('admin.privacy.toast.erased', {
						fields: res.fields_redacted,
						documents: res.documents_deleted ?? 0
					}),
					'success'
				);
			}
			// A storage object that could not be deleted leaves the erasure
			// incomplete; say so rather than let the success toast stand alone.
			if ((res.documents_failed ?? 0) > 0) {
				toast(m('admin.privacy.toast.erasureIncomplete', { failed: res.documents_failed }), 'error');
			}
			identifier = '';
			await loadRequests();
		} catch (e) {
			toast(privacyError(e, m('admin.privacy.error.erase')), 'error');
		} finally {
			erasureSubmitting = false;
			erasureArmed = false;
		}
	}

	function requestTypeLabel(t: string): string {
		return t === 'erasure' ? m('admin.privacy.requestType.erasure') : m('admin.privacy.requestType.dsarExport');
	}

	const dsarLines = $derived(dsarCountLines(dsarResult?.record_counts));
</script>

<PageHeader title={m('admin.privacy.title')}>
	<p class="page-hint">
		{m('admin.privacy.hint')} <HelpTip term="dsar" />
	</p>

	<div class="request-card">
		<form
			onsubmit={(e) => {
				e.preventDefault();
				handleExport();
			}}
		>
			<div class="form-row">
				<label for="privacy-subject-type">{m('admin.privacy.field.subjectType')}</label>
				<select id="privacy-subject-type" bind:value={subjectType}>
					{#each SUBJECT_TYPES as t (t)}
						<option value={t}>{m(SUBJECT_TYPE_LABEL_KEYS[t])}</option>
					{/each}
				</select>
			</div>
			<div class="form-row">
				<label for="privacy-identifier">{m('admin.privacy.field.identifier')}</label>
				<input
					id="privacy-identifier"
					type="text"
					bind:value={identifier}
					placeholder={subjectType === 'vendor_contact'
						? m('admin.privacy.placeholder.vendorUuid')
						: m('admin.privacy.placeholder.email')}
					required
				/>
				<p class="field-hint">{m(SUBJECT_IDENTIFIER_HINT_KEYS[subjectType])}</p>
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
					{dsarSubmitting ? m('admin.privacy.action.exporting') : m('admin.privacy.action.export')}
				</button>
				<button
					type="button"
					class="btn-danger"
					onclick={openErasureConfirm}
					disabled={!identifier.trim()}
				>
					{m('admin.privacy.action.erase')}
				</button>
			</div>
		</form>
	</div>

	<h2 class="section-heading">{m('admin.privacy.history.title')}</h2>
	{#if loading}
		<p class="state" data-testid="privacy-loading">{m('common.loading')}</p>
	{:else if error}
		<LoadError message={error} onretry={loadRequests} testId="privacy-error" />
	{:else}
		<DataTable columns={COLUMNS} isEmpty={requests.length === 0} empty={m('admin.privacy.history.empty')}>
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
								<span>{subjectLabel(r.subject_type)}</span>
								{#if r.subject_id}
									<span class="mono subject-id" title={r.subject_id}
										>{r.subject_id.slice(0, 8)}…</span
									>
								{/if}
							</div>
						</td>
						<td>
							{#if r.status === 'completed'}
								<Badge tone="success">{m('admin.privacy.status.completed')}</Badge>
							{:else if r.status === 'noop'}
								<Badge tone="muted">{m('admin.privacy.status.noop')}</Badge>
							{:else}
								<Badge tone="danger">{m('admin.privacy.status.failed')}</Badge>
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
	ariaLabel={m('admin.privacy.export.title')}
	width="lg"
	onclose={() => (dsarResult = null)}
>
	{#if dsarResult}
		<h2>{m('admin.privacy.export.title')}</h2>
		<p class="modal-hint">
			{m('admin.privacy.export.generated', {
				subject: subjectLabel(dsarResult.subject_type),
				when: formatDate(dsarResult.generated_at, undefined, { hour: 'numeric', minute: 'numeric' })
			})}
		</p>
		{#if dsarResult.record_counts}
			<section class="export-summary" data-testid="dsar-summary">
				<h3>{m('admin.privacy.export.summaryTitle')}</h3>
				{#if dsarLines.length === 0}
					<p class="modal-hint">{m('admin.privacy.export.summaryProfileOnly')}</p>
				{:else}
					<ul>
						{#each dsarLines as line (line.section)}
							<li data-section={line.section}>{m(line.key, { n: line.n })}</li>
						{/each}
					</ul>
				{/if}
			</section>
		{/if}
		{#if dsarResult.complete === false}
			<p class="truncated-notice" role="status" data-testid="dsar-truncated-notice">
				<!-- The bundle download is always masked (decisions §369), so after an
				     unmasked disclosure it is not "everything held": say where the
				     unmasked bank details are. -->
				{m(
					dsarResult.banking_disclosure === 'unmasked'
						? 'admin.privacy.export.truncatedUnmasked'
						: 'admin.privacy.export.truncated',
					{ sections: dsarResult.truncated_sections?.length ?? 0 }
				)}
			</p>
		{/if}
		{#if dsarResult.banking_disclosure === 'unmasked'}
			<p class="unmask-refusal" data-testid="dsar-unmasked-notice">
				{m('privacyDsar.unmaskedNotice')}
			</p>
		{/if}
		<div class="json-view-wrap">
			<pre class="json-view" data-testid="dsar-bundle">{JSON.stringify(dsarResult.data, null, 2)}</pre>
		</div>
		<div class="modal-footer export-footer">
			<button type="button" class="btn-cancel" onclick={() => (dsarResult = null)}
				>{m('admin.privacy.export.close')}</button
			>
			{#if dsarResult.full_bundle_url}
				{#if dsarResult.banking_disclosure === 'unmasked'}
					<small class="bundle-masked-note" id="dsar-bundle-masked" data-testid="dsar-bundle-masked">
						{m('admin.privacy.export.bundleMasked')}
					</small>
				{/if}
				<button
					type="button"
					class={dsarResult.complete === false ? 'btn-primary' : 'btn-cancel'}
					onclick={downloadFullBundle}
					disabled={bundleDownloading}
					aria-describedby={dsarResult.banking_disclosure === 'unmasked'
						? 'dsar-bundle-masked'
						: undefined}
					data-testid="dsar-download-bundle"
				>
					{bundleDownloading
						? m('admin.privacy.export.downloading')
						: m('admin.privacy.export.download')}
				</button>
			{/if}
			<button
				type="button"
				class={dsarResult.complete === false ? 'btn-cancel' : 'btn-primary'}
				onclick={copyDsarJson}
			>
				{dsarCopied ? m('admin.privacy.export.copied') : m('admin.privacy.export.copy')}
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
<Modal
	open={erasing}
	ariaLabel={m('admin.privacy.erase.title')}
	width="md"
	onclose={closeErasureConfirm}
>
	<h2>{m('admin.privacy.erase.title')}</h2>
	<div class="erase-warning" role="alert">
		<strong>{m('admin.privacy.erase.warningLead')}</strong>
		{m('admin.privacy.erase.warningBody')}
	</div>
	<dl class="erase-meta">
		<div>
			<dt>{m('admin.privacy.field.subjectType')}</dt>
			<dd>{m(SUBJECT_TYPE_LABEL_KEYS[subjectType])}</dd>
		</div>
		<div>
			<dt>{m('admin.privacy.field.identifier')}</dt>
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
			{m('admin.privacy.erase.note')}
			<span class="optional">{m('admin.privacy.erase.noteHint')}</span>
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
			<span>{m('admin.privacy.erase.acknowledge')}</span>
		</label>
		<div class="modal-footer">
			<button
				type="button"
				class="btn-cancel"
				onclick={closeErasureConfirm}
				disabled={erasureSubmitting}
			>
				{m('common.cancel')}
			</button>
			<RowAction
				variant="danger"
				armed={erasureArmed}
				disabled={!erasureAcknowledged || erasureSubmitting}
				type="submit"
			>
				{erasureSubmitting
					? m('admin.privacy.erase.erasing')
					: erasureArmed
						? m('admin.privacy.erase.confirmAgain')
						: m('admin.privacy.erase.confirm')}
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
		background: color-mix(in srgb, var(--danger) 10%, transparent);
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

	.export-summary {
		margin: 0 0 1rem;
	}

	.export-summary h3 {
		font-size: 0.85rem;
		margin: 0 0 0.35rem;
	}

	.export-summary ul {
		margin: 0;
		padding-left: 1.1rem;
		font-size: 0.85rem;
		columns: 2 14rem;
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
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 35%, transparent);
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

	/* An inline export that was cut short. Warning tint + its on-tint text, the
	   pair app.css measures for contrast (not the bare --warning hue). */
	.truncated-notice {
		margin: 0.5rem 0 0;
		padding: 0.5rem 0.75rem;
		border-radius: 4px;
		background: var(--warning-tint);
		color: var(--warning-on-tint);
		font-size: 0.85rem;
	}

	.unmask-refusal {
		margin: 0.5rem 0 0;
		color: var(--danger);
		font-size: 0.85rem;
	}

	/* The masked-bundle note sits beside the download button it describes, and
	   wraps onto its own line rather than squeezing the buttons at 320px. */
	.export-footer {
		flex-wrap: wrap;
		align-items: center;
	}
	.bundle-masked-note {
		flex: 1 1 14rem;
		color: var(--text-muted);
		font-size: 0.8rem;
		line-height: 1.4;
		text-align: right;
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
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}
</style>
