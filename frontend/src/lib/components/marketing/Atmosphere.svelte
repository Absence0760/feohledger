<script lang="ts">
	import { asset } from '$app/paths';

	/**
	 * Named `Atmosphere`, with a root class of `.atmosphere`, and NOT `backdrop`:
	 * `app.css` owns a global `.backdrop` — the modal overlay — and a global
	 * class selector reaches any element carrying that class, scoped component
	 * or not. As `.backdrop` this layer silently inherited the overlay's 50%
	 * black fill (darkening every page it sat under), a `backdrop-filter` blur on
	 * a fixed full-viewport element (a repaint cost on every scroll), and grid
	 * layout with padding. Scoped rules only out-voted the two properties they
	 * happened to redeclare.
	 *
	 * The atmosphere behind every public-facing surface — the marketing page and
	 * the two auth shells. One component rather than three copies of the same
	 * four gradients, because the three pages have to look like the same product
	 * and they drift the moment a colour is typed twice.
	 *
	 * Four layers, back to front: a slow aurora of tinted blobs, a fine ruled
	 * grid, the generated grain, and a vignette that sinks the edges so page
	 * content sits on something quiet. All of it is `aria-hidden` and none of it
	 * carries text.
	 *
	 * `dense` tightens the grid and brings the aurora forward, for the narrow
	 * panel beside an auth card where the full-page spread reads as empty.
	 *
	 * `still` holds the aurora at its resting frame. The auth pages take it,
	 * and not for performance: WCAG 2.2.2 asks for a control over content that
	 * moves automatically for more than five seconds, and the honest answers are
	 * either a visible pause control or no continuous motion. A pause button on a
	 * sign-in form is clutter nobody wants, so those pages get their life from
	 * entrance and focus transitions — which are short and user-initiated, and so
	 * outside 2.2.2 — while the marketing page, which earns its ambient motion,
	 * carries the control (`MotionToggle`).
	 *
	 * `fixed` pins the layers to the viewport instead of the parent. The
	 * marketing page takes it: sized to a 5,700 px document, the percentage-
	 * positioned aurora blobs became thousand-pixel smears with dark diagonal
	 * gaps between them, and the atmosphere changed colour as you scrolled.
	 * Pinned, it stays one consistent field that the content slides over. The
	 * auth panel does not take it, because there the parent IS the frame.
	 */
	interface Props {
		dense?: boolean;
		still?: boolean;
		fixed?: boolean;
	}

	let { dense = false, still = false, fixed = false }: Props = $props();

	// The grain is a generated file (assets/marketing/gen-marketing.sh), so its
	// URL has to survive a non-empty `paths.base` — which a hardcoded `url()` in
	// the stylesheet would not. Handed to CSS as a custom property instead.
	const grain = `url(${asset('/marketing/grain.svg')})`;
</script>

<div class="atmosphere" class:dense class:still class:fixed aria-hidden="true" style="--grain-url: {grain}">
	<div class="aurora aurora-a"></div>
	<div class="aurora aurora-b"></div>
	<div class="aurora aurora-c"></div>
	<div class="grid"></div>
	<div class="grain"></div>
	<div class="vignette"></div>
</div>

<style>
	.atmosphere {
		position: absolute;
		inset: 0;
		overflow: hidden;
		pointer-events: none;
		z-index: 0;
	}
	.atmosphere.fixed {
		position: fixed;
	}

	/* ---------------------------------------------------------------
	   Aurora. Three blurred blobs drifting on long, mutually prime
	   periods (23s / 31s / 41s) so the composite never visibly repeats
	   — equal periods read as a loop within about two cycles, which is
	   the thing that makes a gradient backdrop look cheap.

	   Every keyframe ENDS where the layer should rest. `app.css`
	   collapses animation-duration to 0.001ms under
	   `prefers-reduced-motion`, so the final frame is what a
	   reduced-motion visitor actually sees — an animation that ended
	   off-screen or at zero opacity would render as a missing layer
	   rather than a still one.
	   --------------------------------------------------------------- */
	.aurora {
		position: absolute;
		border-radius: 50%;
		filter: blur(90px);
	}
	.aurora-a {
		top: -22%;
		right: -8%;
		width: 62%;
		height: 68%;
		background: radial-gradient(circle, rgba(99, 140, 255, 0.30), transparent 70%);
		animation: drift-a 23s ease-in-out infinite alternate;
	}
	.aurora-b {
		top: 18%;
		left: -18%;
		width: 55%;
		height: 60%;
		background: radial-gradient(circle, rgba(140, 106, 255, 0.24), transparent 70%);
		animation: drift-b 31s ease-in-out infinite alternate;
	}
	/* The one warm note: the brand's gilt, kept faint and low so it reads as a
	   reflection off the tally rather than as a second accent competing with
	   --accent. */
	.aurora-c {
		bottom: -18%;
		left: 34%;
		width: 48%;
		height: 46%;
		background: radial-gradient(circle, rgba(231, 185, 94, 0.13), transparent 72%);
		animation: drift-c 41s ease-in-out infinite alternate;
	}
	.dense .aurora-a {
		width: 100%;
		height: 58%;
		filter: blur(70px);
	}
	.dense .aurora-b {
		width: 92%;
		height: 52%;
		filter: blur(70px);
	}

	.still .aurora {
		animation: none;
	}

	@keyframes drift-a {
		from { transform: translate3d(0, 0, 0) scale(1); }
		to { transform: translate3d(-6%, 7%, 0) scale(1.12); }
	}
	@keyframes drift-b {
		from { transform: translate3d(0, 0, 0) scale(1.05); }
		to { transform: translate3d(9%, -6%, 0) scale(0.95); }
	}
	@keyframes drift-c {
		from { transform: translate3d(0, 0, 0) scale(0.95); }
		to { transform: translate3d(-8%, -5%, 0) scale(1.1); }
	}

	/* ---------------------------------------------------------------
	   The ruled grid: an accounts-payable product drawn on ledger
	   paper. Masked to fade out below the fold so it frames the hero
	   rather than tiling the whole document.
	   --------------------------------------------------------------- */
	.grid {
		position: absolute;
		inset: 0;
		background-image:
			linear-gradient(to right, rgba(226, 228, 234, 0.028) 1px, transparent 1px),
			linear-gradient(to bottom, rgba(226, 228, 234, 0.028) 1px, transparent 1px);
		background-size: 68px 68px;
		mask-image: radial-gradient(120% 78% at 50% 0%, #000 30%, transparent 78%);
	}
	.dense .grid {
		background-size: 40px 40px;
		mask-image: radial-gradient(110% 90% at 50% 12%, #000 25%, transparent 82%);
	}

	/* The generated feTurbulence tile. Its alpha is baked into the SVG, so this
	   needs no `opacity` of its own — see assets/marketing/gen-marketing.sh. */
	.grain {
		position: absolute;
		inset: 0;
		background-image: var(--grain-url);
		background-repeat: repeat;
		mix-blend-mode: overlay;
	}

	.vignette {
		position: absolute;
		inset: 0;
		background: radial-gradient(130% 90% at 50% 0%, transparent 42%, rgba(8, 9, 14, 0.62) 100%);
	}
</style>
