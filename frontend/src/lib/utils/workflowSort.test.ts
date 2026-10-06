import { describe, expect, it } from 'vitest';
import { previewOrder } from './workflowSort.ts';

// Four 60px steps stacked from y=0: midpoints at 30, 90, 150, 210.
const MIDS = [30, 90, 150, 210];

describe('previewOrder', () => {
	it('keeps the order while the held step is still between its neighbours', () => {
		const order = [0, 1, 2, 3];
		expect(previewOrder(order, 1, 80, MIDS)).toBe(order);
	});

	it('swaps a neighbour the moment the pointer passes its midpoint', () => {
		// Holding step 1 and dragging down past step 2's middle (150).
		expect(previewOrder([0, 1, 2, 3], 1, 151, MIDS)).toEqual([0, 2, 1, 3]);
		// …and upward past step 0's middle (30).
		expect(previewOrder([0, 1, 2, 3], 1, 29, MIDS)).toEqual([1, 0, 2, 3]);
	});

	it('moves to either end when the pointer goes past every step', () => {
		expect(previewOrder([0, 1, 2, 3], 0, 500, MIDS)).toEqual([1, 2, 3, 0]);
		expect(previewOrder([0, 1, 2, 3], 3, -40, MIDS)).toEqual([3, 0, 1, 2]);
	});

	it('works from an already-moved preview', () => {
		// Preview [0, 2, 1, 3] with step 1 held at position 2; drag back up past 2.
		expect(previewOrder([0, 2, 1, 3], 1, 80, MIDS)).toEqual([0, 1, 2, 3]);
	});
});
