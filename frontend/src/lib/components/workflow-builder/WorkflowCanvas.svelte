<script lang="ts">
	import { flushSync } from 'svelte';
	import { flip } from 'svelte/animate';
	import type { WorkflowStep, ConditionStepConfig, ParallelStepConfig } from '#lib/types/workflow.ts';
	import StepNode from './StepNode.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { previewOrder } from '#lib/utils/workflowSort.ts';

	type Props = {
		steps: WorkflowStep[];
		selectedIndex: number;
		onselect: (index: number) => void;
		onreorder: (from: number, to: number) => void;
		ontoggle: (index: number) => void;
		ondelete: (index: number) => void;
	};

	let { steps, selectedIndex, onselect, onreorder, ontoggle, ondelete }: Props = $props();

	/*
	 * Pointer-driven sorting. A press on a step arms a drag; once the pointer
	 * has moved DRAG_THRESHOLD px the step lifts and follows the pointer, and
	 * the other steps slide out of its way (animate:flip) as it crosses their
	 * midpoints. The step's own slot stays as a dashed placeholder, so where it
	 * will land is visible the whole time. Release commits the order the reader
	 * is already looking at; Escape (or a cancelled pointer) puts it back.
	 *
	 * Pointer events rather than HTML5 drag-and-drop: native DnD paints a
	 * browser ghost image and only reports the drop at the end, so nothing could
	 * move while the step was held — and it does not work on touch at all.
	 * Touch drags start from the ⋮⋮ handle only, so the page still scrolls.
	 *
	 * The ↑ / ↓ buttons on each step stay as the keyboard and single-pointer
	 * path (WCAG 2.1.1, 2.5.7).
	 */
	const DRAG_THRESHOLD = 5;
	const EDGE_SCROLL_ZONE = 48;
	const EDGE_SCROLL_STEP = 14;

	let canvasEl = $state<HTMLDivElement>();
	const itemEls: HTMLElement[] = [];

	// The press that may become a drag (armed until it passes the threshold).
	let press: { index: number; pointerId: number; x: number; y: number } | null = null;
	// While dragging: the preview order (indices into `steps`) and the lift.
	let order = $state<number[] | null>(null);
	let dragFrom = $state<number | null>(null);
	let liftDy = $state(0);
	let grabOffset = 0;
	// A drag ends in a click on the step it started on — that click must not
	// also select. Reset on the next press, so it can never eat a real click.
	let suppressClick = false;

	const reduceMotion =
		typeof window !== 'undefined' &&
		window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

	const displayed = $derived(
		(order ?? steps.map((_, i) => i)).map((i) => ({ step: steps[i], i }))
	);

	function stepNumberLabel(num: number | null): string {
		if (num === null) return m('workflows.builder.canvas.nextStep');
		const target = steps.find((s) => s.number === num);
		return target
			? m('workflows.builder.canvas.namedStep', { name: target.name, number: num })
			: m('workflows.builder.canvas.unnamedStep', { number: num });
	}

	/** Pointer Y in canvas coordinates — the space `offsetTop` is measured in. */
	function canvasY(clientY: number): number {
		return clientY - (canvasEl?.getBoundingClientRect().top ?? 0);
	}

	function onPointerDown(e: PointerEvent, index: number) {
		if (e.button !== 0) return;
		const target = e.target as Element;
		// The step's own controls (move, toggle, delete) keep their clicks.
		if (target.closest('button, input, select, textarea, a')) return;
		// On touch, only the handle starts a drag; elsewhere the finger scrolls.
		if (e.pointerType === 'touch' && !target.closest('.drag-handle')) return;
		suppressClick = false;
		press = { index, pointerId: e.pointerId, x: e.clientX, y: e.clientY };
		window.addEventListener('pointermove', onPointerMove);
		window.addEventListener('pointerup', onPointerUp);
		window.addEventListener('pointercancel', cancelDrag);
		window.addEventListener('keydown', onKeyDown);
	}

	function startDrag(e: PointerEvent) {
		if (!press) return;
		dragFrom = press.index;
		order = steps.map((_, i) => i);
		grabOffset = canvasY(press.y) - itemEls[press.index].offsetTop;
		document.documentElement.classList.add('wf-sorting');
		follow(e.clientY);
	}

	function follow(clientY: number) {
		if (dragFrom === null || !order) return;
		const y = canvasY(clientY);
		// Midpoints from LAYOUT (offsetTop ignores the flip transforms in
		// flight), so a step mid-slide can't make the order flicker.
		const mids = order.map((i) => itemEls[i].offsetTop + itemEls[i].offsetHeight / 2);
		const next = previewOrder(order, dragFrom, y, mids);
		if (next !== order) flushSync(() => (order = next));
		// Re-read the slot after the reorder so the lifted step stays glued to
		// the pointer instead of jumping with its slot.
		liftDy = y - grabOffset - itemEls[dragFrom].offsetTop;
	}

	function onPointerMove(e: PointerEvent) {
		if (!press || e.pointerId !== press.pointerId) return;
		if (dragFrom === null) {
			if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_THRESHOLD) return;
			startDrag(e);
		}
		e.preventDefault();
		if (e.clientY < EDGE_SCROLL_ZONE) window.scrollBy(0, -EDGE_SCROLL_STEP);
		else if (e.clientY > window.innerHeight - EDGE_SCROLL_ZONE) window.scrollBy(0, EDGE_SCROLL_STEP);
		follow(e.clientY);
	}

	function onPointerUp(e: PointerEvent) {
		if (!press || e.pointerId !== press.pointerId) return;
		const from = dragFrom;
		const to = from !== null && order ? order.indexOf(from) : null;
		const dragged = from !== null;
		endDrag();
		if (!dragged) return;
		suppressClick = true;
		if (from !== null && to !== null && to !== from) onreorder(from, to);
	}

	function onKeyDown(e: KeyboardEvent) {
		if (e.key === 'Escape' && dragFrom !== null) {
			e.preventDefault();
			cancelDrag();
		}
	}

	function cancelDrag() {
		const dragged = dragFrom !== null;
		endDrag();
		if (dragged) suppressClick = true;
	}

	function endDrag() {
		press = null;
		order = null;
		dragFrom = null;
		liftDy = 0;
		document.documentElement.classList.remove('wf-sorting');
		window.removeEventListener('pointermove', onPointerMove);
		window.removeEventListener('pointerup', onPointerUp);
		window.removeEventListener('pointercancel', cancelDrag);
		window.removeEventListener('keydown', onKeyDown);
	}

	$effect(() => endDrag);

	function select(index: number) {
		if (suppressClick) {
			suppressClick = false;
			return;
		}
		onselect(index);
	}
</script>

<div class="canvas" class:sorting={dragFrom !== null} bind:this={canvasEl}>
	{#if steps.length === 0}
		<div class="empty-canvas">{m('workflows.builder.canvas.empty')}</div>
	{:else}
		{#each displayed as { step, i }, pos (step.number)}
			{@const lifted = dragFrom === i}
			<div
				class="item"
				class:placeholder={lifted}
				bind:this={itemEls[i]}
				animate:flip={{ duration: lifted || reduceMotion ? 0 : 180 }}
			>
				<div
					class="item-body"
					class:lifted
					style:transform={lifted ? `translateY(${liftDy}px)` : null}
				>
					<StepNode
						{step}
						index={i}
						selected={selectedIndex === i}
						isFirst={i === 0}
						isLast={i === steps.length - 1}
						onpointerdown={(e) => onPointerDown(e, i)}
						onselect={() => select(i)}
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
				</div>

				{#if pos < displayed.length - 1}
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
		/* The offsetParent for the items, so offsetTop is canvas-relative. */
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		padding: 14px;
		min-height: 360px;
	}

	.empty-canvas {
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

	/* Each step and the connector below it. While its step is lifted, the item
	   keeps its height and shows where the step will land. */
	.item {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		border-radius: 10px;
	}

	.item.placeholder {
		outline: 2px dashed var(--accent);
		outline-offset: -2px;
		background: var(--accent-tint);
	}

	/* Hide the placeholder's connector line: it would show through the gap. */
	.item.placeholder .connector {
		visibility: hidden;
	}

	.item-body {
		position: relative;
	}

	.item-body.lifted {
		z-index: 2;
		/* The step "in hand": raised, slightly larger, no transition so it
		   tracks the pointer exactly. */
		scale: 1.02;
		box-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
		border-radius: 8px;
		cursor: grabbing;
	}

	.canvas.sorting :global(.node) {
		cursor: grabbing;
	}

	:global(html.wf-sorting) {
		cursor: grabbing;
		user-select: none;
	}

	.connector {
		display: block;
		margin: 2px auto;
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
