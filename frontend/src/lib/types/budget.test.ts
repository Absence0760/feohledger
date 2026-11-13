import { describe, expect, it } from 'vitest';
import { BUDGET_DIMENSIONS, BUDGET_DIMENSION_LABEL_KEYS, budgetDimensionLabelKey } from './budget';
import { EN_ALL } from '#lib/i18n/allMessages.ts';

/**
 * The budget dimensions were an English-only label map; they are catalogue
 * keys now, so the help guide can name them with `{ui:budgets.dimension.*}`
 * (decisions §360). The English must stay what the chips and select showed.
 */
describe('budget dimension labels', () => {
	it('keys every dimension', () => {
		expect(Object.keys(BUDGET_DIMENSION_LABEL_KEYS).sort()).toEqual([...BUDGET_DIMENSIONS].sort());
	});

	it('keeps the English the list always showed', () => {
		expect(BUDGET_DIMENSIONS.map((d) => EN_ALL[BUDGET_DIMENSION_LABEL_KEYS[d]])).toEqual([
			'Department',
			'Project',
			'Cost Center',
			'GL Account'
		]);
	});

	it('has no key for a dimension this build predates', () => {
		expect(budgetDimensionLabelKey('cost_center')).toBe('budgets.dimension.costCenter');
		expect(budgetDimensionLabelKey('region')).toBeNull();
	});
});
