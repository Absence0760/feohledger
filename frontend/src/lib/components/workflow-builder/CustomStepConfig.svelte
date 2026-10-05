<script lang="ts">
	import type {
		WorkflowStepType,
		WebhookStepConfig,
		EmailStepConfig,
		DelayStepConfig,
		WebhookMethod,
		EmailRecipientKind,
	} from '#lib/types/workflow.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		headersToRows,
		rowsMatchHeaders,
		rowsToHeaders,
		type HeaderRow
	} from '#lib/utils/webhookHeaders.ts';
	import { untrack } from 'svelte';

	type Props = {
		type: Extract<WorkflowStepType, 'webhook' | 'email' | 'delay'>;
		config: WebhookStepConfig | EmailStepConfig | DelayStepConfig;
		onchange: (config: WebhookStepConfig | EmailStepConfig | DelayStepConfig) => void;
	};

	let { type, config, onchange }: Props = $props();

	// ── Webhook ──
	let webhook = $derived(config as WebhookStepConfig);

	function patchWebhook(p: Partial<WebhookStepConfig>) {
		onchange({ ...webhook, ...p });
	}

	// Headers persist as an OBJECT but are edited as key/value ROWS, and the two
	// shapes are not interchangeable: an object cannot hold a blank key, which
	// is exactly what a row being typed has. So the rows are local STATE seeded
	// from the config — not a `$derived` round-trip through it.
	//
	// As a `$derived` this was a dead control: "+ Add header" appended
	// `['', '']`, the projection dropped it, `onchange` handed back a
	// byte-identical config, and the row re-derived away. No blank row ever
	// appeared, so a webhook header could not be added through the UI at all.
	// The same round trip made clearing an existing header's NAME delete the
	// whole row mid-edit, including the value the user had not touched.
	// `untrack` states the intent the compiler would otherwise warn about: this
	// is a SEED, read once. The $effect below owns every later re-seed.
	let headerRows = $state<HeaderRow[]>(untrack(() => headersToRows(webhook.headers)));

	// Re-seed only when the incoming config no longer describes these rows — a
	// different step selected, a version restored. `rowsMatchHeaders` ignores
	// key ORDER and projects blank-key rows away, so a row being typed is NOT a
	// mismatch and survives. `untrack` on the read so writing the state this
	// effect also inspects can't loop.
	$effect(() => {
		const incoming = webhook.headers;
		if (rowsMatchHeaders(untrack(() => headerRows), incoming)) return;
		headerRows = headersToRows(incoming);
	});

	/** Apply an edit locally, then push its projection down to the config. */
	function commitRows(rows: HeaderRow[]) {
		headerRows = rows;
		patchWebhook({ headers: rowsToHeaders(rows) });
	}

	function patchHeaderKey(idx: number, key: string) {
		commitRows(headerRows.map((r, i) => (i === idx ? ([key, r[1]] as HeaderRow) : r)));
	}

	function patchHeaderValue(idx: number, value: string) {
		commitRows(headerRows.map((r, i) => (i === idx ? ([r[0], value] as HeaderRow) : r)));
	}

	function addHeader() {
		commitRows([...headerRows, ['', '']]);
	}

	function removeHeader(idx: number) {
		commitRows(headerRows.filter((_, i) => i !== idx));
	}

	// ── Email ──
	let email = $derived(config as EmailStepConfig);

	function patchEmail(p: Partial<EmailStepConfig>) {
		onchange({ ...email, ...p });
	}

	function parseAddresses(raw: string): string[] {
		return raw
			.split(',')
			.map((s) => s.trim())
			.filter((s) => s.length > 0);
	}

	// ── Delay ──
	let delay = $derived(config as DelayStepConfig);

	function patchDelay(p: Partial<DelayStepConfig>) {
		onchange({ ...delay, ...p });
	}
</script>

{#if type === 'webhook'}
	<div class="custom">
		<div class="field">
			<label for="wh-url">{m('workflows.builder.webhook.url')}</label>
			<input
				id="wh-url"
				type="url"
				placeholder="https://example.com/hook"
				value={webhook.url}
				oninput={(e) => patchWebhook({ url: e.currentTarget.value })}
			/>
		</div>
		<div class="row">
			<div class="field">
				<label for="wh-method">{m('workflows.builder.webhook.method')}</label>
				<select
					id="wh-method"
					value={webhook.method}
					onchange={(e) => patchWebhook({ method: e.currentTarget.value as WebhookMethod })}
				>
					<option value="POST">POST</option>
					<option value="GET">GET</option>
					<option value="PUT">PUT</option>
				</select>
			</div>
			<div class="field">
				<label for="wh-timeout">{m('workflows.builder.webhook.timeout')}</label>
				<input
					id="wh-timeout"
					type="number"
					min="1"
					value={webhook.timeout_seconds}
					oninput={(e) =>
						patchWebhook({
							timeout_seconds: Math.max(1, parseInt(e.currentTarget.value, 10) || 1),
						})}
				/>
			</div>
		</div>
		<div class="field">
			<div class="label-row">
				<span class="field-label">{m('workflows.builder.webhook.headers')}</span>
				<button type="button" class="link-btn" onclick={addHeader}>{m('workflows.builder.webhook.addHeader')}</button>
			</div>
			{#each headerRows as [key, value], idx (idx)}
				<div class="kv-row">
					<input
						type="text"
						aria-label={m('workflows.builder.webhook.headerName')}
						placeholder={m('workflows.builder.webhook.headerNamePlaceholder')}
						value={key}
						oninput={(e) => patchHeaderKey(idx, e.currentTarget.value)}
					/>
					<input
						type="text"
						aria-label={m('workflows.builder.webhook.headerValue')}
						placeholder={m('workflows.builder.webhook.headerValuePlaceholder')}
						{value}
						oninput={(e) => patchHeaderValue(idx, e.currentTarget.value)}
					/>
					<button
						type="button"
						class="icon-btn danger"
						title={m('workflows.builder.webhook.removeHeader')}
						aria-label={m('workflows.builder.webhook.removeHeader')}
						onclick={() => removeHeader(idx)}
					>×</button>
				</div>
			{/each}
		</div>
		<div class="field">
			<label for="wh-body">{m('workflows.builder.webhook.bodyTemplate')}</label>
			<textarea
				id="wh-body"
				rows="4"
				placeholder={'{"invoice_id": "{{invoice.id}}", "amount": "{{invoice.amount}}"}'}
				value={webhook.body_template ?? ''}
				oninput={(e) => patchWebhook({ body_template: e.currentTarget.value || null })}
			></textarea>
			<p class="hint">
				{m('workflows.builder.webhook.bodyHint')}
			</p>
		</div>
	</div>
{:else if type === 'email'}
	<div class="custom">
		<div class="field">
			<label for="em-to">{m('workflows.builder.email.recipients')}</label>
			<select
				id="em-to"
				value={email.to}
				onchange={(e) => patchEmail({ to: e.currentTarget.value as EmailRecipientKind })}
			>
				<option value="approver">{m('workflows.builder.email.recipientApprover')}</option>
				<option value="vendor">{m('workflows.builder.email.recipientVendor')}</option>
				<option value="custom">{m('workflows.builder.email.recipientCustom')}</option>
			</select>
		</div>
		{#if email.to === 'custom'}
			<div class="field">
				<label for="em-addrs">{m('workflows.builder.email.addresses')}</label>
				<input
					id="em-addrs"
					type="text"
					placeholder="alice@corp.com, bob@corp.com"
					value={email.to_addresses.join(', ')}
					oninput={(e) => patchEmail({ to_addresses: parseAddresses(e.currentTarget.value) })}
				/>
			</div>
		{/if}
		<div class="field">
			<label for="em-subject">{m('workflows.builder.email.subject')}</label>
			<input
				id="em-subject"
				type="text"
				placeholder={m('workflows.builder.email.subjectPlaceholder')}
				value={email.subject}
				oninput={(e) => patchEmail({ subject: e.currentTarget.value })}
			/>
		</div>
		<div class="field">
			<label for="em-body">{m('workflows.builder.email.bodyTemplate')}</label>
			<textarea
				id="em-body"
				rows="5"
				placeholder={m('workflows.builder.email.bodyPlaceholder')}
				value={email.body_template}
				oninput={(e) => patchEmail({ body_template: e.currentTarget.value })}
			></textarea>
			<p class="hint">{m('workflows.builder.email.bodyHint')}</p>
		</div>
	</div>
{:else if type === 'delay'}
	<div class="custom">
		<div class="field">
			<label for="dl-duration">{m('workflows.builder.delay.duration')}</label>
			<input
				id="dl-duration"
				type="number"
				min="0"
				value={delay.duration_seconds}
				oninput={(e) =>
					patchDelay({ duration_seconds: Math.max(0, parseInt(e.currentTarget.value, 10) || 0) })}
			/>
			<p class="hint">{m('workflows.builder.delay.hours', { hours: (delay.duration_seconds / 3600).toFixed(2) })}</p>
		</div>
		<div class="field">
			<label for="dl-until">{m('workflows.builder.delay.untilField')}</label>
			<input
				id="dl-until"
				type="text"
				placeholder={m('workflows.builder.delay.untilFieldPlaceholder')}
				value={delay.until_field ?? ''}
				oninput={(e) => patchDelay({ until_field: e.currentTarget.value || null })}
			/>
			<p class="hint">
				{m('workflows.builder.delay.untilHint')}
			</p>
		</div>
	</div>
{/if}

<style>
	.custom {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.field {
		display: flex;
		flex-direction: column;
	}

	.field label,
	.field-label {
		display: block;
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 5px;
	}

	.field input,
	.field select,
	.field textarea {
		width: 100%;
		padding: 8px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
		outline: none;
		box-sizing: border-box;
	}

	.field textarea {
		resize: vertical;
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.82rem;
	}

	.field input:focus,
	.field select:focus,
	.field textarea:focus {
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.label-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 5px;
	}

	.label-row .field-label {
		margin-bottom: 0;
	}

	.kv-row {
		display: grid;
		grid-template-columns: 1fr 1.3fr 32px;
		gap: 8px;
		align-items: center;
		margin-bottom: 8px;
	}

	.icon-btn {
		width: 28px;
		height: 28px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text-muted);
		cursor: pointer;
		font-size: 14px;
		font-family: inherit;
	}

	.icon-btn.danger:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	.link-btn {
		background: none;
		border: none;
		color: var(--accent);
		cursor: pointer;
		font-size: 0.8rem;
		padding: 0;
		font-family: inherit;
	}

	.link-btn:hover {
		filter: brightness(1.2);
	}

	.hint {
		margin: 5px 0 0;
		font-size: 0.78rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
</style>
