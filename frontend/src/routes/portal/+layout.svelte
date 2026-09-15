<script lang="ts">
	import { portalAuth } from '$lib/stores/portalAuth.svelte';
	import { portalBrand } from '$lib/stores/portalBrand.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { page } from '$app/stores';
	import { goto } from '$app/navigation';
	import { browser } from '$app/environment';
	import { hasTenantContext } from '$lib/tenant';
	import { m } from '$lib/i18n/store.svelte';

	// Tri-state: `undefined` until the browser resolves it, then true/false.
	// "Does this host carry a tenant", NOT "what is its slug" — a white-label
	// vanity host has a tenant but no slug in the URL (the backend resolves it
	// from `Host`), and gating on the slug showed a supplier the no-tenant page
	// on their customer's own domain. The portal never needs the slug value:
	// `$lib/portalApi` attaches the header when there is one.
	let hasTenant = $state<boolean | undefined>(undefined);

	$effect(() => {
		if (browser) hasTenant = hasTenantContext();
	});

	// Apply the tenant's white-label brand (accent colors + logo + product name +
	// <title>) across the WHOLE portal — the unauthenticated login page AND the
	// authed pages. The read is the public `GET /api/portal/branding`, so it works
	// before a vendor signs in. Fail-soft: any failure leaves the default theme.
	$effect(() => {
		if (hasTenant) portalBrand.ensureLoadedAndApply();
	});

	$effect(() => {
		if (!hasTenant) return;
		const path = $page.url.pathname;

		// `/portal/cards/<token>` is the email-emitted single-use reveal
		// link. The URL token is the credential — vendors don't need a
		// portal login to use it. Skip the auth redirect for this path.
		const isPublicCardReveal = path.startsWith('/portal/cards/');

		if (!portalAuth.loggedIn && path !== '/portal/login' && !isPublicCardReveal) {
			goto('/portal/login');
			return;
		}
		if (portalAuth.loggedIn && !portalAuth.user) {
			portalAuth.fetchUser();
			return;
		}
		if (
			portalAuth.loggedIn &&
			portalAuth.user?.must_change_password &&
			path !== '/portal/change-password'
		) {
			goto('/portal/change-password');
		}
	});

	async function handleLogout() {
		await portalAuth.logout();
	}
</script>

<svelte:head>
	<title>{m('portal.shell.title', { product: portalBrand.productName })}</title>
</svelte:head>

{#if hasTenant === undefined}
	<!-- hydrating -->
{:else if hasTenant === false}
	<div class="no-tenant">
		<p>{m('portal.shell.noTenant')}</p>
	</div>
{:else if $page.url.pathname === '/portal/login' || $page.url.pathname === '/portal/change-password' || $page.url.pathname.startsWith('/portal/cards/')}
	<slot />
{:else if portalAuth.loggedIn && portalAuth.user && !portalAuth.user.must_change_password}
	<div class="portal-shell">
		<!-- WCAG 2.4.1 Bypass Blocks. -->
		<a href="#main-content" class="skip-link">{m('portal.shell.skipToMain')}</a>
		<header class="portal-header">
			<div class="brand">
				{#if portalBrand.mark.kind === 'logo'}
					<img class="brand-logo" src={portalBrand.logoUrl} alt={portalBrand.productName} />
				{:else if portalBrand.mark.kind === 'platform'}
					<!-- Same fork as the employee sidebar: FeohLedger's mark only beside
					     FeohLedger's own name (docs/decisions.md §171). -->
					<BrandMark size={28} />
				{/if}
				<div class="brand-text">
					<strong>{portalBrand.productName}</strong>
					<span class="vendor">{portalAuth.user.vendor_name}</span>
				</div>
			</div>
			<nav aria-label={m('portal.shell.nav')}>
				<a href="/portal" class:active={$page.url.pathname === '/portal'}
					>{m('portal.nav.overview')}</a
				>
				<a href="/portal/invoices" class:active={$page.url.pathname.startsWith('/portal/invoices')}
					>{m('portal.nav.invoices')}</a
				>
				<a
					href="/portal/purchase-orders"
					class:active={$page.url.pathname.startsWith('/portal/purchase-orders')}
					>{m('portal.nav.purchaseOrders')}</a
				>
				<a href="/portal/payments" class:active={$page.url.pathname.startsWith('/portal/payments')}
					>{m('portal.nav.payments')}</a
				>
				<a
					href="/portal/discount-offers"
					class:active={$page.url.pathname.startsWith('/portal/discount-offers')}
					>{m('portal.nav.discounts')}</a
				>
				<a href="/portal/company" class:active={$page.url.pathname.startsWith('/portal/company')}
					>{m('portal.nav.company')}</a
				>
				<a
					href="/portal/notifications"
					class:active={$page.url.pathname.startsWith('/portal/notifications')}
					>{m('portal.nav.notifications')}</a
				>
			</nav>
			<div class="user">
				<span>{portalAuth.user.full_name}</span>
				<button type="button" onclick={handleLogout}>{m('portal.shell.logOut')}</button>
			</div>
		</header>
		<main id="main-content" tabindex="-1" class="portal-main">
			<slot />
		</main>
		{#if portalBrand.supportUrl || portalBrand.legalUrl}
			<!-- A stuck vendor has no AP login and no colleague to ask — the
			     branding fetch (GET /api/portal/branding) already carries the
			     tenant's support/legal URLs, but nothing rendered them, leaving
			     no "who do I contact" affordance anywhere in the portal
			     (persona-supplier audit finding, issue #328). Fail-soft like the
			     rest of white-labeling: an empty URL renders nothing, never a
			     dead link. -->
			<footer class="portal-footer">
				{#if portalBrand.supportUrl}
					<a href={portalBrand.supportUrl} target="_blank" rel="noopener noreferrer">
						{m('portal.shell.footerSupport')}
					</a>
				{/if}
				{#if portalBrand.legalUrl}
					<a href={portalBrand.legalUrl} target="_blank" rel="noopener noreferrer">
						{m('portal.shell.footerLegal')}
					</a>
				{/if}
			</footer>
		{/if}
	</div>
{/if}

<style>
	.no-tenant {
		min-height: 100vh;
		display: grid;
		place-items: center;
		color: var(--text-muted);
	}

	.portal-shell {
		min-height: 100vh;
		display: flex;
		flex-direction: column;
	}

	.portal-header {
		display: flex;
		align-items: center;
		gap: 24px;
		padding: 12px 24px;
		background: var(--surface);
		border-bottom: 1px solid var(--border);
	}

	.brand {
		display: flex;
		align-items: center;
		gap: 10px;
		line-height: 1.2;
	}

	.brand-logo {
		height: 28px;
		width: auto;
		max-width: 140px;
		object-fit: contain;
	}

	.brand-text {
		display: flex;
		flex-direction: column;
		line-height: 1.2;
	}

	.brand strong {
		font-size: 0.95rem;
	}

	.vendor {
		font-size: 0.75rem;
		color: var(--text-muted);
	}

	nav {
		display: flex;
		gap: 16px;
		flex: 1;
		margin-left: 24px;
	}

	nav a {
		color: var(--text-muted);
		text-decoration: none;
		padding: 6px 10px;
		border-radius: 4px;
		font-size: 0.9rem;
	}

	nav a.active,
	nav a:hover {
		color: var(--text);
		background: var(--bg);
	}

	.user {
		display: flex;
		align-items: center;
		gap: 12px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.user button {
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 5px 10px;
		color: var(--text);
		font-size: 0.8rem;
		cursor: pointer;
	}

	.portal-main {
		flex: 1;
		padding: 24px;
	}

	.portal-footer {
		display: flex;
		justify-content: center;
		gap: 20px;
		padding: 14px 24px;
		border-top: 1px solid var(--border);
		font-size: 0.8rem;
	}

	.portal-footer a {
		color: var(--text-muted);
		text-decoration: none;
	}

	.portal-footer a:hover {
		color: var(--text);
		text-decoration: underline;
	}
</style>
