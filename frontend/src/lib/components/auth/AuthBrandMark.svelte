<script lang="ts">
	import BrandMark from '#lib/components/ui/BrandMark.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { hasTenantContext } from '#lib/tenant.ts';

	/**
	 * The mark at the top of a pre-auth page's form column — and, on the
	 * platform apex only, the way back to the landing page.
	 *
	 * **A link only on the apex** (`!hasTenantContext()`), where `/` is the
	 * marketing page a visitor arrived from. Everywhere else it stays a plain,
	 * decorative mark, deliberately:
	 *  - on a workspace subdomain `/` only redirects to `/login`, so a "home"
	 *    link would be a link to the page the visitor is already on, or one
	 *    step from it;
	 *  - on a customer's vanity host (white-label) it would send their users
	 *    to FeohLedger's marketing site, which is platform branding the
	 *    partner never chose (`docs/white-label.md`).
	 *
	 * As a link, the image stays `alt=""` and the link carries the name
	 * ("FeohLedger home"): a link whose only content is a decorative image has
	 * no accessible name at all (WCAG 2.4.4, 4.1.2).
	 *
	 * This is the one place the rule lives — every auth page renders its mark
	 * through here rather than deciding per page. The brand panel's mark in
	 * `AuthShell` is NOT a link: it would be a second link to the same place.
	 */
	interface Props {
		size?: number;
	}

	let { size = 40 }: Props = $props();

	const linkHome = !hasTenantContext();
</script>

{#if linkHome}
	<a class="auth-home" href="/" aria-label={m('auth.shell.homeLink')}>
		<BrandMark {size} />
	</a>
{:else}
	<BrandMark {size} />
{/if}

<style>
	.auth-home {
		display: inline-flex;
		border-radius: 10px;
	}
	.auth-home:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 3px;
	}
</style>
