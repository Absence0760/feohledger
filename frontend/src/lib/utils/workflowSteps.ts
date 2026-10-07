import type { ConditionStepConfig, WorkflowStep } from '#lib/types/workflow.ts';

/**
 * Renumber a workflow's steps 1..n in their new order, keeping every
 * condition's branch targets pointed at the SAME steps.
 *
 * A condition step names where each branch goes by step number
 * (`on_true_goto` / `on_false_goto`). Moving, adding or removing a step
 * renumbers the steps after it, so a plain renumber silently re-aims each
 * branch at whichever step now holds the old number. This maps every target
 * old number → new number. A target that no longer exists (its step was
 * removed) becomes `null`, "continue to the next step", which is the
 * builder's default, rather than a number that now names a different step.
 *
 * `steps` must still carry their OLD numbers, in the new order. Old numbers
 * are unique: the builder numbers 1..n and gives a new step n + 1.
 */
export function renumberSteps(steps: WorkflowStep[]): WorkflowStep[] {
	const newNumber = new Map(steps.map((s, i) => [s.number, i + 1]));
	const remap = (target: number | null): number | null =>
		target === null ? null : (newNumber.get(target) ?? null);

	return steps.map((s, i) => {
		if (s.type !== 'condition') return { ...s, number: i + 1 };
		const cfg = s.config as ConditionStepConfig;
		return {
			...s,
			number: i + 1,
			config: {
				...cfg,
				on_true_goto: remap(cfg.on_true_goto),
				on_false_goto: remap(cfg.on_false_goto),
			},
		};
	});
}
