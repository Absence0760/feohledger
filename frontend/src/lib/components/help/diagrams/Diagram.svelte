<script lang="ts">
	/**
	 * Renders the guide illustration `id` (see DIAGRAM_IDS in #lib/help/types.ts).
	 *
	 * Each diagram draws itself twice: a landscape `svg.d-wide` and a portrait
	 * `svg.d-tall`. A landscape picture scaled down to a phone would shrink its
	 * text to a few pixels, so below 40rem of guide column the portrait one is
	 * shown instead — a container query on the help centre's `help-main`, so it
	 * follows the column, not the window. The hidden copy is `display: none`,
	 * which also takes it out of the accessibility tree; each carries the same
	 * translated aria-label.
	 */
	import type { Component } from 'svelte';
	import type { DiagramId } from '#lib/help/types.ts';
	import CaptureChannels from './CaptureChannels.svelte';
	import ThreeWayMatch from './ThreeWayMatch.svelte';
	import ApprovalChain from './ApprovalChain.svelte';
	import SegregationOfDuties from './SegregationOfDuties.svelte';
	import BankChangeDualControl from './BankChangeDualControl.svelte';
	import PaymentRun from './PaymentRun.svelte';
	import ExceptionFlow from './ExceptionFlow.svelte';
	import RolesOverview from './RolesOverview.svelte';

	let { id }: { id: DiagramId } = $props();

	const DIAGRAMS: Record<DiagramId, Component> = {
		'capture-channels': CaptureChannels,
		'three-way-match': ThreeWayMatch,
		'approval-chain': ApprovalChain,
		'segregation-of-duties': SegregationOfDuties,
		'bank-change-dual-control': BankChangeDualControl,
		'payment-run': PaymentRun,
		'exception-flow': ExceptionFlow,
		'roles-overview': RolesOverview
	};
	const Drawing = $derived(DIAGRAMS[id]);
</script>

<div class="diagram" data-diagram={id}><Drawing /></div>

<style>
	.diagram :global(svg) {
		display: block;
		overflow: visible;
		font-family: inherit;
	}
	.diagram :global(svg.d-tall) {
		display: none;
		max-width: 22rem;
		margin-inline: auto;
	}
	@container help-main (max-width: 40rem) {
		.diagram :global(svg.d-wide) {
			display: none;
		}
		.diagram :global(svg.d-tall) {
			display: block;
		}
	}
</style>
