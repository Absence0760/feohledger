<script lang="ts" module>
	import { workflowStore } from '#lib/stores/workflows.svelte.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	/**
	 * Export a workflow definition as a downloaded `.json` file. Exposed at the
	 * module level so a list-row "Export" action can call it directly without
	 * mounting the import modal. Pulls the `WorkflowExport` JSON from the store
	 * and triggers a client-side download via an object URL.
	 */
	export async function exportWorkflowToFile(id: string, name: string): Promise<void> {
		try {
			const data = await workflowStore.exportDefinition(id);
			const json = JSON.stringify(data, null, 2);
			const blob = new Blob([json], { type: 'application/json' });
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `${slugify(name) || 'workflow'}.json`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
			toast(m('workflows.mgmt.exported', { name }), 'success');
		} catch (e) {
			toast(e instanceof Error ? e.message : m('workflows.mgmt.exportFailed'), 'error');
		}
	}

	function slugify(name: string): string {
		return name
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/^-+|-+$/g, '');
	}
</script>

<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { goto } from '$app/navigation';
	import type { WorkflowExport } from '#lib/types/workflow.ts';

	let {
		open,
		onclose,
	}: {
		open: boolean;
		onclose: () => void;
	} = $props();

	let rawJson = $state('');
	let name = $state('');
	let importing = $state(false);
	let validationErrors = $state<string[]>([]);

	function reset() {
		rawJson = '';
		name = '';
		validationErrors = [];
	}

	function close() {
		reset();
		onclose();
	}

	async function onFileChange(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		validationErrors = [];
		try {
			rawJson = await file.text();
			// Pre-fill the name from the parsed definition when present.
			try {
				const parsed = JSON.parse(rawJson) as Partial<WorkflowExport>;
				if (parsed?.name && !name.trim()) name = parsed.name;
			} catch {
				/* leave rawJson; submit-time validation reports the parse error */
			}
		} catch {
			validationErrors = [m('workflows.mgmt.import.errFileRead')];
		}
		// Reset the input so re-selecting the same file fires change again.
		input.value = '';
	}

	function parseDefinition(): WorkflowExport | null {
		validationErrors = [];
		const errs: string[] = [];
		if (!rawJson.trim()) {
			errs.push(m('workflows.mgmt.import.errEmpty'));
			validationErrors = errs;
			return null;
		}
		let parsed: unknown;
		try {
			parsed = JSON.parse(rawJson);
		} catch {
			validationErrors = [m('workflows.mgmt.import.errInvalidJson')];
			return null;
		}
		const def = parsed as Partial<WorkflowExport>;
		if (typeof def !== 'object' || def === null) {
			errs.push(m('workflows.mgmt.import.errNotObject'));
		}
		if (!def?.steps_config || typeof def.steps_config !== 'object') {
			errs.push(m('workflows.mgmt.import.errMissingSteps'));
		} else if (!Array.isArray((def.steps_config as { steps?: unknown }).steps)) {
			errs.push(m('workflows.mgmt.import.errStepsArray'));
		}
		if (errs.length > 0) {
			validationErrors = errs;
			return null;
		}
		return def as WorkflowExport;
	}

	async function handleImport() {
		const definition = parseDefinition();
		if (!definition) return;
		importing = true;
		try {
			const created = await workflowStore.importDefinition({
				name: name.trim() || null,
				definition,
			});
			toast(m('workflows.mgmt.imported', { name: created.name }), 'success');
			close();
			await goto(`/workflows/${created.id}`);
		} catch (e) {
			// Surface server-side validation (e.g. unknown step type) inline.
			const msg = e instanceof Error ? e.message : m('workflows.mgmt.importFailed');
			validationErrors = [msg];
		} finally {
			importing = false;
		}
	}
</script>

<Modal {open} ariaLabel={m('workflows.mgmt.import.aria')} title={m('workflows.mgmt.import.title')} width="md" onclose={close}>
	<div class="ie-body">
		<div class="form-group">
			<label for="ie-name">{m('workflows.mgmt.import.name')} <span class="ie-optional">{m('workflows.mgmt.import.nameHint')}</span></label>
			<input id="ie-name" type="text" bind:value={name} placeholder={m('workflows.mgmt.import.namePlaceholder')} />
		</div>

		<div class="form-group">
			<label for="ie-file">{m('workflows.mgmt.import.chooseFile')}</label>
			<input id="ie-file" type="file" accept="application/json,.json" onchange={onFileChange} />
		</div>

		<div class="form-group">
			<label for="ie-json">{m('workflows.mgmt.import.pasteJson')}</label>
			<textarea
				id="ie-json"
				bind:value={rawJson}
				rows="10"
				placeholder={'{\n  "schema_version": 1,\n  "name": "…",\n  "steps_config": { "steps": [ … ] }\n}'}
			></textarea>
		</div>

		{#if validationErrors.length > 0}
			<ul class="ie-errors" aria-label={m('workflows.mgmt.import.errorsAria')}>
				{#each validationErrors as err, i (i)}
					<li>{err}</li>
				{/each}
			</ul>
		{/if}
	</div>

	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={close}>{m('common.cancel')}</button>
		<button type="button" class="btn-import" disabled={importing} onclick={handleImport}>
			{importing ? m('workflows.mgmt.import.importing') : m('workflows.mgmt.import.import')}
		</button>
	</div>
</Modal>

<style>
	.ie-body {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.form-group {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}

	.form-group label {
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.ie-optional {
		text-transform: none;
		font-weight: 400;
		letter-spacing: normal;
	}

	.form-group input[type='text'],
	.form-group textarea {
		width: 100%;
		padding: 8px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.86rem;
		box-sizing: border-box;
	}

	.form-group textarea {
		resize: vertical;
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.8rem;
	}

	.ie-errors {
		list-style: none;
		margin: 0;
		padding: 10px 12px;
		border-radius: 6px;
		background: rgba(240, 70, 70, 0.1);
		color: var(--danger);
		font-size: 0.82rem;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.btn-cancel {
		padding: 8px 16px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-import {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-import:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
