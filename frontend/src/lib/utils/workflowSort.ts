/**
 * Pure ordering rule behind the workflow canvas's drag-to-reorder
 * (`components/workflow-builder/WorkflowCanvas.svelte`).
 */

/**
 * The preview order while step `dragged` is held at pointer height `y`.
 *
 * `order` is the current preview (indices into the step list) and `mids` the
 * vertical midpoint of each entry, aligned with `order`. The held step goes
 * after every OTHER step whose midpoint the pointer has passed, which is the
 * rule that makes a neighbour swap the moment the pointer crosses its middle.
 *
 * Returns `order` itself (same reference) when nothing moves, so the caller
 * can skip a re-render.
 */
export function previewOrder(order: number[], dragged: number, y: number, mids: number[]): number[] {
	const others: number[] = [];
	let slot = 0;
	order.forEach((index, pos) => {
		if (index === dragged) return;
		others.push(index);
		if (y > mids[pos]) slot++;
	});
	const next = [...others.slice(0, slot), dragged, ...others.slice(slot)];
	return next.every((v, i) => v === order[i]) ? order : next;
}
