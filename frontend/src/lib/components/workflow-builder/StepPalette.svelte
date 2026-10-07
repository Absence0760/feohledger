<script lang="ts">
	import type { WorkflowStepType } from '#lib/types/workflow.ts';
	import { STEP_TYPE_LABELS, STEP_TYPE_DESCRIPTIONS } from '#lib/types/workflow.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import HelpTip from '#lib/components/help/HelpTip.svelte';

	// Click-to-add only. Steps are dragged once they're ON the canvas, where
	// the order lives; dragging from here was a second, worse way to add one.
	type Props = {
		onadd: (type: WorkflowStepType) => void;
	};

	let { onadd }: Props = $props();

	const PALETTE: WorkflowStepType[] = [
		'extraction',
		'approval',
		'erp_export',
		'condition',
		'parallel',
		'webhook',
		'email',
		'delay',
	];

	const ICONS: Record<WorkflowStepType, string> = {
		extraction: 'M9 2L4.5 6.5 9 11M15 2l4.5 4.5L15 11M12 2v9',
		approval: 'M9 12l2 2 4-4m5 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
		erp_export: 'M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2M7 10l5 5 5-5M12 15V3',
		condition: 'M12 2v6m0 0L7 13m5-5l5 5M4 17a3 3 0 1 0 6 0 3 3 0 0 0-6 0zm10 0a3 3 0 1 0 6 0 3 3 0 0 0-6 0z',
		parallel: 'M4 6h4M4 12h4M4 18h4M14 6h6M14 12h6M14 18h6M8 12h6',
		webhook: 'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1',
		email: 'M4 4h16v16H4zM4 6l8 6 8-6',
		delay: 'M12 6v6l4 2m5-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z',
	};
</script>

<div class="palette">
	<div class="palette-header">
		<div class="help-row palette-title-row">
			<span class="palette-label">{m('workflows.builder.palette.title')}</span>
			<HelpTip term="workflow" />
		</div>
		<p class="palette-hint">{m('workflows.builder.palette.hint')}</p>
	</div>
	<div class="palette-list">
		{#each PALETTE as type (type)}
			<button
				type="button"
				class="palette-item"
				onclick={() => onadd(type)}
				data-palette-type={type}
				title={STEP_TYPE_DESCRIPTIONS[type]}
				aria-label={m('workflows.builder.palette.addStep', { step: STEP_TYPE_LABELS[type] })}
			>
				<svg
					class="palette-icon"
					width="16"
					height="16"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					aria-hidden="true"
				>
					<path d={ICONS[type]} />
				</svg>
				<div class="palette-text">
					<span class="palette-name">{STEP_TYPE_LABELS[type]}</span>
					<span class="palette-desc">{STEP_TYPE_DESCRIPTIONS[type]}</span>
				</div>
			</button>
		{/each}
	</div>
</div>

<style>
	.palette {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		display: flex;
		flex-direction: column;
	}

	.palette-header {
		padding: 12px 14px;
		border-bottom: 1px solid var(--border);
	}

	/* The palette title's ⓘ row (global `.help-row`) sits at the label gap. */
	.palette-title-row {
		gap: 4px;
	}

	.palette-label {
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.palette-hint {
		margin: 4px 0 0;
		font-size: 0.74rem;
		color: var(--text-muted);
		line-height: 1.3;
	}

	.palette-list {
		padding: 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.palette-item {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 9px 10px;
		/* Solid border and a pointer cursor: these are buttons that add a step.
		   (Dashed + grab was the old "drag me" affordance.) */
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
		cursor: pointer;
		width: 100%;
		text-align: left;
		font: inherit;
		color: inherit;
		transition:
			border-color 0.15s,
			background 0.15s;
	}

	.palette-item:hover {
		border-color: var(--accent);
	}

	.palette-item:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.palette-icon {
		color: var(--accent);
		flex-shrink: 0;
		margin-top: 2px;
	}

	.palette-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.palette-name {
		font-size: 0.84rem;
		font-weight: 500;
		color: var(--text);
	}

	.palette-desc {
		font-size: 0.72rem;
		color: var(--text-muted);
		line-height: 1.3;
	}

</style>
