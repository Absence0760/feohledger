<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { workflowStore } from '#lib/stores/workflows.svelte.ts';
	import type { WorkflowVersion, WorkflowDiff } from '#lib/types/workflow.ts';

	let {
		open,
		workflowId,
		workflowName,
		onclose,
		onrestored,
	}: {
		open: boolean;
		workflowId: string;
		workflowName: string;
		onclose: () => void;
		// Fired after a successful restore so the list page can refetch.
		onrestored?: () => void;
	} = $props();

	let versions = $state<WorkflowVersion[]>([]);
	let loading = $state(false);
	let error = $state('');

	// Two selected version ids for the diff (oldest → newest convention).
	let fromId = $state('');
	let toId = $state('');

	let diff = $state<WorkflowDiff | null>(null);
	let diffing = $state(false);

	let confirmRestoreId = $state<string | null>(null);
	let restoringId = $state<string | null>(null);

	// (Re)load whenever the modal opens for a given workflow.
	$effect(() => {
		if (open && workflowId) {
			void loadVersions();
		}
	});

	async function loadVersions() {
		loading = true;
		error = '';
		diff = null;
		confirmRestoreId = null;
		try {
			versions = await workflowStore.listVersions(workflowId);
			// Default the diff selectors to the two most recent versions.
			if (versions.length >= 2) {
				fromId = versions[1].id;
				toId = versions[0].id;
			} else if (versions.length === 1) {
				fromId = versions[0].id;
				toId = versions[0].id;
			}
		} catch (e) {
			error = e instanceof Error ? e.message : m('workflows.mgmt.ver.loadFailed');
		} finally {
			loading = false;
		}
	}

	async function runDiff() {
		if (!fromId || !toId) return;
		diffing = true;
		try {
			diff = await workflowStore.diffVersions(workflowId, fromId, toId);
		} catch (e) {
			toast(e instanceof Error ? e.message : m('workflows.mgmt.ver.diffFailed'), 'error');
		} finally {
			diffing = false;
		}
	}

	async function restore(v: WorkflowVersion) {
		restoringId = v.id;
		try {
			await workflowStore.restoreVersion(workflowId, v.id);
			toast(m('workflows.mgmt.ver.restored', { n: v.version_number }), 'success');
			confirmRestoreId = null;
			onrestored?.();
			await loadVersions();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('workflows.mgmt.ver.restoreFailed'), 'error');
		} finally {
			restoringId = null;
		}
	}

	function fmtDate(iso: string): string {
		if (!iso) return '—';
		return new Date(iso).toLocaleString('en-US', {
			month: 'short',
			day: 'numeric',
			year: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
	}

	function changeText(c: WorkflowDiff['changes'][number]): string {
		return c.summary;
	}
</script>

<Modal {open} ariaLabel={m('workflows.mgmt.ver.aria')} title={m('workflows.mgmt.ver.title', { name: workflowName })} width="lg" {onclose}>
	<div class="vh-body">
		{#if loading}
			<p class="vh-status">{m('workflows.mgmt.ver.loading')}</p>
		{:else if error}
			<p class="vh-status vh-error">{error}</p>
		{:else if versions.length === 0}
			<p class="vh-status">{m('workflows.mgmt.ver.empty')}</p>
		{:else}
			<div class="vh-diff-controls">
				<label>
					{m('workflows.mgmt.ver.from')}
					<select bind:value={fromId} aria-label={m('workflows.mgmt.ver.fromAria')}>
						{#each versions as v (v.id)}
							<option value={v.id}>v{v.version_number} — {fmtDate(v.created_at)}</option>
						{/each}
					</select>
				</label>
				<label>
					{m('workflows.mgmt.ver.to')}
					<select bind:value={toId} aria-label={m('workflows.mgmt.ver.toAria')}>
						{#each versions as v (v.id)}
							<option value={v.id}>v{v.version_number} — {fmtDate(v.created_at)}</option>
						{/each}
					</select>
				</label>
				<button class="vh-diff-btn" disabled={diffing || !fromId || !toId} onclick={runDiff}>
					{diffing ? m('workflows.mgmt.ver.comparing') : m('workflows.mgmt.ver.compare')}
				</button>
			</div>

			{#if diff}
				<div class="vh-diff" aria-label={m('workflows.mgmt.ver.diffAria')}>
					<h4>
						{m('workflows.mgmt.ver.changesFromTo', { from: diff.from_version, to: diff.to_version })}
					</h4>
					{#if diff.changes.length === 0}
						<p class="vh-status">{m('workflows.mgmt.ver.noDiff')}</p>
					{:else}
						<ul class="vh-changes">
							{#each diff.changes as c, i (i)}
								<li class="vh-change kind-{c.kind}">
									<span class="vh-kind">{c.kind}</span>
									<span class="vh-change-text">{changeText(c)}</span>
								</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}

			<h4 class="vh-list-title">{m('workflows.mgmt.ver.allVersions')}</h4>
			<ul class="vh-list">
				{#each versions as v (v.id)}
					<li class="vh-row">
						<div class="vh-row-main">
							<span class="vh-vnum">v{v.version_number}</span>
							<span class="vh-meta">{fmtDate(v.created_at)}</span>
							{#if v.note}<span class="vh-note">{v.note}</span>{/if}
						</div>
						{#if confirmRestoreId === v.id}
							<div class="vh-row-actions">
								<button
									class="vh-restore armed"
									disabled={restoringId === v.id}
									onclick={() => restore(v)}
								>
									{restoringId === v.id ? m('workflows.mgmt.ver.restoring') : m('workflows.mgmt.ver.confirmRestore')}
								</button>
								<button class="vh-cancel" onclick={() => (confirmRestoreId = null)}>{m('common.cancel')}</button>
							</div>
						{:else}
							<button class="vh-restore" onclick={() => (confirmRestoreId = v.id)}>{m('workflows.mgmt.ver.restore')}</button>
						{/if}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={onclose}>{m('workflows.mgmt.close')}</button>
	</div>
</Modal>

<style>
	.vh-body {
		max-height: 62vh;
		overflow-y: auto;
	}

	.vh-status {
		text-align: center;
		color: var(--text-muted);
		padding: 24px 0;
	}

	.vh-error {
		color: var(--danger);
	}

	.vh-diff-controls {
		display: flex;
		gap: 12px;
		align-items: flex-end;
		flex-wrap: wrap;
		margin-bottom: 14px;
	}

	.vh-diff-controls label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.74rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.vh-diff-controls select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 6px 30px 6px 8px;
		border-radius: 6px;
		background-color: var(--surface);
		font-size: 0.82rem;
	}

	.vh-diff-btn {
		padding: 7px 16px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.vh-diff-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.vh-diff {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 12px 14px;
		margin-bottom: 18px;
		background: var(--bg);
	}

	.vh-diff h4 {
		margin: 0 0 10px;
		font-size: 0.88rem;
	}

	.vh-changes {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.vh-change {
		display: flex;
		gap: 8px;
		align-items: baseline;
		font-size: 0.84rem;
	}

	.vh-kind {
		font-size: 0.68rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 7px;
		border-radius: 4px;
		white-space: nowrap;
	}

	/* Not `<Badge>`: a diff-KIND tag inside a change list, 0.68rem with a 4px
	   corner so a column of them reads as a legend rather than a row of status
	   pills. `.kind-added` was already on the palette pairs; its two siblings
	   now match. */
	.kind-added .vh-kind {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.kind-removed .vh-kind {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	.kind-changed .vh-kind {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.vh-list-title {
		margin: 0 0 8px;
		font-size: 0.74rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}

	.vh-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.vh-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 10px;
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.vh-row-main {
		display: flex;
		gap: 10px;
		align-items: baseline;
		flex-wrap: wrap;
	}

	.vh-vnum {
		font-weight: 600;
		font-size: 0.86rem;
	}

	.vh-meta {
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.vh-note {
		font-size: 0.8rem;
		color: var(--text);
	}

	.vh-row-actions {
		display: flex;
		gap: 6px;
	}

	.vh-restore {
		padding: 5px 12px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}

	.vh-restore:hover:not(.armed) {
		border-color: var(--accent);
		color: var(--accent);
	}

	/* Not a badge at all — the restore button's armed/confirm state. It keeps
	   its button metrics; only the three amber literals are retired to the
	   palette pair (the border tracks the text so the outline and the label
	   stay one colour). */
	.vh-restore.armed {
		border-color: var(--warning-on-tint);
		background: var(--warning-tint);
		color: var(--warning-on-tint);
	}

	.vh-restore:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.vh-cancel {
		padding: 5px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
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
