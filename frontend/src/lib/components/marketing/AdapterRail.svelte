<script lang="ts">
	/**
	 * The row of shipped adapters.
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
	 * Static, deliberately: a wrapped, centred row rather than the marquee it
	 * used to be. A 46-second scroll cannot fit inside the five seconds of
	 * motion this page allows itself (WCAG 2.2.2 with no pause control,
	 * decisions §279), and a row the reader can scan beats one they have to
	 * wait for — every name is visible at once, nothing is masked at the edges,
	 * and the list is rendered once instead of twice.
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
	<ul class="set">
		{#each adapters as adapter}
			<li>{adapter}</li>
		{/each}
	</ul>
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

	.set {
		display: flex;
		flex-wrap: wrap;
		justify-content: center;
		align-items: center;
		gap: 10px 12px;
		margin: 0;
		padding: 0;
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
