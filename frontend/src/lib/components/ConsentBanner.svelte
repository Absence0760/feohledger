<script lang="ts" module>
	/**
	 * Cookie / consent banner — GDPR (ePrivacy) + CCPA non-essential consent.
	 *
	 * Scope note: the app authenticates with a JWT held in `localStorage`
	 * (essential storage), so this banner governs ONLY *non-essential*
	 * (analytics / product) storage. Essential auth/session storage is exempt
	 * from consent under both regimes and is never gated by this choice. The
	 * copy stays honest about that.
	 *
	 * The choice is persisted in `localStorage` under `CONSENT_KEY`; once a
	 * value is recorded the banner stays hidden on every subsequent visit.
	 * Other code can read the recorded choice via `getConsent()` before
	 * loading any non-essential script.
	 */
	export type ConsentChoice = 'accepted' | 'rejected';

	export const CONSENT_KEY = 'feoh_consent_choice';

	/** Read the persisted non-essential-storage consent choice (or `null`). */
	export function getConsent(): ConsentChoice | null {
		if (typeof localStorage === 'undefined') return null;
		const v = localStorage.getItem(CONSENT_KEY);
		return v === 'accepted' || v === 'rejected' ? v : null;
	}
</script>

<script lang="ts">
	import { browser } from '$app/environment';

	// `undefined` until we've read localStorage on the client — render nothing
	// during that window so the banner never flashes for a user who already chose.
	let choice = $state<ConsentChoice | null | undefined>(undefined);
	let showDetails = $state(false);
	// The banner's rendered height, for the room it reserves below.
	let height = $state(0);

	$effect(() => {
		if (browser) choice = getConsent();
	});

	const visible = $derived(browser && choice === null);

	// Matches the banner's `bottom: 16px` in the styles below.
	const EDGE_PX = 16;

	// The banner is `position: fixed`, so on a short viewport it lands on a page's
	// own controls: at 1280×720 it covers the reset-password submit button, on a
	// page exactly one viewport tall with nothing to scroll. While it shows, it
	// reserves its footprint instead. The spacer after it makes the document tall
	// enough to scroll anything out from under it, and `scroll-padding-bottom`
	// makes keyboard focus and scrollIntoView stop above it rather than behind it
	// (WCAG 2.4.11 Focus Not Obscured). The edge offset counts twice: once below
	// the banner, once as a gap above it.
	const inset = $derived(visible && height > 0 ? height + EDGE_PX * 2 : 0);

	$effect(() => {
		if (inset === 0) return;
		const root = document.documentElement;
		root.style.scrollPaddingBottom = `${inset}px`;
		return () => root.style.removeProperty('scroll-padding-bottom');
	});

	function record(value: ConsentChoice) {
		try {
			localStorage.setItem(CONSENT_KEY, value);
		} catch {
			// Storage may be blocked (private mode / disabled). Hiding the banner
			// for the session is the safe, honest outcome — we simply won't load
			// any non-essential script either way.
		}
		choice = value;
	}
</script>

{#if visible}
	<section
		class="consent"
		role="region"
		aria-live="polite"
		aria-label="Cookie and privacy consent"
		bind:offsetHeight={height}
	>
		<div class="consent-body">
			<div class="consent-copy">
				<h2 class="consent-title">Your privacy choices</h2>
				<p>
					We use storage that is strictly necessary to run the app — signing
					you in and keeping your session — which works regardless of your
					choice here. We load <strong>no analytics or tracking</strong> storage
					today. Your choice is recorded now and governs any optional storage we
					introduce later. Full detail is in the
					<a href="/legal/cookies">Cookie Notice</a>.
				</p>
				{#if showDetails}
					<dl class="consent-details">
						<dt>Strictly necessary (always on)</dt>
						<dd>
							Authentication token and session state. Required to log in and
							use the app; cannot be turned off.
						</dd>
						<dt>Analytics (optional)</dt>
						<dd>
							<!--
								This category is empty, and saying so is the point. The copy
								previously read "Aggregate product-usage measurement to improve
								the app", which described a thing the frontend does not do:
								there is no analytics script anywhere in it, and `getConsent()`
								has no caller. Soliciting consent for processing that isn't
								happening is an accuracy problem in its own right, so the
								category stays — the mechanism should exist before it is
								needed — but it states what is true today.
							-->
							Nothing is loaded under this category today. If product analytics
							is added, it stays off unless you have accepted it, and the Cookie
							Notice is updated before anything is set.
						</dd>
					</dl>
				{/if}
			</div>
			<div class="consent-actions">
				<button type="button" class="btn-link" onclick={() => (showDetails = !showDetails)}>
					{showDetails ? 'Hide details' : 'Manage'}
				</button>
				<button type="button" class="btn-secondary" onclick={() => record('rejected')}>
					Reject non-essential
				</button>
				<button type="button" class="btn-accent" onclick={() => record('accepted')}>
					Accept all
				</button>
			</div>
		</div>
	</section>
	<div aria-hidden="true" style:height="{inset}px"></div>
{/if}

<style>
	.consent {
		position: fixed;
		left: 16px;
		right: 16px;
		bottom: 16px;
		z-index: 10000;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 10px;
		box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
		color: var(--text);
		max-width: 920px;
		margin: 0 auto;
	}

	.consent-body {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-start;
		gap: 20px;
		padding: 18px 20px;
	}

	.consent-copy {
		flex: 1 1 360px;
		min-width: 0;
		font-size: 0.88rem;
		line-height: 1.5;
		color: var(--text);
	}

	.consent-title {
		margin: 0 0 6px;
		font-size: 1rem;
		font-weight: 600;
	}

	.consent-copy p {
		margin: 0;
		color: var(--text-muted);
	}

	.consent-copy strong {
		color: var(--text);
	}

	.consent-copy a {
		color: var(--accent-on-tint);
	}

	.consent-details {
		margin: 12px 0 0;
		padding: 12px 14px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 8px;
		font-size: 0.82rem;
	}

	.consent-details dt {
		font-weight: 600;
		color: var(--text);
	}

	.consent-details dd {
		margin: 2px 0 10px;
		color: var(--text-muted);
	}

	.consent-details dd:last-child {
		margin-bottom: 0;
	}

	.consent-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 10px;
		flex-shrink: 0;
	}

	.consent-actions button {
		font: inherit;
		font-size: 0.85rem;
		padding: 8px 16px;
		border-radius: 6px;
		cursor: pointer;
		border: 1px solid transparent;
	}

	.btn-link {
		background: transparent;
		border-color: transparent;
		color: var(--accent);
		text-decoration: underline;
		padding-left: 4px;
		padding-right: 4px;
	}

	.btn-secondary {
		background: transparent;
		border-color: var(--border);
		color: var(--text);
	}

	.btn-secondary:hover {
		border-color: var(--text-muted);
	}

	.btn-accent {
		background: var(--accent-strong);
		border-color: var(--accent-strong);
		color: #fff;
		font-weight: 600;
	}

	.btn-accent:hover {
		filter: brightness(1.08);
	}

	/* AA-contrast, always-visible keyboard focus. */
	.consent-actions button:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	@media (max-width: 640px) {
		.consent-actions {
			width: 100%;
		}

		.consent-actions button {
			flex: 1 1 auto;
		}
	}
</style>
