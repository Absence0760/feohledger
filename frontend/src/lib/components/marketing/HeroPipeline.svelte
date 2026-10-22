<script lang="ts">
	import IconUpload from '~icons/material-symbols/upload-file-outline';
	import IconBolt from '~icons/material-symbols/bolt';
	import IconApprove from '~icons/material-symbols/fact-check-outline';
	import IconPay from '~icons/material-symbols/payments-outline';
	import Badge from '#lib/components/ui/Badge.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatMoney } from '#lib/utils/money.ts';
	import { formatDate } from '#lib/utils/time.ts';

	/**
	 * The hero's moving part: one invoice walking the pipeline the product
	 * exists to automate — captured, extracted field by field, matched against
	 * its PO, approved, paid. The card assembles once in about four seconds, the
	 * extraction sweep and the stage rail run once over it, and then everything
	 * holds on the finished card. **Nothing here runs past five seconds** — that
	 * is how the page meets WCAG 2.2.2 with no pause control (decisions §279).
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
	 * the final keyframe is the *entire* experience for that visitor — and, with
	 * `forwards` fill, it is what every visitor is left looking at once the
	 * five seconds are up. So the end state of each animation is the finished
	 * card, fully populated and marked paid, rather than the blank frame it
	 * starts from.
	 *
	 * Whole thing is `aria-hidden`: it re-states the surrounding copy in pictures
	 * and its animated field values would otherwise be announced mid-transition
	 * as a stream of half-written numbers.
	 */

	// The sample invoice. Its vendor and number are invented proper nouns and
	// stay as written; everything a reader would see differently in their own
	// language — the labels, the amount, the date — is localized, because this
	// is the first picture of the product a German or Japanese visitor sees.
	// Read inside `$derived`, so a language switch redraws it.
	const fields = $derived([
		{ label: m('marketing.demo.vendor'), value: 'Northwind Suppliers Ltd', confidence: 98 },
		{ label: m('marketing.demo.invoiceNo'), value: 'INV-2026-00418', confidence: 99 },
		{ label: m('marketing.demo.amount'), value: formatMoney('12480.00', { currency: 'USD' }), confidence: 96 },
		{
			label: m('marketing.demo.due'),
			value: formatDate('2026-04-29', '—', { year: 'numeric', month: 'short', day: 'numeric' }),
			confidence: 94
		},
		{ label: m('marketing.demo.glCode'), value: `5200 · ${m('marketing.demo.glName')}`, confidence: 91 }
	]);

	const stages = $derived([
		{ icon: IconUpload, label: m('marketing.demo.stage.capture') },
		{ icon: IconBolt, label: m('marketing.demo.stage.extract') },
		{ icon: IconApprove, label: m('marketing.demo.stage.approve') },
		{ icon: IconPay, label: m('marketing.demo.stage.pay') }
	]);
</script>

<div class="stage" aria-hidden="true">
	<!-- The two cards behind: depth, and a hint that this is a queue. -->
	<div class="ghost ghost-2"></div>
	<div class="ghost ghost-1"></div>

	<div class="card">
		<div class="card-head">
			<Badge tone="success">{m('marketing.demo.status')}</Badge>
			<span class="conf">{m('marketing.demo.matched')}</span>
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
			<span class="stamp">{m('marketing.demo.stamp')}</span>
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
		/* --build is the assembly of the card: fields, meters, the match label,
		   the stamp. The finished card is the frame worth looking at, so it lands
		   early — an earlier cut ran it on a 16s loop, and a visitor glancing for
		   the usual three seconds saw an empty band where the stamp was still ten
		   seconds away. The lift, the sweep, the rail and the stage dots used to
		   cycle forever over the result; each now runs once and every animation
		   in this file has finished by 5s (delay + duration), which the
		   landing spec measures. */
		--build: 3.6s;
	}

	/* ------------------------------ the card ------------------------------ */
	.card {
		position: relative;
		z-index: 3;
		border-radius: 16px;
		padding: 18px;
		background: linear-gradient(180deg, color-mix(in srgb, var(--surface-2) 55%, var(--surface)), var(--surface));
		border: 1px solid var(--border-soft);
		box-shadow: var(--shadow-float);
		animation: card-lift 4s ease-in-out 1 forwards;
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
		border: 1px solid color-mix(in srgb, var(--text) 7%, transparent);
		background: color-mix(in srgb, var(--surface) 55%, transparent);
	}
	.ghost-1 {
		z-index: 2;
		transform: translate3d(14px, 16px, 0) rotate(1.6deg);
	}
	.ghost-2 {
		z-index: 1;
		transform: translate3d(28px, 32px, 0) rotate(3.2deg);
		border-color: color-mix(in srgb, var(--text) 4%, transparent);
		background: color-mix(in srgb, var(--surface) 30%, transparent);
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
		border: 1px solid color-mix(in srgb, var(--text) 7%, transparent);
		background: color-mix(in srgb, var(--bg) 60%, transparent);
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
			color-mix(in srgb, var(--accent) 22%, transparent) 55%,
			color-mix(in srgb, var(--accent) 55%, transparent) 92%,
			transparent
		);
		/* One pass, alongside the fields landing; ends below the last field at
		   zero alpha, so the resting card has no bar across it. */
		animation: sweep 2.4s cubic-bezier(0.5, 0, 0.5, 1) 0.25s 1 both;
	}
	@keyframes sweep {
		0% { transform: translate3d(0, -60px, 0); opacity: 0; }
		20% { opacity: 1; }
		85% { transform: translate3d(0, 230px, 0); opacity: 1; }
		100% { transform: translate3d(0, 230px, 0); opacity: 0; }
	}

	.field {
		display: grid;
		grid-template-columns: 82px 1fr 46px;
		align-items: center;
		gap: 10px;
		padding: 9px 0;
		border-bottom: 1px solid var(--hairline);
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
		background: color-mix(in srgb, var(--text) 9%, transparent);
		overflow: hidden;
	}
	.f-fill {
		display: block;
		height: 100%;
		width: var(--pct);
		border-radius: inherit;
		background: linear-gradient(90deg, var(--accent), var(--brand-violet));
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
		border: 1px solid color-mix(in srgb, var(--success) 35%, transparent);
		background: var(--success-tint);
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
		box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 55%, transparent);
		/* One ring as the stamp lands (3.6s + 1.2s = 4.8s), ending ringless. */
		animation: ping 1.2s ease-out var(--build) 1 forwards;
	}
	@keyframes stamp-in {
		from { opacity: 0; transform: scale(0.88); }
		to { opacity: 1; transform: scale(1); }
	}
	@keyframes ping {
		0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--success) 50%, transparent); }
		70%, 100% { box-shadow: 0 0 0 9px color-mix(in srgb, var(--success) 0%, transparent); }
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
		background: color-mix(in srgb, var(--text) 8%, transparent);
		overflow: hidden;
	}
	.rail-fill {
		display: block;
		height: 100%;
		width: 100%;
		background: linear-gradient(90deg, var(--accent), var(--brand-violet), var(--gilt));
		transform-origin: left center;
		animation: rail 4.4s cubic-bezier(0.65, 0, 0.35, 1) 1 forwards;
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
		border: 1px solid color-mix(in srgb, var(--text) 12%, transparent);
		background: color-mix(in srgb, var(--surface) 92%, transparent);
		color: var(--text-muted);
		font-size: 17px;
		/* Lit in turn, 0.8s apart: the last stop ends at 3 × 0.8 + 1.6 = 4s.
		   `forwards` keeps each one lit, so the rest frame is the whole
		   pipeline done; before its turn a stop shows its base (unlit) style,
		   which is also the first keyframe. */
		animation: stop-live 1.6s ease-in-out calc(var(--i) * 0.8s) 1 forwards;
	}
	/* Colour and scale together, so the active stop reads at a glance and the
	   last frame leaves every stop lit — the finished pipeline. */
	@keyframes stop-live {
		0%, 4% {
			color: var(--text-muted);
			border-color: color-mix(in srgb, var(--text) 12%, transparent);
			transform: scale(1);
		}
		12%, 26% {
			color: var(--accent-on-tint);
			border-color: color-mix(in srgb, var(--accent) 55%, transparent);
			transform: scale(1.12);
		}
		36%, 100% {
			color: var(--accent-on-tint);
			border-color: color-mix(in srgb, var(--accent) 30%, transparent);
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
