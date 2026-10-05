<script lang="ts">
	import type { WorkflowStep, WorkflowStepType, ConditionStepConfig, ParallelStepConfig } from '#lib/types/workflow.ts';
	import StepNode from './StepNode.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';

	type Props = {
		steps: WorkflowStep[];
		selectedIndex: number;
		onselect: (index: number) => void;
		onreorder: (from: number, to: number) => void;
		onaddat: (type: WorkflowStepType, index: number) => void;
		ontoggle: (index: number) => void;
		ondelete: (index: number) => void;
		paletteType: WorkflowStepType | null;
	};

	let {
		steps,
		selectedIndex,
		onselect,
		onreorder,
		onaddat,
		ontoggle,
		ondelete,
		paletteType,
	}: Props = $props();

	// Drag state: either reordering an existing node (dragIndex set) or dropping
	// a new palette step (paletteType set by the parent). dropIndex is the slot
	// the indicator currently highlights.
	let dragIndex = $state<number | null>(null);
	let dropIndex = $state<number | null>(null);

	function stepNumberLabel(num: number | null): string {
		if (num === null) return m('workflows.builder.canvas.nextStep');
		const target = steps.find((s) => s.number === num);
		return target
			? m('workflows.builder.canvas.namedStep', { name: target.name, number: num })
			: m('workflows.builder.canvas.unnamedStep', { number: num });
	}

	function handleNodeDragStart(e: DragEvent, index: number) {
		dragIndex = index;
		if (e.dataTransfer) {
			e.dataTransfer.effectAllowed = 'move';
			e.dataTransfer.setData('text/plain', `node:${index}`);
		}
	}

	function handleNodeDragEnd() {
		dragIndex = null;
		dropIndex = null;
	}

	function handleSlotDragOver(e: DragEvent, slot: number) {
		// Allow drop only when something is being dragged (reorder or palette add).
		if (dragIndex === null && paletteType === null) return;
		e.preventDefault();
		if (e.dataTransfer) {
			e.dataTransfer.dropEffect = dragIndex !== null ? 'move' : 'copy';
		}
		dropIndex = slot;
	}

	function handleSlotDrop(e: DragEvent, slot: number) {
		e.preventDefault();
		if (dragIndex !== null) {
			// Reorder: account for removal shifting indices when moving downward.
			let to = slot;
			if (slot > dragIndex) to = slot - 1;
			if (to !== dragIndex) onreorder(dragIndex, to);
		} else if (paletteType !== null) {
			onaddat(paletteType, slot);
		}
		dragIndex = null;
		dropIndex = null;
	}

	function handleCanvasDragLeave(e: DragEvent) {
		// Clear the indicator only when leaving the canvas entirely.
		const related = e.relatedTarget as Node | null;
		if (!related || !(e.currentTarget as HTMLElement).contains(related)) {
			dropIndex = null;
		}
	}

	const isDragging = $derived(dragIndex !== null || paletteType !== null);
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="canvas" ondragleave={handleCanvasDragLeave}>
	{#if steps.length === 0}
		<div
			class="empty-drop"
			class:over={dropIndex === 0}
			ondragover={(e) => handleSlotDragOver(e, 0)}
			ondrop={(e) => handleSlotDrop(e, 0)}
			role="list"
		>
			{m('workflows.builder.canvas.emptyDrop')}
		</div>
	{:else}
		<!-- Drop slot before the first node -->
		<div
			class="drop-slot"
			class:active={isDragging}
			class:over={dropIndex === 0}
			ondragover={(e) => handleSlotDragOver(e, 0)}
			ondrop={(e) => handleSlotDrop(e, 0)}
		></div>

		{#each steps as step, i (step.number)}
			<StepNode
				{step}
				index={i}
				selected={selectedIndex === i}
				isFirst={i === 0}
				isLast={i === steps.length - 1}
				ondragstart={(e) => handleNodeDragStart(e, i)}
				ondragend={handleNodeDragEnd}
				onselect={() => onselect(i)}
				ontoggle={() => ontoggle(i)}
				ondelete={() => ondelete(i)}
				onmoveup={() => onreorder(i, i - 1)}
				onmovedown={() => onreorder(i, i + 1)}
			/>

			<!-- Branch annotations for condition / parallel -->
			{#if step.type === 'condition'}
				{@const cfg = step.config as ConditionStepConfig}
				<div class="branch-annot">
					<span class="branch-line true">
						<span class="branch-tag true">{m('workflows.builder.canvas.branchTrue')}</span>
						{stepNumberLabel(cfg.on_true_goto)}
					</span>
					<span class="branch-line false">
						<span class="branch-tag false">{m('workflows.builder.canvas.branchFalse')}</span>
						{stepNumberLabel(cfg.on_false_goto)}
					</span>
				</div>
			{:else if step.type === 'parallel'}
				{@const cfg = step.config as ParallelStepConfig}
				<div class="branch-annot parallel">
					<span class="parallel-summary">
						{m('workflows.builder.canvas.fanOut', { join: cfg.join })}{cfg.join === 'any' &&
						cfg.min_approvals
							? m('workflows.builder.canvas.minRequired', { count: cfg.min_approvals })
							: ''}
					</span>
					<div class="parallel-branches">
						{#each cfg.branches as branch (branch.name)}
							<span class="parallel-branch">{branch.name} ({branch.approver_ids.length})</span>
						{/each}
					</div>
				</div>
			{/if}

			<!-- Connector + drop slot after each node -->
			<div
				class="drop-slot connector-slot"
				class:active={isDragging}
				class:over={dropIndex === i + 1}
				ondragover={(e) => handleSlotDragOver(e, i + 1)}
				ondrop={(e) => handleSlotDrop(e, i + 1)}
			>
				{#if !isDragging && i < steps.length - 1}
					<svg class="connector" width="2" height="18" viewBox="0 0 2 18" aria-hidden="true">
						<line x1="1" y1="0" x2="1" y2="18" stroke="var(--border)" stroke-width="2" stroke-dasharray="4 3" />
					</svg>
				{/if}
			</div>
		{/each}
	{/if}
</div>

<style>
	.canvas {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		padding: 14px;
		min-height: 360px;
	}

	.empty-drop {
		flex: 1;
		min-height: 220px;
		display: grid;
		place-items: center;
		text-align: center;
		padding: 24px;
		border: 2px dashed var(--border);
		border-radius: 10px;
		color: var(--text-muted);
		font-size: 0.88rem;
	}

	.empty-drop.over {
		border-color: var(--accent);
		background: rgba(99, 140, 255, 0.06);
	}

	.drop-slot {
		display: flex;
		align-items: center;
		justify-content: center;
		min-height: 8px;
	}

	.connector-slot {
		min-height: 22px;
	}

	/* When a drag is active the slots grow into visible drop targets. */
	.drop-slot.active {
		min-height: 26px;
		margin: 2px 0;
		border-radius: 6px;
		border: 1px dashed transparent;
		transition: all 0.12s;
	}

	.drop-slot.active.over {
		min-height: 34px;
		border-color: var(--accent);
		background: rgba(99, 140, 255, 0.1);
	}

	.connector {
		display: block;
	}

	.branch-annot {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin: 4px 0 0 28px;
		padding: 6px 10px;
		border-left: 2px solid var(--border);
		font-size: 0.76rem;
		color: var(--text-muted);
	}

	.branch-line {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}

	.branch-tag {
		font-weight: 600;
		font-size: 0.7rem;
		padding: 1px 6px;
		border-radius: 4px;
	}

	/* Not `<Badge>`: TRUE/FALSE edge labels on a condition node — 0.7rem square
	   tags sized to sit on a canvas connector, where a status pill's metrics
	   would swamp the node. Colour comes from the palette pairs. */
	.branch-tag.true {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.branch-tag.false {
		background: var(--danger-tint);
		color: var(--danger-on-tint);
	}

	.parallel-summary {
		font-weight: 600;
		font-size: 0.72rem;
		color: var(--accent);
	}

	.parallel-branches {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
		margin-top: 3px;
	}

	/* Not `<Badge>`: a branch NAME + approver count rendered inside a canvas
	   node, several to a row at 0.72rem. Colour comes from the palette pair. */
	.parallel-branch {
		padding: 1px 7px;
		border-radius: 10px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.72rem;
	}
</style>
