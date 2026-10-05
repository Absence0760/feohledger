<script lang="ts">
	// Multi-level approval-chain progress stepper — which levels are done,
	// which is active, who has approved, and who's still pending. Reads the
	// runtime state `GET /api/invoices/{id}/workflow` returns on
	// `state_data.approval_levels` (backend/app/services/approval_chain.py).
	//
	// i18n-agnostic like `FieldWarning`/`SecretReveal`: every string arrives
	// already localized from the caller, which keeps its own i18n key
	// namespace (see frontend/CLAUDE.md § `ui/` primitives).
	import type { ChainLevelState } from '#lib/types/workflowInstance.ts';
	import { distinctApprovedCount } from '#lib/types/workflowInstance.ts';

	let {
		levels,
		currentLevel,
		resolveApproverName,
		formatProgress,
		statusLabel,
		anyApproverLabel,
		title,
	}: {
		levels: ChainLevelState[];
		/** Index of the level still collecting approvals. `>= levels.length`
		 *  means every level is satisfied. */
		currentLevel: number;
		resolveApproverName: (userId: string) => string;
		formatProgress: (approved: number, required: number) => string;
		statusLabel: (status: 'done' | 'current' | 'pending') => string;
		anyApproverLabel: string;
		title: string;
	} = $props();

	function levelStatus(index: number): 'done' | 'current' | 'pending' {
		if (index < currentLevel) return 'done';
		if (index === currentLevel) return 'current';
		return 'pending';
	}
</script>

<div class="chain-progress" data-testid="approval-chain-progress">
	<div class="chain-progress-title">{title}</div>
	<ol class="chain-levels">
		{#each levels as level, i (level.level ?? i)}
			{@const st = levelStatus(i)}
			{@const approvedIds = new Set(level.approvals.map((a) => a.user_id))}
			<li class="chain-level chain-level-{st}">
				<span class="chain-marker" aria-hidden="true">
					{#if st === 'done'}
						<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12" /></svg>
					{/if}
				</span>
				<div class="chain-body">
					<div class="chain-head">
						<span class="chain-name">{level.name}</span>
						<span class="chain-status chain-status-{st}">{statusLabel(st)}</span>
					</div>
					<div class="chain-count">{formatProgress(distinctApprovedCount(level), level.required)}</div>
					{#if level.approver_ids.length > 0}
						<ul class="chain-approvers">
							{#each level.approver_ids as approverId (approverId)}
								<li class="chain-approver" class:approved={approvedIds.has(approverId)}>
									<span class="chain-approver-mark" aria-hidden="true">{approvedIds.has(approverId) ? '✓' : '·'}</span>
									{resolveApproverName(approverId)}
								</li>
							{/each}
						</ul>
					{:else}
						<div class="chain-any">{anyApproverLabel}</div>
					{/if}
				</div>
			</li>
		{/each}
	</ol>
</div>

<style>
	.chain-progress {
		margin-top: 12px;
	}

	.chain-progress-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}

	.chain-levels {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
	}

	.chain-level {
		display: flex;
		gap: 10px;
		padding: 0 0 12px 0;
		border-left: 2px solid var(--border);
		margin-left: 5px;
		padding-left: 14px;
		position: relative;
	}

	.chain-level:last-child {
		padding-bottom: 0;
	}

	.chain-level-done {
		border-left-color: var(--success);
	}

	.chain-marker {
		position: absolute;
		left: -8px;
		top: 2px;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--bg);
		border: 2px solid var(--border);
	}

	/* `-strong`, not the base `--success` token: this fills the marker behind
	   WHITE checkmark content, and only the `-strong` companion is calibrated
	   for that (see frontend/CLAUDE.md § Colour tokens and contrast) — the
	   base token alone is a mid-tone chosen to be legible as TEXT on the dark
	   surface, not as a background under white. */
	.chain-level-done .chain-marker {
		background: var(--success-strong);
		border-color: var(--success-strong);
		color: #fff;
	}

	.chain-level-current .chain-marker {
		border-color: var(--accent);
		background: var(--accent);
	}

	.chain-head {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}

	.chain-name {
		font-weight: 500;
		color: var(--text);
		font-size: 0.85rem;
	}

	.chain-status {
		font-size: 0.68rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}

	.chain-status-done {
		color: var(--success);
	}

	.chain-status-current {
		color: var(--accent);
	}

	.chain-count {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin-top: 2px;
	}

	.chain-approvers {
		list-style: none;
		margin: 6px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.chain-approver {
		font-size: 0.78rem;
		color: var(--text-muted);
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.chain-approver.approved {
		color: var(--text);
	}

	.chain-approver-mark {
		width: 10px;
		display: inline-block;
		color: var(--success);
		font-weight: 700;
	}

	.chain-any {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin-top: 4px;
		font-style: italic;
	}
</style>
