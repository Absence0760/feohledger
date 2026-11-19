<script lang="ts">
	/**
	 * The busy ring: a request the user is waiting on is in flight.
	 *
	 * This is the ONE looping animation the motion budget allows
	 * (`ui-patterns.md` § Motion, rule 4a; guard `a11y/marketingMotion.test.ts`),
	 * and only on two conditions the guard checks at every call site: the
	 * caller mounts it inside an `{#if}` on the request's state, so it unmounts
	 * when the answer lands, and text says what is happening — `label` here, or
	 * the caller's own live region / field hint. A ring left on a settled page
	 * would loop forever, which the exemption does not cover.
	 *
	 * With `label` it is its own `role="status"` region (`hideLabel` keeps the
	 * text for screen readers only). Without one the ring is decorative and
	 * hidden from assistive tech: the caller's text carries the meaning.
	 * Reduced motion shows a still ring.
	 */
	interface Props {
		/** Diameter in CSS px. */
		size?: number;
		/** What is happening, e.g. "Signing you in…". Makes this a status region. */
		label?: string;
		/** Keep `label` for screen readers only (the page already says it visibly). */
		hideLabel?: boolean;
	}

	let { size = 18, label, hideLabel = false }: Props = $props();

	// Thinner stroke on the small inline ring, so it does not read as a blob.
	const stroke = $derived(size <= 24 ? 2 : 3);
</script>

{#if label}
	<span class="spinner-status" role="status">
		<span class="spinner" style="--spinner-size: {size}px; --spinner-stroke: {stroke}px" aria-hidden="true"
		></span>
		<span class:visually-hidden={hideLabel}>{label}</span>
	</span>
{:else}
	<span class="spinner" style="--spinner-size: {size}px; --spinner-stroke: {stroke}px" aria-hidden="true"
	></span>
{/if}

<style>
	.spinner-status {
		display: inline-flex;
		align-items: center;
		gap: 10px;
	}
	.spinner {
		display: inline-block;
		flex: none;
		box-sizing: border-box;
		width: var(--spinner-size);
		height: var(--spinner-size);
		border: var(--spinner-stroke) solid var(--border);
		border-top-color: var(--accent);
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	/* app.css already collapses every iteration count to 1 under reduced
	   motion; the ring says so itself too, so it never depends on that rule
	   reaching it. A still ring beside its text is the resting state. */
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
	}
</style>
