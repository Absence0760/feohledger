<script lang="ts">
	/**
	 * The scrolling rail of shipped adapters.
	 *
	 * **It names adapters, not customers.** Every entry is a provider this repo
	 * has a registered adapter for — the families listed in `backend/CLAUDE.md`
	 * § Adapter patterns — under a heading that says so. A rail of logos under a
	 * vaguer heading is the standard marketing move and it is a claim about who
	 * uses the product, which would be false. If an adapter is removed, remove
	 * it here in the same change.
	 *
	 * Identity providers are deliberately absent. SSO is one generic OIDC flow
	 * and one SAML flow (`services/sso.py`, `api/auth_saml.py`), which Okta and Entra both work
	 * with, but neither has an adapter of its own — naming them here would
	 * assert a per-vendor integration the code does not have.
	 *
	 * Word marks as text rather than logo files: a third party's logo is their
	 * trademark, and using one implies an endorsement none of these have given.
	 * It also keeps the rail weightless — no images, no licence questions.
	 *
	 * The list is rendered twice and the track translates by exactly -50%, so
	 * the second copy is under the cursor at the moment the first wraps and the
	 * loop has no seam. The duplicate is `aria-hidden` so a screen reader is
	 * not read the same names twice.
	 *
	 * Motion: the rail pauses on hover and on keyboard focus within, and the
	 * page's `MotionToggle` stops it outright (WCAG 2.2.2) through the
	 * `data-motion` rule in `app.css`.
	 */
	const adapters = [
		'NetSuite',
		'Dynamics 365 BC',
		'Merge.dev',
		'Modern Treasury',
		'Stripe Treasury',
		'Increase',
		'Column',
		'Dwolla',
		'Lithic',
		'Nium',
		'Checkeeper',
		'AWS Textract',
		'Claude',
		'OpenAI',
		'Ollama',
		'PEPPOL AS4',
		'Slack',
		'Microsoft Teams'
	];
</script>

<div class="rail">
	<p class="rail-head">Ships with adapters for</p>
	<div class="viewport">
		<div class="track">
			<ul class="set">
				{#each adapters as adapter}
					<li>{adapter}</li>
				{/each}
			</ul>
			<ul class="set" aria-hidden="true">
				{#each adapters as adapter}
					<li>{adapter}</li>
				{/each}
			</ul>
		</div>
	</div>
</div>

<style>
	.rail {
		max-width: 1180px;
		margin: 0 auto;
		padding: 0 32px;
	}
	.rail-head {
		margin: 0 0 16px;
		text-align: center;
		font-size: 0.74rem;
		font-weight: 600;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--text-muted);
	}

	.viewport {
		overflow: hidden;
		/* Fade both ends into the page so names enter and leave rather than
		   being clipped at a hard edge. */
		mask-image: linear-gradient(90deg, transparent, #000 9%, #000 91%, transparent);
	}

	.track {
		display: flex;
		width: max-content;
		animation: marquee 46s linear infinite;
	}
	/* Both are genuine user-initiated states, so pausing here is a courtesy on
	   top of the page-level control rather than a substitute for it. */
	.viewport:hover .track,
	.viewport:focus-within .track {
		animation-play-state: paused;
	}
	@keyframes marquee {
		from { transform: translate3d(0, 0, 0); }
		to { transform: translate3d(-50%, 0, 0); }
	}

	.set {
		display: flex;
		align-items: center;
		gap: 14px;
		margin: 0;
		padding: 0 7px;
		list-style: none;
	}
	.set li {
		flex: none;
		padding: 9px 18px;
		border-radius: 999px;
		border: 1px solid var(--border);
		background: rgba(24, 26, 35, 0.55);
		color: var(--text-muted);
		font-size: 0.86rem;
		font-weight: 500;
		white-space: nowrap;
		transition: color 0.2s, border-color 0.2s;
	}
	.set li:hover {
		color: var(--text);
		border-color: rgba(99, 140, 255, 0.45);
	}
</style>
