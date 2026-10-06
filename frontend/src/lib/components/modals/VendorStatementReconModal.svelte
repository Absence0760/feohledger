<script lang="ts">
	import type {
		Reconciliation,
		ReconLine,
		ReconStatus,
		ReconClassification,
		StatementLineInput
	} from '#lib/types/vendorStatementRecon.ts';
	import {
		reconStatusLabelKey,
		RECON_STATUS_TONES,
		RECON_CLASSIFICATION_LABEL_KEYS,
		RECON_RESOLUTION_LABEL_KEYS,
		reconSourceFormatLabelKey,
		ambiguousSkipCount,
		formatExtractionConfidence,
		isMachineRead,
		sourceStatementFilename
	} from '#lib/types/vendorStatementRecon.ts';
	import type { ReconSourceFormat } from '#lib/types/vendorStatementRecon.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import VendorPicker from '#lib/components/ui/VendorPicker.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { normalizeMoneyInput } from '#lib/utils/moneyInput.ts';
	import {
		createReconciliation,
		uploadReconciliation,
		resolveLine,
		getReconciliation,
		downloadSourceStatement
	} from '#lib/api/vendorStatementRecon.ts';

	let {
		recon,
		onclose,
		onsaved
	}: {
		// null → create mode; a Reconciliation → detail/diff mode.
		recon: Reconciliation | null;
		onclose: () => void;
		onsaved: (r: Reconciliation) => void;
	} = $props();

	const isCreate = $derived(recon === null);
	// create + line resolution = admin/ap_manager.
	const canEdit = $derived(auth.isManager);

	// --- Detail-mode local copy (so resolve actions refresh in place) ---
	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot */
	let detail = $state<Reconciliation | null>(recon);
	/* eslint-enable svelte/state-referenced-locally */

	// --- Create-mode fields ---
	let vendor_id = $state('');
	let statement_date = $state('');
	let statement_reference = $state('');
	let currency = $state('USD');
	let notes = $state('');
	// Intake choice, explicit rather than inferred. Both paths used to be visible
	// at once with "a file wins" as the tiebreak, so a user who typed lines AND
	// picked a file silently lost the lines. The mode is now the single answer to
	// "where do these statement lines come from".
	let intakeMode = $state<'paste' | 'file'>('paste');
	let file = $state<File | null>(null);
	let lines = $state<StatementLineInput[]>([{ invoice_number: '', amount: null, invoice_date: '' }]);

	// Mirrors `storage.MAX_FILE_SIZE` (25 MB). The backend is authoritative and
	// 413s regardless — this only spares the user a 25 MB upload that can't land,
	// and lets the refusal read as a size problem instead of a network failure.
	const MAX_UPLOAD_MB = 25;

	// The backend refuses a statement it cannot read HONESTLY (a scan with no text
	// layer, a multi-money-column layout, a CSV with no usable header) and returns
	// a specific, PII-free explanation. That explanation is the whole point of the
	// refusal, so it lands in a persistent inline region — a toast that fades
	// leaves the user with a form that just didn't work.
	let intakeError = $state<string | null>(null);

	let saving = $state(false);
	let busyLineId = $state<string | null>(null);

	function addLine() {
		lines = [...lines, { invoice_number: '', amount: null, invoice_date: '' }];
	}

	function removeLine(idx: number) {
		lines = lines.filter((_, i) => i !== idx);
		if (lines.length === 0) addLine();
	}

	function onFile(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const picked = input.files?.[0] ?? null;
		intakeError = null;
		if (picked && picked.size > MAX_UPLOAD_MB * 1024 * 1024) {
			file = null;
			input.value = '';
			intakeError = m('vendorStatements.modal.fileTooLarge', { max: MAX_UPLOAD_MB });
			return;
		}
		file = picked;
	}

	function clearFile() {
		file = null;
		intakeError = null;
	}

	function setIntakeMode(mode: 'paste' | 'file') {
		intakeMode = mode;
		intakeError = null;
		// Switching to the typed path drops the picked file. The `{#if}` destroys
		// the file input, so keeping the File would leave our "Selected: x.csv"
		// chip contradicting a picker that reads "No file chosen" on the way back —
		// and the mode is supposed to be the single answer to where these lines
		// come from.
		if (mode === 'paste') file = null;
	}

	function handleError(err: unknown, fallback: string) {
		toast(err instanceof Error ? err.message : fallback, 'error');
	}

	// The pasted-lines path needs at least one row carrying something to match
	// on. Submitting an empty editor would create a run asserting the supplier
	// listed nothing — which reads as "we owe them nothing", the same claim the
	// PDF path deliberately refuses to invent.
	const payloadLines = $derived(
		lines
			.filter((l) => (l.invoice_number ?? '').toString().trim() || l.amount != null)
			.map((l) => ({
				invoice_number: (l.invoice_number ?? '').toString().trim() || null,
				invoice_date: l.invoice_date || null,
				amount: l.amount ?? null,
				status: l.status ?? null
			}))
	);

	const canSubmit = $derived(
		!!vendor_id &&
			!!statement_date &&
			(intakeMode === 'file' ? file !== null : payloadLines.length > 0)
	);

	async function handleCreate() {
		if (!canSubmit) return;
		// A typed statement amount goes to the wire as the exact decimal text —
		// it is the figure a mismatch is classified against, so an amount we
		// cannot read is REFUSED, never quietly sent as `null` (which the
		// reconciler reads as a line with no amount and matches on nothing).
		if (payloadLines.some((l) => l.amount !== null && normalizeMoneyInput(l.amount) === null)) {
			intakeError = m('common.amountInvalid');
			return;
		}
		saving = true;
		intakeError = null;
		try {
			let saved: Reconciliation;
			if (intakeMode === 'file' && file) {
				saved = await uploadReconciliation(file, {
					vendor_id,
					statement_date,
					statement_reference: statement_reference.trim() || undefined,
					currency: currency.trim() || undefined
				});
			} else {
				saved = await createReconciliation({
					vendor_id,
					statement_date,
					statement_reference: statement_reference.trim() || null,
					currency: currency.trim() || 'USD',
					notes: notes.trim() || null,
					lines: payloadLines
				});
			}
			toast(m('vendorStatements.modal.toastCreated'), 'success');
			onsaved(saved);
			onclose();
		} catch (err) {
			// 422 (unreadable statement) / 413 (too large) / 404 (vendor out of
			// scope) all arrive here carrying the backend's own explanation.
			intakeError =
				err instanceof Error && err.message
					? err.message
					: m('vendorStatements.modal.toastCreateFailed');
		} finally {
			saving = false;
		}
	}

	async function runResolve(line: ReconLine, status: 'resolved' | 'ignored') {
		if (!detail) return;
		busyLineId = line.id;
		try {
			const updated = await resolveLine(detail.id, line.id, { resolution_status: status });
			detail = updated;
			onsaved(updated);
			toast(
				status === 'resolved'
					? m('vendorStatements.modal.toastResolved')
					: m('vendorStatements.modal.toastIgnored'),
				'success'
			);
		} catch (err) {
			// Refresh from the server so the modal doesn't show stale state.
			try {
				if (detail) detail = await getReconciliation(detail.id);
			} catch {
				/* keep the existing snapshot */
			}
			handleError(err, m('vendorStatements.modal.toastUpdateFailed'));
		} finally {
			busyLineId = null;
		}
	}

	function isActionable(line: ReconLine): boolean {
		return (
			line.resolution_status === 'unresolved' &&
			(line.classification === 'missing_on_our_side' ||
				line.classification === 'amount_mismatch')
		);
	}

	function classificationTone(c: ReconClassification): string {
		// matched = neutral/green; everything else flags a discrepancy.
		return c === 'matched' ? 'ok' : c === 'amount_mismatch' ? 'warn' : 'flag';
	}

	const status = $derived<ReconStatus>(detail?.status ?? 'open');
	const sortedLines = $derived(detail?.lines ?? []);

	// An unrecognised source format renders its raw value rather than a blank
	// pill — a new backend format would otherwise disappear silently.
	const sourceLabel = $derived.by(() => {
		if (!detail) return '';
		const key = reconSourceFormatLabelKey(detail.source_format);
		return key ? m(key) : detail.source_format;
	});

	// The run status is a message key, not an English literal — an unrecognised
	// value from the API renders raw rather than blank.
	const statusLabel = $derived.by(() => {
		const key = reconStatusLabelKey(status);
		return key ? m(key) : status;
	});

	// --- Source document ---
	let downloading = $state(false);

	async function downloadSource() {
		if (!detail) return;
		downloading = true;
		try {
			await downloadSourceStatement(detail.id, sourceStatementFilename(detail));
		} catch {
			// Deliberately NOT the thrown message: `api.downloadBlob` raises a bare
			// transport string ("Failed to load file: 404"), which is not an
			// explanation a clerk can act on — unlike the create path, where the
			// backend's own 422 reason IS the actionable part.
			toast(m('vendorStatements.modal.toastDownloadFailed'), 'error');
		} finally {
			downloading = false;
		}
	}

	const modalTitle = $derived(
		isCreate
			? m('vendorStatements.modal.titleCreate')
			: m('vendorStatements.modal.titleDetail', {
					vendor: detail?.vendor_name ?? m('vendorStatements.modal.statementFallback')
				})
	);
	const ariaLabel = $derived(
		isCreate ? m('vendorStatements.modal.ariaCreate') : m('vendorStatements.modal.ariaDetail')
	);
</script>

<Modal open {ariaLabel} title={modalTitle} width="lg" {onclose}>
	{#if isCreate}
		<form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
			<div class="form-grid">
				<VendorPicker
					bind:value={vendor_id}
					label={m('vendorStatements.modal.vendor')}
					placeholder={m('vendorStatements.modal.selectVendor')}
					required
					disabled={!canEdit}
				/>
				<label>
					<span>{m('vendorStatements.modal.statementDate')} <em class="required">*</em></span>
					<input type="date" bind:value={statement_date} required disabled={!canEdit} />
				</label>
				<label>
					<span>{m('vendorStatements.modal.statementReference')}</span>
					<input type="text" bind:value={statement_reference} disabled={!canEdit} />
				</label>
				<label>
					<span>{m('vendorStatements.modal.currency')}</span>
					<input type="text" bind:value={currency} maxlength="3" disabled={!canEdit} />
				</label>
			</div>

			<fieldset class="intake-mode">
				<legend>{m('vendorStatements.modal.intakeModeLegend')}</legend>
				<label>
					<input
						type="radio"
						name="intake-mode"
						value="paste"
						checked={intakeMode === 'paste'}
						onchange={() => setIntakeMode('paste')}
						disabled={!canEdit}
					/>
					<span>{m('vendorStatements.modal.intakeModePaste')}</span>
				</label>
				<label>
					<input
						type="radio"
						name="intake-mode"
						value="file"
						checked={intakeMode === 'file'}
						onchange={() => setIntakeMode('file')}
						disabled={!canEdit}
					/>
					<span>{m('vendorStatements.modal.intakeModeFile')}</span>
				</label>
			</fieldset>

			{#if intakeMode === 'paste'}
			<div class="intake-section">
				<div class="intake-title">{m('vendorStatements.modal.statementLines')}</div>
				<p class="intake-hint">{m('vendorStatements.modal.pasteHint')}</p>

				<div class="lines-editor" aria-label={m('vendorStatements.modal.linesEditorAria')}>
					<div class="line-head">
						<span>{m('vendorStatements.modal.colInvoice')}</span>
						<span>{m('vendorStatements.modal.colAmount')}</span>
						<span>{m('vendorStatements.modal.colDate')}</span>
						<span></span>
					</div>
					{#each lines as line, idx (idx)}
						<div class="line-row">
							<input
								type="text"
								bind:value={line.invoice_number}
								placeholder="INV-1001"
								aria-label={m('vendorStatements.modal.lineInvoiceAria', { n: idx + 1 })}
								disabled={!canEdit}
							/>
							<input
								type="number"
								step="0.01"
								min="0"
								value={line.amount ?? ''}
								oninput={(e) => (line.amount = e.currentTarget.value.trim() || null)}
								placeholder="0.00"
								aria-label={m('vendorStatements.modal.lineAmountAria', { n: idx + 1 })}
								disabled={!canEdit}
							/>
							<input
								type="date"
								bind:value={line.invoice_date}
								aria-label={m('vendorStatements.modal.lineDateAria', { n: idx + 1 })}
								disabled={!canEdit}
							/>
							<button
								type="button"
								class="line-remove"
								onclick={() => removeLine(idx)}
								aria-label={m('vendorStatements.modal.removeLineAria', { n: idx + 1 })}
								disabled={!canEdit}
							>
								×
							</button>
						</div>
					{/each}
					{#if canEdit}
						<button type="button" class="line-add" onclick={addLine}
							>{m('vendorStatements.modal.addLine')}</button
						>
					{/if}
				</div>

				<label class="note-field">
					<span>{m('vendorStatements.modal.notes')}</span>
					<input type="text" bind:value={notes} disabled={!canEdit} />
				</label>
			</div>
			{:else}
			<div class="intake-section">
				<div class="intake-title">{m('vendorStatements.modal.uploadFile')}</div>
				<p class="intake-hint">{m('vendorStatements.modal.fileHintCsv')}</p>
				<p class="intake-hint">{m('vendorStatements.modal.fileHintPdf')}</p>

				<label class="file-label">
					<span>{m('vendorStatements.modal.fileAria')}</span>
					<input
						type="file"
						accept=".csv,.pdf,text/csv,application/pdf"
						onchange={onFile}
						aria-label={m('vendorStatements.modal.fileAria')}
						disabled={!canEdit}
					/>
				</label>

				{#if file}
					<div class="file-chosen" data-testid="statement-file-chosen">
						<span class="file-name">{m('vendorStatements.modal.fileSelected', { name: file.name })}</span>
						<button type="button" class="file-clear" onclick={clearFile} disabled={!canEdit}>
							{m('vendorStatements.modal.fileRemove')}
						</button>
					</div>
				{/if}
			</div>
			{/if}

			{#if intakeError}
				<div class="intake-error" role="alert" data-testid="statement-intake-error">
					<strong>{m('vendorStatements.modal.intakeErrorTitle')}</strong>
					<span>{intakeError}</span>
				</div>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={onclose}
					>{m('vendorStatements.modal.cancel')}</button
				>
				{#if canEdit}
					<button type="submit" class="btn-primary" disabled={saving || !canSubmit}>
						{saving
							? m('vendorStatements.modal.reconciling')
							: m('vendorStatements.modal.reconcile')}
					</button>
				{/if}
			</div>
		</form>
	{:else if detail}
		<!-- Detail / diff view -->
		<div class="status-row">
			<Badge tone={RECON_STATUS_TONES[status]} variant={status}>{statusLabel}</Badge>
			<span class="meta-pill">{formatDate(detail.statement_date)}</span>
			{#if detail.statement_reference}
				<span class="meta-pill"
					>{m('vendorStatements.modal.refPill', { reference: detail.statement_reference })}</span
				>
			{/if}
			<span class="meta-pill" data-testid="statement-source">{sourceLabel}</span>
		</div>

		<!-- Provenance: how these lines got here, and the supplier's own document.
		     A reviewer clearing a machine-read run is clearing a model's reading of
		     a PDF, and the run says so rather than presenting the lines as fact. -->
		{#if isMachineRead(detail) || detail.has_source_file}
			<div class="provenance" data-testid="statement-provenance">
				<div class="prov-title">{m('vendorStatements.modal.provenanceTitle')}</div>
				{#if detail.extraction}
					<p class="prov-line">
						{m('vendorStatements.modal.provenanceRead', {
							provider: detail.extraction.provider,
							confidence: formatExtractionConfidence(detail.extraction.confidence),
							n: detail.extraction.line_count
						})}
						<HelpTip term="confidence-score" />
					</p>
					{#if ambiguousSkipCount(detail.extraction) > 0}
						<!-- The reader saw these rows and refused to book them, so the
						     diff below is short by exactly this many supplier rows.
						     `role="alert"` because it changes what the diff means: our
						     invoices for the skipped rows show as `missing_on_their
						     _side` and would otherwise read as a real discrepancy. -->
						<p class="prov-skipped" role="alert" data-testid="statement-skipped-rows">
							{m('vendorStatements.modal.provenanceSkipped', {
								n: ambiguousSkipCount(detail.extraction)
							})}
						</p>
					{:else}
						<p class="prov-note">{m('vendorStatements.modal.provenanceSkipNote')}</p>
					{/if}
				{:else if detail.source_format === 'csv'}
					<p class="prov-line">{m('vendorStatements.modal.provenanceCsv')}</p>
				{/if}
				{#if detail.has_source_file}
					<button
						type="button"
						class="prov-download"
						onclick={downloadSource}
						disabled={downloading}
					>
						{downloading
							? m('vendorStatements.modal.downloadingSource')
							: m('vendorStatements.modal.downloadSource')}
					</button>
				{/if}
			</div>
		{/if}

		<!-- Summary counts -->
		<div class="stat-chips">
			<span class="stat-chip"
				>{m('vendorStatements.modal.statLines', { n: detail.summary.line_count })}</span
			>
			<span class="stat-chip ok"
				>{m('vendorStatements.modal.statMatched', { n: detail.summary.matched_count })}</span
			>
			<span class="stat-chip warn"
				>{m('vendorStatements.modal.statMismatch', { n: detail.summary.amount_mismatch_count })}</span
			>
			<span class="stat-chip flag"
				>{m('vendorStatements.modal.statMissingOurs', {
					n: detail.summary.missing_our_side_count
				})}</span
			>
			<span class="stat-chip flag"
				>{m('vendorStatements.modal.statMissingTheirs', {
					n: detail.summary.missing_their_side_count
				})}</span
			>
		</div>

		<div class="totals-row">
			<div class="total-box">
				<span class="total-label">{m('vendorStatements.modal.statementTotal')}</span>
				<span class="total-value">
					<Money amount={detail.summary.statement_total} currency={detail.currency} mono />
				</span>
			</div>
			<div class="total-box">
				<span class="total-label">{m('vendorStatements.modal.ledgerTotal')}</span>
				<span class="total-value">
					<Money amount={detail.summary.ledger_total} currency={detail.currency} mono />
				</span>
			</div>
		</div>

		<!-- Side-by-side diff table -->
		<div class="diff-section">
			<table class="diff-table">
				<thead>
					<tr>
						<th>{m('vendorStatements.modal.thStatementInv')}</th>
						<th class="right">{m('vendorStatements.modal.thStatementAmount')}</th>
						<th class="right">{m('vendorStatements.modal.thOurAmount')}</th>
						<th class="right">{m('vendorStatements.modal.thDifference')}</th>
						<th>{m('vendorStatements.modal.thClassification')} <HelpTip term="statement-reconciliation" /></th>
						<th>{m('vendorStatements.modal.thResolution')}</th>
					</tr>
				</thead>
				<tbody>
					{#if sortedLines.length === 0}
						<tr><td colspan="6" class="diff-empty">{m('vendorStatements.modal.noLines')}</td></tr>
					{/if}
					{#each sortedLines as line (line.id)}
						<tr>
							<td class="mono">{line.statement_invoice_number ?? '—'}</td>
							<td class="right mono">
								<Money amount={line.statement_amount} currency={detail.currency} />
							</td>
							<td class="right mono">
								{#if line.matched_invoice_number}
									<span class="ledger-ref">{line.matched_invoice_number}</span>
								{/if}
								<Money amount={line.ledger_amount} currency={detail.currency} />
							</td>
							<td class="right mono">
								<Money amount={line.amount_difference} currency={detail.currency} accounting />
							</td>
							<td>
								<span class="cls {classificationTone(line.classification)}">
									{m(RECON_CLASSIFICATION_LABEL_KEYS[line.classification])}
								</span>
							</td>
							<td class="resolution-cell">
								{#if isActionable(line) && canEdit}
									<RowAction
										variant="success"
										disabled={busyLineId === line.id}
										onclick={() => runResolve(line, 'resolved')}
										ariaLabel={m('vendorStatements.modal.resolveAria', {
											line: line.statement_invoice_number ?? line.id
										})}
									>
										{m('vendorStatements.modal.resolve')}
									</RowAction>
									<RowAction
										disabled={busyLineId === line.id}
										onclick={() => runResolve(line, 'ignored')}
										ariaLabel={m('vendorStatements.modal.ignoreAria', {
											line: line.statement_invoice_number ?? line.id
										})}
									>
										{m('vendorStatements.modal.ignore')}
									</RowAction>
								{:else}
									<span class="res-state {line.resolution_status}">
										{m(RECON_RESOLUTION_LABEL_KEYS[line.resolution_status])}
									</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}
				>{m('vendorStatements.modal.close')}</button
			>
		</div>
	{/if}
</Modal>

<style>
	.status-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 12px;
		flex-wrap: wrap;
	}

	/* The status pill is `<Badge>` now; the tone per status lives beside the
	   labels in `types/vendorStatementRecon`, so this modal and the list page
	   can't drift apart again. */

	.meta-pill {
		font-size: 0.72rem;
		padding: 2px 8px;
		border-radius: 8px;
		background: var(--bg);
		color: var(--text-muted);
	}

	/* --- Provenance --- */
	.provenance {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 4px;
		margin-bottom: 12px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
	}
	.prov-title {
		font-size: 0.78rem;
		font-weight: 600;
		color: var(--text);
	}
	.prov-line {
		margin: 0;
		font-size: 0.78rem;
		color: var(--text);
	}
	.prov-note {
		margin: 0;
		font-size: 0.74rem;
		color: var(--text-muted);
	}
	/* Louder than `.prov-note`: this one says the diff below is incomplete, not
	   merely how the reader behaves. Amber, matching the warning tone used for
	   the `amount_mismatch` stat chip. */
	.prov-skipped {
		margin: 0;
		font-size: 0.76rem;
		color: #d4940a;
	}
	.prov-download {
		margin-top: 4px;
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-muted);
		border-radius: 5px;
		padding: 4px 12px;
		cursor: pointer;
		font-family: inherit;
		font-size: 0.78rem;
	}
	.prov-download:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.prov-download:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* --- Summary stat chips --- */
	.stat-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-bottom: 12px;
	}
	.stat-chip {
		font-size: 0.72rem;
		font-weight: 600;
		padding: 3px 9px;
		border-radius: 8px;
		background: var(--bg);
		color: var(--text-muted);
	}
	/* Not `<Badge>`: these are counts in a summary row, not a status — they
	   keep their own denser metrics (8px radius, sentence case) and read as a
	   group. Only the colour literals are retired to the palette pairs. */
	.stat-chip.ok {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}
	.stat-chip.warn {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}
	.stat-chip.flag {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	/* --- Totals --- */
	.totals-row {
		display: flex;
		gap: 12px;
		margin-bottom: 16px;
	}
	.total-box {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 8px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
	}
	.total-label {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.total-value {
		font-size: 0.95rem;
		font-weight: 600;
	}

	/* --- Diff table --- */
	.diff-section {
		margin-top: 4px;
	}
	.diff-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.diff-table th {
		text-align: left;
		padding: 5px 6px;
		color: var(--text-muted);
		font-weight: 500;
		border-bottom: 1px solid var(--border);
	}
	.diff-table th.right {
		text-align: right;
	}
	.diff-table td {
		padding: 6px;
		border-bottom: 1px solid var(--border);
		vertical-align: middle;
	}
	.diff-table td.right {
		text-align: right;
	}
	.diff-empty {
		text-align: center;
		color: var(--text-muted);
		padding: 16px;
	}
	.mono {
		font-variant-numeric: tabular-nums;
		font-family: ui-monospace, 'SF Mono', 'Cascadia Code', Menlo, Consolas, monospace;
	}
	.ledger-ref {
		display: block;
		font-size: 0.7rem;
		color: var(--text-muted);
	}

	.cls {
		display: inline-block;
		padding: 2px 8px;
		border-radius: 8px;
		font-size: 0.7rem;
		font-weight: 600;
	}
	/* Not `<Badge>`: a per-line classification chip inside a dense diff table,
	   deliberately smaller than the run's status pill (0.7rem, 8px radius, no
	   uppercase) so a table of them stays scannable. Colour comes from the
	   palette pairs. */
	.cls.ok {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}
	.cls.warn {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}
	.cls.flag {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	.resolution-cell {
		display: flex;
		align-items: center;
		gap: 6px;
		white-space: nowrap;
	}
	.res-state {
		font-size: 0.72rem;
		font-weight: 600;
		color: var(--text-muted);
	}
	.res-state.resolved {
		color: #1fa86a;
	}
	.res-state.ignored {
		color: var(--text-muted);
		font-style: italic;
	}

	/* --- Create form --- */
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.form-grid label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.form-grid label.full-width {
		grid-column: 1 / -1;
	}
	.form-grid input,
	.form-grid select {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}
	.form-grid input:disabled,
	.form-grid select:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.intake-section {
		margin-top: 16px;
	}
	.intake-title {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 4px;
	}
	.intake-hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 0 0 10px;
	}

	.lines-editor {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.line-head,
	.line-row {
		display: grid;
		grid-template-columns: 1.4fr 1fr 1fr 32px;
		gap: 8px;
		align-items: center;
	}
	.line-head {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.line-row input {
		padding: 6px 8px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.84rem;
	}
	.line-remove {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		border-radius: 5px;
		height: 30px;
		cursor: pointer;
		font-size: 1rem;
		line-height: 1;
	}
	.line-remove:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}
	.line-remove:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.line-add {
		align-self: flex-start;
		margin-top: 2px;
		border: 1px dashed var(--border);
		background: transparent;
		color: var(--text-muted);
		border-radius: 5px;
		padding: 5px 12px;
		cursor: pointer;
		font-family: inherit;
		font-size: 0.8rem;
	}
	.line-add:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.file-label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 14px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.note-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 14px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.note-field input {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	/* --- Intake mode picker --- */
	.intake-mode {
		display: flex;
		align-items: center;
		gap: 18px;
		flex-wrap: wrap;
		margin: 16px 0 0;
		padding: 0;
		border: 0;
	}
	.intake-mode legend {
		padding: 0;
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 6px;
	}
	.intake-mode label {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.84rem;
		color: var(--text);
		cursor: pointer;
	}
	.intake-mode input:disabled + span {
		opacity: 0.6;
	}

	/* --- Chosen file --- */
	.file-chosen {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 8px;
		flex-wrap: wrap;
	}
	.file-name {
		font-size: 0.8rem;
		color: var(--text);
		word-break: break-all;
	}
	.file-clear {
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-muted);
		border-radius: 5px;
		padding: 3px 10px;
		cursor: pointer;
		font-family: inherit;
		font-size: 0.76rem;
	}
	.file-clear:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}
	.file-clear:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* --- Inline intake refusal ---
	   Persistent by design: the backend refuses a statement it can't read
	   honestly and explains why, and that explanation is the actionable part. */
	.intake-error {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin-top: 14px;
		padding: 10px 12px;
		border: 1px solid rgba(224, 64, 64, 0.4);
		border-radius: 6px;
		background: rgba(224, 64, 64, 0.08);
		font-size: 0.82rem;
		color: var(--text);
	}
	.intake-error strong {
		color: var(--danger);
		font-size: 0.8rem;
	}
</style>
