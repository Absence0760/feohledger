<script lang="ts">
	// Save-as-report dialog. Collects a name (required) + optional description;
	// the parent owns the actual POST/PATCH and passes save state back in.
	import Modal from '#lib/components/ui/Modal.svelte';

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
	ariaLabel={editing ? 'Save report changes' : 'Save report'}
	title={editing ? 'Save changes' : 'Save report'}
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
			<label for="report-name">Name <em class="required">*</em></label>
			<input id="report-name" type="text" bind:value={name} placeholder="Vendor spend by month" />
		</div>
		<div class="field">
			<label for="report-desc">Description</label>
			<textarea
				id="report-desc"
				rows="3"
				bind:value={description}
				placeholder="Optional notes about what this report shows"
			></textarea>
		</div>
		{#if error}
			<p class="error-banner">{error}</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>Cancel</button>
			<button type="submit" class="btn-primary" disabled={saving || !name.trim()}>
				{saving ? 'Saving…' : editing ? 'Save changes' : 'Save report'}
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
