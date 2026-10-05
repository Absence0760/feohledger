<script lang="ts">
	import type {
		ConditionStepConfig,
		ConditionRule,
		ConditionField,
		ConditionOperator,
		WorkflowStep,
	} from '#lib/types/workflow.ts';
	import { CONDITION_FIELD_LABELS, CONDITION_OPERATOR_LABELS } from '#lib/types/workflow.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	type Props = {
		config: ConditionStepConfig;
		steps: WorkflowStep[];
		selfNumber: number;
		onchange: (config: ConditionStepConfig) => void;
	};

	let { config, steps, selfNumber, onchange }: Props = $props();

	const SET_OPERATORS: ConditionOperator[] = ['in', 'not_in'];
	const NUMERIC_FIELDS: ConditionField[] = ['amount'];

	// Steps that can be a branch target — everything except this step itself.
	let targets = $derived(steps.filter((s) => s.number !== selfNumber));

	function patch(p: Partial<ConditionStepConfig>) {
		onchange({ ...config, ...p });
	}

	function patchRule(idx: number, p: Partial<ConditionRule>) {
		patch({ rules: config.rules.map((r, i) => (i === idx ? { ...r, ...p } : r)) });
	}

	function addRule() {
		const rule: ConditionRule = { field: 'amount', operator: 'gt', value: 0 };
		patch({ rules: [...config.rules, rule] });
	}

	function removeRule(idx: number) {
		patch({ rules: config.rules.filter((_, i) => i !== idx) });
	}

	function valueForInput(rule: ConditionRule): string {
		if (Array.isArray(rule.value)) return rule.value.join(', ');
		return String(rule.value ?? '');
	}

	function parseValue(rule: ConditionRule, raw: string): number | string | string[] {
		if (SET_OPERATORS.includes(rule.operator)) {
			return raw
				.split(',')
				.map((s) => s.trim())
				.filter((s) => s.length > 0);
		}
		if (NUMERIC_FIELDS.includes(rule.field)) {
			const n = parseFloat(raw);
			return raw === '' || Number.isNaN(n) ? raw : n;
		}
		return raw;
	}

	function operatorChanged(idx: number, newOp: ConditionOperator) {
		const rule = config.rules[idx];
		const wasSet = SET_OPERATORS.includes(rule.operator);
		const willBeSet = SET_OPERATORS.includes(newOp);
		let value: number | string | string[] = rule.value;
		if (!wasSet && willBeSet) {
			value = rule.value !== '' && rule.value != null ? [String(rule.value)] : [];
		} else if (wasSet && !willBeSet) {
			value = Array.isArray(rule.value) ? (rule.value[0] ?? '') : '';
		}
		patchRule(idx, { operator: newOp, value });
	}

	function gotoValue(v: number | null): string {
		return v === null ? '' : String(v);
	}

	function parseGoto(raw: string): number | null {
		return raw === '' ? null : parseInt(raw, 10);
	}
</script>

<div class="condition">
	<div class="match-row">
		<span class="match-label">{m('workflows.builder.condition.match')}</span>
		<select
			value={config.match}
			onchange={(e) => patch({ match: e.currentTarget.value as 'all' | 'any' })}
		>
			<option value="all">{m('workflows.builder.condition.matchAll')}</option>
			<option value="any">{m('workflows.builder.condition.matchAny')}</option>
		</select>
	</div>

	<div class="rules">
		{#each config.rules as rule, idx (idx)}
			<div class="rule-row">
				<select
					aria-label={m('workflows.builder.condition.field')}
					value={rule.field}
					onchange={(e) => patchRule(idx, { field: e.currentTarget.value as ConditionField })}
				>
					{#each Object.entries(CONDITION_FIELD_LABELS) as [val, label]}
						<option value={val}>{label}</option>
					{/each}
				</select>
				<select
					aria-label={m('workflows.builder.condition.operator')}
					value={rule.operator}
					onchange={(e) => operatorChanged(idx, e.currentTarget.value as ConditionOperator)}
				>
					{#each Object.entries(CONDITION_OPERATOR_LABELS) as [val, label]}
						<option value={val}>{label}</option>
					{/each}
				</select>
				<input
					type="text"
					aria-label={m('workflows.builder.condition.value')}
					placeholder={SET_OPERATORS.includes(rule.operator)
						? m('workflows.builder.condition.valuePlaceholderSet')
						: m('workflows.builder.condition.valuePlaceholder')}
					value={valueForInput(rule)}
					oninput={(e) => patchRule(idx, { value: parseValue(rule, e.currentTarget.value) })}
				/>
				<button
					type="button"
					class="icon-btn danger"
					title={m('workflows.builder.condition.removeRule')}
					aria-label={m('workflows.builder.condition.removeRule')}
					onclick={() => removeRule(idx)}
				>×</button>
			</div>
		{/each}
		{#if config.rules.length === 0}
			<p class="hint">{m('workflows.builder.condition.noRules')}</p>
		{/if}
		<button type="button" class="link-btn" onclick={addRule}>{m('workflows.builder.condition.addRule')}</button>
	</div>

	<div class="branch-row">
		<label class="branch-field">
			<span class="branch-label true">{m('workflows.builder.condition.whenTrue')}</span>
			<select
				value={gotoValue(config.on_true_goto)}
				onchange={(e) => patch({ on_true_goto: parseGoto(e.currentTarget.value) })}
			>
				<option value="">{m('workflows.builder.condition.fallThrough')}</option>
				{#each targets as t (t.number)}
					<option value={t.number}>{t.number}. {t.name}</option>
				{/each}
			</select>
		</label>
		<label class="branch-field">
			<span class="branch-label false">{m('workflows.builder.condition.whenFalse')}</span>
			<select
				value={gotoValue(config.on_false_goto)}
				onchange={(e) => patch({ on_false_goto: parseGoto(e.currentTarget.value) })}
			>
				<option value="">{m('workflows.builder.condition.fallThrough')}</option>
				{#each targets as t (t.number)}
					<option value={t.number}>{t.number}. {t.name}</option>
				{/each}
			</select>
		</label>
	</div>
</div>

<style>
	.condition {
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	.match-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.match-label {
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.rules {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.rule-row {
		display: grid;
		grid-template-columns: 1.1fr 1.2fr 1.4fr 32px;
		gap: 8px;
		align-items: center;
	}

	.rule-row select,
	.rule-row input,
	.match-row select,
	.branch-field select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 7px 30px 7px 9px;
		border-radius: 5px;
		font-size: 0.85rem;
		box-sizing: border-box;
		width: 100%;
	}

	.rule-row select:focus,
	.rule-row input:focus,
	.match-row select:focus,
	.branch-field select:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.icon-btn {
		width: 28px;
		height: 28px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text-muted);
		cursor: pointer;
		font-size: 14px;
		font-family: inherit;
	}

	.icon-btn.danger:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	.link-btn {
		background: none;
		border: none;
		color: var(--accent);
		cursor: pointer;
		font-size: 0.8rem;
		padding: 0;
		font-family: inherit;
		align-self: flex-start;
	}

	.link-btn:hover {
		filter: brightness(1.2);
	}

	.branch-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.branch-field {
		display: flex;
		flex-direction: column;
		gap: 5px;
	}

	.branch-label {
		font-size: 0.74rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.branch-label.true {
		color: #1fa86a;
	}

	.branch-label.false {
		color: var(--danger);
	}

	.hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 0;
	}
</style>
