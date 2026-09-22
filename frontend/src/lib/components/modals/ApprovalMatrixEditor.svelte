<script lang="ts">
	import type {
		ApprovalLevelConfig,
		RoutingField,
		RoutingOperator,
		RoutingRule,
	} from '$lib/types/workflow';
	import {
		ROUTING_FIELD_LABELS,
		ROUTING_OPERATOR_LABELS,
	} from '$lib/types/workflow';
	import type { AdminUser } from '$lib/types/admin';
	import { m } from '$lib/i18n/store.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';

	type Props = {
		chain: ApprovalLevelConfig[];
		users: AdminUser[];
		onchange: (chain: ApprovalLevelConfig[]) => void;
	};

	let { chain, users, onchange }: Props = $props();

	// The per-level amount bands are denominated in the org's REPORTING
	// currency (the backend converts each invoice before applying them —
	// `approval_chain.resolve_applicable_levels`), so the column labels name
	// the code. Resolved here rather than taken as a prop, so the editor is
	// correct wherever it is mounted; `ensureLoaded` is idempotent and shares
	// one in-flight request with every other caller.
	$effect(() => {
		orgCurrency.ensureLoaded();
	});

	const SET_OPERATORS: RoutingOperator[] = ['in', 'not_in'];

	function newLevel(): ApprovalLevelConfig {
		return {
			name: m('approvalMatrix.defaultLevelName', { n: chain.length + 1 }),
			min_amount: null,
			max_amount: null,
			approver_ids: [],
			required_approvals: 1,
			parallel_mode: 'any',
			routing_rules: [],
			escalation_hours: null,
			escalation_to_user_ids: [],
		};
	}

	function addLevel() {
		onchange([...chain, newLevel()]);
	}

	function removeLevel(index: number) {
		onchange(chain.filter((_, i) => i !== index));
	}

	function moveLevel(index: number, direction: -1 | 1) {
		const target = index + direction;
		if (target < 0 || target >= chain.length) return;
		const arr = [...chain];
		[arr[index], arr[target]] = [arr[target], arr[index]];
		onchange(arr);
	}

	function patchLevel(index: number, patch: Partial<ApprovalLevelConfig>) {
		onchange(chain.map((lvl, i) => (i === index ? { ...lvl, ...patch } : lvl)));
	}

	function patchRule(levelIdx: number, ruleIdx: number, patch: Partial<RoutingRule>) {
		const rules = chain[levelIdx].routing_rules.map((r, i) =>
			i === ruleIdx ? { ...r, ...patch } : r
		);
		patchLevel(levelIdx, { routing_rules: rules });
	}

	function addRule(levelIdx: number) {
		const rule: RoutingRule = { field: 'gl_account', operator: 'eq', value: '' };
		patchLevel(levelIdx, {
			routing_rules: [...chain[levelIdx].routing_rules, rule],
		});
	}

	function removeRule(levelIdx: number, ruleIdx: number) {
		patchLevel(levelIdx, {
			routing_rules: chain[levelIdx].routing_rules.filter((_, i) => i !== ruleIdx),
		});
	}

	// `in`/`not_in` carry a list-of-strings value; the others carry a scalar.
	// We surface a single text input either way and split on commas for sets,
	// so the UI is uniform without losing the semantic difference.
	function ruleValueForInput(rule: RoutingRule): string {
		if (Array.isArray(rule.value)) return rule.value.join(', ');
		return rule.value ?? '';
	}

	function parseRuleValue(rule: RoutingRule, raw: string): string | string[] {
		if (SET_OPERATORS.includes(rule.operator)) {
			return raw
				.split(',')
				.map((s) => s.trim())
				.filter((s) => s.length > 0);
		}
		return raw;
	}

	function operatorChanged(levelIdx: number, ruleIdx: number, newOp: RoutingOperator) {
		// Coerce value shape on operator change (eg switching eq→in turns "x" into ["x"]).
		const rule = chain[levelIdx].routing_rules[ruleIdx];
		const wasSet = SET_OPERATORS.includes(rule.operator);
		const willBeSet = SET_OPERATORS.includes(newOp);
		let value: string | string[] = rule.value;
		if (!wasSet && willBeSet) {
			value = rule.value ? [String(rule.value)] : [];
		} else if (wasSet && !willBeSet) {
			value = Array.isArray(rule.value) ? rule.value[0] ?? '' : '';
		}
		patchRule(levelIdx, ruleIdx, { operator: newOp, value });
	}

	function userById(id: string): AdminUser | undefined {
		return users.find((u) => u.id === id);
	}

	function toggleApprover(levelIdx: number, userId: string, list: 'approver_ids' | 'escalation_to_user_ids') {
		const current = chain[levelIdx][list];
		const next = current.includes(userId)
			? current.filter((id) => id !== userId)
			: [...current, userId];
		patchLevel(levelIdx, { [list]: next });
	}
</script>

<div class="matrix">
	{#each chain as level, levelIdx}
		<div class="level-card" data-level-index={levelIdx}>
			<div class="level-header">
				<input
					class="level-name"
					type="text"
					placeholder={m('approvalMatrix.levelNamePlaceholder')}
					aria-label={m('approvalMatrix.levelNameAria', { n: levelIdx + 1 })}
					value={level.name}
					oninput={(e) => patchLevel(levelIdx, { name: e.currentTarget.value })}
				/>
				<div class="level-actions">
					<button
						type="button"
						class="icon-btn"
						title={m('approvalMatrix.moveUp')}
						aria-label={m('approvalMatrix.moveUp')}
						disabled={levelIdx === 0}
						onclick={() => moveLevel(levelIdx, -1)}
					>↑</button>
					<button
						type="button"
						class="icon-btn"
						title={m('approvalMatrix.moveDown')}
						aria-label={m('approvalMatrix.moveDown')}
						disabled={levelIdx === chain.length - 1}
						onclick={() => moveLevel(levelIdx, 1)}
					>↓</button>
					<button
						type="button"
						class="icon-btn danger"
						title={m('approvalMatrix.removeLevel')}
						aria-label={m('approvalMatrix.removeLevel')}
						onclick={() => removeLevel(levelIdx)}
					>×</button>
				</div>
			</div>

			<div class="row">
				<label class="field">
					<span>{m('approvalMatrix.minAmount', { currency: orgCurrency.label })}</span>
					<input
						type="number"
						step="0.01"
						min="0"
						placeholder={m('approvalMatrix.noMin')}
						value={level.min_amount ?? ''}
						oninput={(e) =>
							patchLevel(levelIdx, {
								min_amount: e.currentTarget.value.trim() || null,
							})}
					/>
				</label>
				<label class="field">
					<span>{m('approvalMatrix.maxAmount', { currency: orgCurrency.label })}</span>
					<input
						type="number"
						step="0.01"
						min="0"
						placeholder={m('approvalMatrix.noMax')}
						value={level.max_amount ?? ''}
						oninput={(e) =>
							patchLevel(levelIdx, {
								max_amount: e.currentTarget.value.trim() || null,
							})}
					/>
				</label>
			</div>

			<div class="field">
				<span class="field-label">{m('approvalMatrix.approvers')}</span>
				<div class="user-chips">
					{#each users.filter((u) => u.is_active) as u}
						<button
							type="button"
							class="chip"
							class:on={level.approver_ids.includes(u.id)}
							onclick={() => toggleApprover(levelIdx, u.id, 'approver_ids')}
						>
							{u.full_name}
						</button>
					{/each}
				</div>
			</div>

			<div class="row">
				<label class="field">
					<span>{m('approvalMatrix.parallelMode')}</span>
					<select
						value={level.parallel_mode}
						onchange={(e) =>
							patchLevel(levelIdx, {
								parallel_mode: e.currentTarget.value as 'any' | 'all',
							})}
					>
						<option value="any">{m('approvalMatrix.parallelAny')}</option>
						<option value="all">{m('approvalMatrix.parallelAll')}</option>
					</select>
				</label>
				{#if level.parallel_mode === 'any'}
					<label class="field">
						<span>{m('approvalMatrix.requiredApprovals')}</span>
						<input
							type="number"
							min="1"
							value={level.required_approvals}
							oninput={(e) =>
								patchLevel(levelIdx, {
									required_approvals: Math.max(1, parseInt(e.currentTarget.value) || 1),
								})}
						/>
					</label>
				{/if}
			</div>

			<div class="field">
				<div class="field-label-row">
					<span class="field-label">{m('approvalMatrix.routingRules')}</span>
					<button type="button" class="link-btn" onclick={() => addRule(levelIdx)}>
						{m('approvalMatrix.addRule')}
					</button>
				</div>
				{#if level.routing_rules.length === 0}
					<p class="hint">{m('approvalMatrix.noRules')}</p>
				{/if}
				{#each level.routing_rules as rule, ruleIdx}
					<div class="rule-row">
						<select
							value={rule.field}
							aria-label={m('approvalMatrix.ruleFieldAria')}
							onchange={(e) =>
								patchRule(levelIdx, ruleIdx, { field: e.currentTarget.value as RoutingField })}
						>
							{#each Object.entries(ROUTING_FIELD_LABELS) as [val, label]}
								<option value={val}>{label}</option>
							{/each}
						</select>
						<select
							value={rule.operator}
							aria-label={m('approvalMatrix.ruleOperatorAria')}
							onchange={(e) =>
								operatorChanged(levelIdx, ruleIdx, e.currentTarget.value as RoutingOperator)}
						>
							{#each Object.entries(ROUTING_OPERATOR_LABELS) as [val, label]}
								<option value={val}>{label}</option>
							{/each}
						</select>
						<input
							type="text"
							placeholder={SET_OPERATORS.includes(rule.operator) ? m('approvalMatrix.ruleValueSetPlaceholder') : m('approvalMatrix.ruleValuePlaceholder')}
							aria-label={m('approvalMatrix.ruleValueAria')}
							value={ruleValueForInput(rule)}
							oninput={(e) =>
								patchRule(levelIdx, ruleIdx, {
									value: parseRuleValue(rule, e.currentTarget.value),
								})}
						/>
						<button
							type="button"
							class="icon-btn danger"
							title={m('approvalMatrix.removeRule')}
							aria-label={m('approvalMatrix.removeRule')}
							onclick={() => removeRule(levelIdx, ruleIdx)}
						>×</button>
					</div>
				{/each}
			</div>

			<div class="row">
				<label class="field">
					<span>{m('approvalMatrix.escalateAfter')}</span>
					<input
						type="number"
						min="1"
						placeholder={m('approvalMatrix.disabled')}
						value={level.escalation_hours ?? ''}
						oninput={(e) =>
							patchLevel(levelIdx, {
								escalation_hours: e.currentTarget.value
									? parseInt(e.currentTarget.value)
									: null,
							})}
					/>
				</label>
			</div>

			{#if level.escalation_hours}
				<div class="field">
					<span class="field-label">{m('approvalMatrix.escalateTo')}</span>
					<div class="user-chips">
						{#each users.filter((u) => u.is_active) as u}
							<button
								type="button"
								class="chip"
								class:on={level.escalation_to_user_ids.includes(u.id)}
								onclick={() => toggleApprover(levelIdx, u.id, 'escalation_to_user_ids')}
							>
								{u.full_name}
							</button>
						{/each}
					</div>
					<p class="hint">
						{m('approvalMatrix.escalateHint', { hours: level.escalation_hours })}
					</p>
				</div>
			{/if}
		</div>
	{/each}

	<button type="button" class="add-level-btn" onclick={addLevel}>
		{m('approvalMatrix.addLevel')}
	</button>
</div>

<style>
	.matrix {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.level-card {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 16px;
		background: var(--bg);
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.level-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}

	.level-name {
		font-size: 0.95rem;
		font-weight: 600;
		border: 1px solid transparent;
		background: transparent;
		padding: 4px 8px;
		flex: 1;
		min-width: 0;
		color: var(--text);
		border-radius: 4px;
		font-family: inherit;
	}

	.level-name:hover {
		border-color: var(--border);
	}

	.level-name:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.level-actions {
		display: flex;
		gap: 4px;
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

	.icon-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.icon-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.icon-btn.danger:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}

	.row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.field-label,
	.field > span {
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}

	.field-label-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.field input,
	.field select {
		/* base look (border/radius/colour/font/chevron) from the global recipe */
		padding: 8px 30px 8px 10px;
		font-size: 0.85rem;
		background-color: var(--surface);
	}

	.field input:focus,
	.field select:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.user-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.chip {
		padding: 4px 10px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.78rem;
		cursor: pointer;
		font-family: inherit;
	}

	.chip:hover:not(.on) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.chip.on {
		background: var(--accent-strong);
		color: white;
		border-color: var(--accent-strong);
	}

	.rule-row {
		display: grid;
		grid-template-columns: 1fr 1fr 1.4fr 32px;
		gap: 8px;
		align-items: center;
	}

	.rule-row select,
	.rule-row input {
		padding: 6px 8px;
		border: 1px solid var(--border);
		border-radius: 4px;
		font-size: 0.85rem;
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
	}

	.rule-row select:focus,
	.rule-row input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.link-btn {
		background: none;
		border: none;
		color: var(--accent);
		cursor: pointer;
		font-size: 0.78rem;
		padding: 0;
		font-family: inherit;
	}

	.link-btn:hover {
		filter: brightness(1.2);
	}

	.add-level-btn {
		padding: 10px 14px;
		border: 1px dashed var(--border);
		border-radius: 8px;
		background: transparent;
		cursor: pointer;
		font-size: 0.85rem;
		color: var(--text-muted);
		font-family: inherit;
	}

	.add-level-btn:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.hint {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 4px 0 0 0;
	}
</style>
