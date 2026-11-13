<script lang="ts">
	// Save-as-report dialog. Collects a name (required) + optional description;
	// the parent owns the actual POST/PATCH and passes save state back in.
	import Modal from '#lib/components/ui/Modal.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	interface Props {
		open: boolean;
		/** Pre-fill (editing an existing definition). */
		initialName?: string;
		initialDescription?: string;
		/** True while the parent's save request is in flight. */
		saving?: boolean;
		/** Non-null renders an inline error banner. */
		error?: string | null;
		/** "Save report" for a new one, "Save changes" when editing. */
		editing?: boolean;
		onsave: (data: { name: string; description: string }) => void;
		onclose: () => void;
	}
	let {
		open,
		initialName = '',
		initialDescription = '',
		saving = false,
		error = null,
		editing = false,
		onsave,
		onclose
	}: Props = $props();

	let name = $state('');
	let description = $state('');

	// Re-seed the fields whenever the dialog opens.
	$effect(() => {
		if (open) {
			name = initialName;
			description = initialDescription;
		}
	});

	function submit() {
		if (!name.trim()) return;
		onsave({ name: name.trim(), description: description.trim() });
	}
</script>

<Modal
	{open}
	ariaLabel={editing ? m('reports.save.ariaEdit') : m('reports.action.save')}
	title={editing ? m('reports.action.saveChanges') : m('reports.action.save')}
	width="sm"
	{onclose}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			submit();
		}}
	>
		<div class="field">
			<label for="report-name">{m('reports.save.name')} <em class="required">*</em></label>
			<input
				id="report-name"
				type="text"
				bind:value={name}
				placeholder={m('reports.save.namePlaceholder')}
			/>
		</div>
		<div class="field">
			<label for="report-desc">{m('reports.save.description')}</label>
			<textarea
				id="report-desc"
				rows="3"
				bind:value={description}
				placeholder={m('reports.save.descriptionPlaceholder')}
			></textarea>
		</div>
		{#if error}
			<p class="error-banner">{error}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving || !name.trim()}>
				{saving
					? m('common.saving')
					: editing
						? m('reports.action.saveChanges')
						: m('reports.action.save')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 12px;
	}
	.field label {
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.required {
		color: var(--danger);
		font-style: normal;
	}
	.error-banner {
		color: var(--danger);
		margin: 8px 0;
	}
</style>
