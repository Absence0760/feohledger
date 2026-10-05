<script lang="ts">
	import ToolResultView from '#lib/components/assistant/ToolResultView.svelte';
	import CashPositionChart from '#lib/components/cash-flow/CashPositionChart.svelte';
	import PlanCard from '#lib/components/cash-flow/PlanCard.svelte';
	import type { ToolInvocation } from '#lib/types/assistant.ts';
	import type { CashPositionResult, PaymentPlanResult } from '#lib/types/cashFlow.ts';

	let { invocation }: { invocation: ToolInvocation } = $props();

	// The cash-position tool gets the dedicated running-balance chart, and the
	// Phase 2 propose_payment_plan tool gets the plan card; every other tool
	// (the base assistant tools + the other cash-flow tools) reuses the
	// assistant's `ToolResultView` dispatcher unchanged.
	let position = $derived(
		invocation.tool === 'get_cash_position' && invocation.result && !invocation.error
			? (invocation.result as unknown as CashPositionResult)
			: null
	);
	let plan = $derived(
		invocation.tool === 'propose_payment_plan' && invocation.result && !invocation.error
			? (invocation.result as unknown as PaymentPlanResult)
			: null
	);
</script>

{#if position}
	<CashPositionChart result={position} />
{:else if plan}
	<PlanCard result={plan} />
{:else}
	<ToolResultView {invocation} />
{/if}
