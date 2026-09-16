<script lang="ts">
	import IconUpload from '~icons/material-symbols/upload-file-outline';
	import IconBolt from '~icons/material-symbols/bolt';
	import IconApprove from '~icons/material-symbols/fact-check-outline';
	import IconPay from '~icons/material-symbols/payments-outline';
	import Badge from '$lib/components/ui/Badge.svelte';

	/**
	 * The hero's moving part: one invoice walking the pipeline the product
	 * exists to automate — captured, extracted field by field, matched against
	 * its PO, approved, paid. The card assembles once in under four seconds and
	 * holds; the extraction sweep and the stage rail keep cycling over it.
	 *
	 * **It is a drawing, not a screenshot.** A screenshot of the real app would
	 * be a maintenance liability (it goes stale on the next restyle and nobody
	 * notices) and would show a seeded tenant's data at a size where none of it
	 * is legible. This shows the four states a viewer needs to understand in
	 * order, at a size where each is readable, and it stays true because the
	 * states are the product's actual workflow (`workflow_engine.VALID_TRANSITIONS`).
	 *
	 * **Every animation ends where the element should rest.** `app.css` collapses
	 * `animation-duration` to 0.001ms under `prefers-reduced-motion`, which means
	 * the final keyframe is the *entire* experience for that visitor — so the
	 * end state of each loop is the finished card, fully populated and marked
	 * paid, rather than the blank frame the loop starts from. A visitor who asked
	 * for less motion gets the completed story, not an empty card.
	 *
	 * Whole thing is `aria-hidden`: it re-states the surrounding copy in pictures
	 * and its animated field values would otherwise be announced mid-transition
	 * as a stream of half-written numbers.
	 */

	// Staggered via a CSS custom property rather than seven near-identical rules.
	const fields = [
		{ label: 'Vendor', value: 'Northwind Suppliers Ltd', confidence: 98 },
		{ label: 'Invoice no.', value: 'INV-2026-00418', confidence: 99 },
		{ label: 'Amount', value: '$12,480.00', confidence: 96 },
		{ label: 'Due', value: 'Apr 29, 2026', confidence: 94 },
		{ label: 'GL code', value: '5200 · Cost of goods', confidence: 91 }
	];

	const stages = [
		{ icon: IconUpload, label: 'Capture' },
		{ icon: IconBolt, label: 'Extract' },
		{ icon: IconApprove, label: 'Approve' },
		{ icon: IconPay, label: 'Pay' }
	];
</script>

<div class="stage" aria-hidden="true">
	<!-- The two cards behind: depth, and a hint that this is a queue. -->
	<div class="ghost ghost-2"></div>
	<div class="ghost ghost-1"></div>

	<div class="card">
		<div class="card-head">
			<Badge tone="success">Ready for review</Badge>
			<span class="conf">3-way matched</span>
		</div>

		<div class="doc">
			<!-- The extraction sweep. Ends off the bottom edge at zero alpha, so
			     the resting frame is a clean card rather than a bar across it. -->
			<div class="sweep"></div>
			{#each fields as field, i}
				<div class="field" style="--i: {i}">
					<span class="f-label">{field.label}</span>
					<span class="f-value">{field.value}</span>
					<span class="f-meter"><span class="f-fill" style="--pct: {field.confidence}%"></span></span>
				</div>
			{/each}
		</div>

		<div class="card-foot">
			<span class="stamp">Approved · Paid via virtual card</span>
		</div>
	</div>

	<div class="rail">
		{#each stages as stage, i}
			<div class="stop" style="--i: {i}">
				<span class="dot"><stage.icon /></span>
				<span class="stop-label">{stage.label}</span>
			</div>
		{/each}
		<div class="rail-line"><span class="rail-fill"></span></div>
	</div>
</div>

<style>
	.stage {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 22px;
		/* Two clocks, deliberately.
		   --build is the ONE-SHOT assembly of the card: fields, meters, the
		   match label, the stamp. It runs once and holds, because the finished
		   card is the frame worth looking at — the first cut ran it on the same
		   16s clock as everything else, and a visitor glancing for the usual
		   three seconds saw an empty band where the stamp was still ten seconds
		   away.
		   --loop is the AMBIENT cycle that keeps going over the finished card:
		   the sweep, the rail and the lift. */
		--build: 3.6s;
		--loop: 16s;
	}

	/* ------------------------------ the card ------------------------------ */
	.card {
		position: relative;
		z-index: 3;
		border-radius: 16px;
		padding: 18px;
		background: linear-gradient(180deg, rgba(31, 35, 48, 0.94), rgba(20, 22, 31, 0.94));
		border: 1px solid rgba(226, 228, 234, 0.10);
		box-shadow:
			0 32px 70px -28px rgba(0, 0, 0, 0.85),
			inset 0 1px 0 rgba(255, 255, 255, 0.05);
		backdrop-filter: blur(12px);
		animation: card-lift var(--loop) ease-in-out infinite;
	}
	@keyframes card-lift {
		0%, 100% { transform: translate3d(0, 0, 0); }
		50% { transform: translate3d(0, -8px, 0); }
	}

	.ghost {
		position: absolute;
		left: 0;
		right: 0;
		top: 0;
		height: 190px;
		border-radius: 16px;
		border: 1px solid rgba(226, 228, 234, 0.07);
		background: rgba(24, 26, 35, 0.55);
	}
	.ghost-1 {
		z-index: 2;
		transform: translate3d(14px, 16px, 0) rotate(1.6deg);
	}
	.ghost-2 {
		z-index: 1;
		transform: translate3d(28px, 32px, 0) rotate(3.2deg);
		border-color: rgba(226, 228, 234, 0.04);
		background: rgba(24, 26, 35, 0.3);
	}

	.card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		margin-bottom: 14px;
	}
	.conf {
		font-size: 0.74rem;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--success-on-tint);
		/* Lands late in the build, once the fields it vouches for are in; ends
		   visible so the resting frame is the matched invoice. */
		animation: fade-in 0.5s ease-out calc(var(--build) * 0.72) backwards;
	}

	/* ----------------------------- the fields ----------------------------- */
	.doc {
		position: relative;
		overflow: hidden;
		border-radius: 10px;
		border: 1px solid rgba(226, 228, 234, 0.07);
		background: rgba(15, 17, 23, 0.6);
		padding: 4px 12px;
	}

	.sweep {
		position: absolute;
		left: 0;
		right: 0;
		top: 0;
		height: 46px;
		background: linear-gradient(
			180deg,
			transparent,
			rgba(99, 140, 255, 0.22) 55%,
			rgba(99, 140, 255, 0.55) 92%,
			transparent
		);
		animation: sweep var(--loop) cubic-bezier(0.5, 0, 0.5, 1) infinite;
	}
	@keyframes sweep {
		0% { transform: translate3d(0, -60px, 0); opacity: 0; }
		8% { opacity: 1; }
		34% { transform: translate3d(0, 230px, 0); opacity: 1; }
		40%, 100% { transform: translate3d(0, 230px, 0); opacity: 0; }
	}

	.field {
		display: grid;
		grid-template-columns: 82px 1fr 46px;
		align-items: center;
		gap: 10px;
		padding: 9px 0;
		border-bottom: 1px solid rgba(226, 228, 234, 0.05);
		/* Fields land top to bottom, a tenth of the build apart. `backwards` holds
		   the first frame through the delay, so nothing flashes in at full
		   opacity and then restarts. One-shot: no iteration count, so after the
		   build the card stays assembled and only the ambient loop keeps moving. */
		animation: field-in 0.45s ease-out calc(0.25s + var(--i) * var(--build) * 0.1) backwards;
	}
	.field:last-child {
		border-bottom: none;
	}
	@keyframes field-in {
		from { opacity: 0; transform: translate3d(-6px, 0, 0); }
		to { opacity: 1; transform: translate3d(0, 0, 0); }
	}

	.f-label {
		font-size: 0.7rem;
		letter-spacing: 0.05em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.f-value {
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.f-meter {
		height: 4px;
		border-radius: 999px;
		background: rgba(226, 228, 234, 0.09);
		overflow: hidden;
	}
	.f-fill {
		display: block;
		height: 100%;
		width: var(--pct);
		border-radius: inherit;
		background: linear-gradient(90deg, var(--accent), #a37dff);
		transform-origin: left center;
		animation: meter 0.7s ease-out calc(0.4s + var(--i) * var(--build) * 0.1) backwards;
	}
	@keyframes meter {
		from { transform: scaleX(0); }
		to { transform: scaleX(1); }
	}

	.card-foot {
		margin-top: 14px;
	}
	.stamp {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		padding: 7px 13px;
		border-radius: 999px;
		border: 1px solid rgba(38, 185, 119, 0.35);
		background: rgba(38, 185, 119, 0.10);
		color: var(--success-on-tint);
		font-size: 0.78rem;
		font-weight: 600;
		animation: stamp-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) var(--build) backwards;
	}
	.stamp::before {
		content: '';
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--success);
		box-shadow: 0 0 0 0 rgba(31, 168, 106, 0.55);
		animation: ping 2.4s ease-out infinite;
	}
	@keyframes stamp-in {
		from { opacity: 0; transform: scale(0.88); }
		to { opacity: 1; transform: scale(1); }
	}
	@keyframes ping {
		0% { box-shadow: 0 0 0 0 rgba(31, 168, 106, 0.5); }
		70%, 100% { box-shadow: 0 0 0 9px rgba(31, 168, 106, 0); }
	}

	/* ------------------------------ the rail ------------------------------ */
	.rail {
		position: relative;
		z-index: 3;
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 8px;
		padding-top: 16px;
	}
	.rail-line {
		position: absolute;
		top: 0;
		left: 12%;
		right: 12%;
		height: 2px;
		border-radius: 999px;
		background: rgba(226, 228, 234, 0.08);
		overflow: hidden;
	}
	.rail-fill {
		display: block;
		height: 100%;
		width: 100%;
		background: linear-gradient(90deg, var(--accent), #a37dff, #e7b95e);
		transform-origin: left center;
		animation: rail var(--loop) cubic-bezier(0.65, 0, 0.35, 1) infinite;
	}
	@keyframes rail {
		0% { transform: scaleX(0); }
		/* Four even steps — one per stage — with a hold at each, so the fill
		   reads as a pipeline advancing rather than a progress bar sliding. */
		10%, 22% { transform: scaleX(0.25); }
		34%, 46% { transform: scaleX(0.5); }
		58%, 70% { transform: scaleX(0.75); }
		82%, 100% { transform: scaleX(1); }
	}

	.stop {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		text-align: center;
	}
	.dot {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		height: 34px;
		border-radius: 50%;
		border: 1px solid rgba(226, 228, 234, 0.12);
		background: rgba(24, 26, 35, 0.92);
		color: var(--text-muted);
		font-size: 17px;
		animation: stop-live var(--loop) ease-in-out infinite;
		animation-delay: calc(var(--i) * var(--loop) * 0.24);
	}
	/* Colour and scale together, so the active stop reads at a glance and the
	   last frame leaves every stop lit — the finished pipeline. */
	@keyframes stop-live {
		0%, 4% {
			color: var(--text-muted);
			border-color: rgba(226, 228, 234, 0.12);
			transform: scale(1);
		}
		12%, 26% {
			color: var(--accent-on-tint);
			border-color: rgba(99, 140, 255, 0.55);
			transform: scale(1.12);
		}
		36%, 100% {
			color: var(--accent-on-tint);
			border-color: rgba(99, 140, 255, 0.3);
			transform: scale(1);
		}
	}
	.stop-label {
		font-size: 0.72rem;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	@media (max-width: 520px) {
		/* The stacked cards step right by their translate, which on a phone
		   pushes their edge past the viewport. Halve the step. */
		.ghost-1 {
			transform: translate3d(7px, 12px, 0) rotate(1.2deg);
		}
		.ghost-2 {
			transform: translate3d(14px, 24px, 0) rotate(2.4deg);
		}
		.field {
			grid-template-columns: 74px 1fr;
		}
		.f-meter {
			display: none;
		}
	}
</style>
