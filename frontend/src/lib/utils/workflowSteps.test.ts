import { describe, expect, it } from 'vitest';
import type { ConditionStepConfig, WorkflowStep } from '#lib/types/workflow.ts';
import { renumberSteps } from './workflowSteps.ts';

const step = (number: number, name: string): WorkflowStep => ({
	number,
	type: 'approval',
	name,
	enabled: true,
	config: {} as WorkflowStep['config'],
});
const condition = (
	number: number,
	onTrue: number | null,
	onFalse: number | null
): WorkflowStep => ({
	number,
	type: 'condition',
	name: 'Branch',
	enabled: true,
	config: { rules: [], match: 'all', on_true_goto: onTrue, on_false_goto: onFalse },
});
const gotos = (s: WorkflowStep) => {
	const c = s.config as ConditionStepConfig;
	return [c.on_true_goto, c.on_false_goto];
};

describe('renumberSteps', () => {
	it('numbers the steps 1..n in their new order', () => {
		const out = renumberSteps([step(3, 'C'), step(1, 'A'), step(2, 'B')]);
		expect(out.map((s) => [s.number, s.name])).toEqual([
			[1, 'C'],
			[2, 'A'],
			[3, 'B'],
		]);
	});

	it('keeps a condition pointed at the same steps after a reorder', () => {
		// 1 Branch (true→3 Fast, false→4 Slow), 2 Mid, 3 Fast, 4 Slow; move Slow to the top.
		const before = [condition(1, 3, 4), step(2, 'Mid'), step(3, 'Fast'), step(4, 'Slow')];
		const moved = [before[3], before[0], before[1], before[2]];
		const out = renumberSteps(moved);
		const byName = Object.fromEntries(out.map((s) => [s.name, s.number]));
		expect(gotos(out[1])).toEqual([byName.Fast, byName.Slow]);
		expect(gotos(out[1])).toEqual([4, 1]);
	});

	it('sends a branch whose target was removed to the next step, never a different step', () => {
		// Remove step 3 (Fast): its old number now belongs to Slow.
		const out = renumberSteps([condition(1, 3, 4), step(2, 'Mid'), step(4, 'Slow')]);
		expect(gotos(out[0])).toEqual([null, 3]);
	});

	it('leaves "next step" (null) targets alone and does not mutate its input', () => {
		const input = [condition(2, null, 1), step(1, 'A')];
		const out = renumberSteps(input);
		expect(gotos(out[0])).toEqual([null, 2]);
		expect(gotos(input[0])).toEqual([null, 1]);
	});
});
