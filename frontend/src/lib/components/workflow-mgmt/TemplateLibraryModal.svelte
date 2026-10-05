<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { workflowStore } from '#lib/stores/workflows.svelte.ts';
	import type { WorkflowTemplate } from '#lib/types/workflow.ts';
	import { goto } from '$app/navigation';

	let {
		open,
		onclose,
	}: {
		open: boolean;
		onclose: () => void;
	} = $props();

	let templates = $state<WorkflowTemplate[]>([]);
	let loading = $state(false);
	let error = $state('');
	// The template whose "Use template" name field is open, keyed by template key.
	let namingKey = $state<string | null>(null);
	let nameInput = $state('');
	let creatingKey = $state<string | null>(null);

	// Group templates by category for the gallery layout. `$derived` so the
	// grouping recomputes whenever the loaded set changes.
	let grouped = $derived.by(() => {
		const map = new Map<string, WorkflowTemplate[]>();
		for (const t of templates) {
			const list = map.get(t.category) ?? [];
			list.push(t);
			map.set(t.category, list);
		}
		return [...map.entries()].map(([category, items]) => ({ category, items }));
	});

	// Lazy-load the template catalogue the first time the modal opens.
	$effect(() => {
		if (open && templates.length === 0 && !loading && !error) {
			void loadTemplates();
		}
	});

	async function loadTemplates() {
		loading = true;
		error = '';
		try {
			templates = await workflowStore.listTemplates();
		} catch (e) {
			error = e instanceof Error ? e.message : m('workflows.mgmt.tpl.loadFailed');
		} finally {
			loading = false;
		}
	}

	function startNaming(t: WorkflowTemplate) {
		namingKey = t.key;
		nameInput = t.name;
	}

	function cancelNaming() {
		namingKey = null;
		nameInput = '';
	}

	async function useTemplate(t: WorkflowTemplate) {
		const name = nameInput.trim() || t.name;
		creatingKey = t.key;
		try {
			const created = await workflowStore.createFromTemplate(t.key, name);
			toast(m('workflows.mgmt.tpl.created', { name: created.name }), 'success');
			onclose();
			await goto(`/workflows/${created.id}`);
		} catch (e) {
			toast(e instanceof Error ? e.message : m('workflows.mgmt.tpl.createFailed'), 'error');
		} finally {
			creatingKey = null;
		}
	}

	function stepCount(t: WorkflowTemplate): number {
		return t.steps_config?.steps?.length ?? 0;
	}
</script>

<Modal {open} ariaLabel={m('workflows.mgmt.tpl.aria')} title={m('workflows.mgmt.tpl.title')} width="lg" {onclose}>
	<div class="tl-body">
		{#if loading}
			<p class="tl-status">{m('workflows.mgmt.tpl.loading')}</p>
		{:else if error}
			<p class="tl-status tl-error" role="alert">{error}</p>
		{:else if templates.length === 0}
			<p class="tl-status">{m('workflows.mgmt.tpl.empty')}</p>
		{:else}
			{#each grouped as group (group.category)}
				<section class="tl-group">
					<h4 class="tl-category">{group.category}</h4>
					<div class="tl-grid">
						{#each group.items as t (t.key)}
							<div class="tl-card" data-template-key={t.key}>
								<div class="tl-card-head">
									<span class="tl-name">{t.name}</span>
									<span class="tl-steps">{m('workflows.mgmt.tpl.steps', { n: stepCount(t) })}</span>
								</div>
								<p class="tl-desc">{t.description}</p>
								{#if namingKey === t.key}
									<div class="tl-name-row">
										<input
											type="text"
											bind:value={nameInput}
											aria-label={m('workflows.mgmt.tpl.nameFor', { name: t.name })}
											placeholder={m('workflows.mgmt.tpl.namePlaceholder')}
										/>
										<button
											class="tl-use"
											disabled={creatingKey === t.key}
											onclick={() => useTemplate(t)}
										>
											{creatingKey === t.key ? m('workflows.mgmt.tpl.creating') : m('workflows.mgmt.tpl.create')}
										</button>
										<button class="tl-cancel" onclick={cancelNaming}>{m('common.cancel')}</button>
									</div>
								{:else}
									<button class="tl-use" onclick={() => startNaming(t)}>{m('workflows.mgmt.tpl.use')}</button>
								{/if}
							</div>
						{/each}
					</div>
				</section>
			{/each}
		{/if}
	</div>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('workflows.mgmt.close')}</button>
	</div>
</Modal>

<style>
	.tl-body {
		max-height: 60vh;
		overflow-y: auto;
	}

	.tl-status {
		text-align: center;
		color: var(--text-muted);
		padding: 32px 0;
	}

	.tl-error {
		color: var(--danger);
	}

	.tl-group {
		margin-bottom: 18px;
	}

	.tl-category {
		margin: 0 0 8px;
		font-size: 0.74rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}

	.tl-grid {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 12px;
	}

	.tl-card {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 12px 14px;
		background: var(--bg);
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.tl-card-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
	}

	.tl-name {
		font-weight: 600;
		font-size: 0.92rem;
	}

	.tl-steps {
		font-size: 0.72rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.tl-desc {
		margin: 0;
		font-size: 0.82rem;
		color: var(--text-muted);
		flex: 1;
	}

	.tl-name-row {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.tl-name-row input {
		flex: 1;
		min-width: 0;
		padding: 6px 8px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.82rem;
	}

	.tl-use {
		padding: 6px 14px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
	}

	.tl-use:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.tl-cancel {
		padding: 6px 12px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		cursor: pointer;
		font-family: inherit;
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
</style>
