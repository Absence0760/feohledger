<script lang="ts">
	import '../app.css';
	import Landing from '$lib/components/marketing/Landing.svelte';
	import Sidebar from '$lib/components/layout/Sidebar.svelte';
	import SectionTabs from '$lib/components/layout/SectionTabs.svelte';
	import Toast from '$lib/components/ui/Toast.svelte';
	import ConsentBanner from '$lib/components/ConsentBanner.svelte';
	import { sidebar } from '$lib/stores/sidebar.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { brand } from '$lib/stores/brand.svelte';
	import { notificationStore } from '$lib/stores/notifications.svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { hasTenantContext } from '$lib/tenant';
	import { browser } from '$app/environment';
	import { initLocale, m } from '$lib/i18n/store.svelte';

	// Tri-state: `undefined` until the browser resolves it (render nothing, no
	// flash), then true/false. This asks "does this host carry a tenant at all",
	// NOT "what is its slug" — a white-label vanity host has a tenant but no
	// slug in the URL (the backend resolves it from `Host`), so gating on the
	// slug rendered the marketing Landing page to a customer on their own
	// domain. The slug itself is never needed here; `$lib/api` attaches it.
	let hasTenant = $state<boolean | undefined>(undefined);

	// Detect + apply the visitor's UI language once on first client mount
	// (stored choice → navigator.languages → English). This also sets the
	// <html lang/dir> attributes and the active Intl format locale.
	$effect(() => {
		if (browser) initLocale();
	});

	// Routes that render without a tenant context (signup flow).
	const PUBLIC_PATHS = ['/signup', '/verify'];

	// The published legal documents (/legal, /legal/privacy, …). Public by
	// design and by necessity: a data subject exercising a GDPR right, a
	// procurement reviewer reading the DPA, and a supplier checking what we do
	// with their bank details all arrive without an account, and often on the
	// apex domain where there is no tenant at all. A prefix, not a
	// PUBLIC_PATHS entry, because that list is matched exactly and these are a
	// subtree.
	const LEGAL_PREFIX = '/legal';

	// The supplier portal runs on the tenant subdomain but uses a separate
	// auth surface (VendorUser, not User). Bypass the root-layout's
	// employee-auth logic for any /portal path — `/portal/+layout.svelte`
	// handles portal-specific routing.
	const PORTAL_PREFIX = '/portal';

	$effect(() => {
		if (browser) {
			hasTenant = hasTenantContext();
		}
	});

	$effect(() => {
		if (!hasTenant) return;

		const path = $page.url.pathname;

		// Portal has its own auth tree — don't interleave the two.
		if (path.startsWith(PORTAL_PREFIX)) return;

		// Never bounce a legal page to /login. On a tenant subdomain hasTenant
		// is true, so without this an anonymous reader following a link to the
		// privacy policy would land on a sign-in form instead — which for a
		// data subject with no account is a dead end, not a detour.
		if (path.startsWith(LEGAL_PREFIX)) return;

		if (!auth.loggedIn && !path.startsWith('/login')) {
			goto('/login');
			return;
		}

		if (auth.loggedIn && !auth.user) {
			auth.fetchUser();
			return;
		}

		if (
			auth.loggedIn &&
			auth.user?.must_change_password &&
			path !== '/change-password'
		) {
			goto('/change-password');
		}
	});

	// WCAG 1.4.10 Reflow: on narrow viewports collapse the sidebar to its icon
	// rail so the 220px panel doesn't force the page wider than the screen. Nav
	// stays reachable (icon rail), the user can still expand it. Desktop is
	// unaffected — the breakpoint only fires below 700px.
	$effect(() => {
		if (!browser) return;
		const mq = window.matchMedia('(max-width: 700px)');
		const apply = () => {
			if (mq.matches && !sidebar.collapsed) sidebar.toggle();
		};
		apply();
		mq.addEventListener('change', apply);
		return () => mq.removeEventListener('change', apply);
	});

	// White-label theming: once the user is fully signed into a tenant, load the
	// org's brand config and apply its accent colors as CSS custom properties on
	// <html>. Only the org-configured tokens are written — an unset accent leaves
	// the AA-passing app.css default in place (see brand.svelte.ts). The branding
	// read needs auth, so it's gated on the same signed-in condition as the bell.
	$effect(() => {
		const active =
			hasTenant === true &&
			auth.loggedIn &&
			!!auth.user &&
			!auth.user.must_change_password &&
			!$page.url.pathname.startsWith(PORTAL_PREFIX);
		if (active) {
			brand.ensureLoadedAndApply();
		}
	});

	// Drive the sidebar's unread-notification badge. Start the 60s poll once the
	// user is fully signed in (and past the change-password gate); stop it on
	// logout so a stale timer doesn't fire 401s after the token is cleared.
	$effect(() => {
		const active =
			hasTenant === true &&
			auth.loggedIn &&
			!!auth.user &&
			!auth.user.must_change_password &&
			!$page.url.pathname.startsWith(PORTAL_PREFIX);
		if (active) {
			notificationStore.startPolling();
		} else {
			notificationStore.stopPolling();
		}
	});
</script>

<svelte:head>
	<title>{brand.productName}</title>
</svelte:head>

{#if $page.url.pathname.startsWith(LEGAL_PREFIX)}
	<!--
		Legal documents render standalone and first — ahead of the tenant probe,
		because they are identical on the apex and on every tenant subdomain and
		need no tenant to be resolved. Gating them on `hasTenant` would blank
		them for a beat on load, and on the apex would hand the reader the
		marketing Landing page instead of the document they asked for.
	-->
	<slot />
{:else if hasTenant === undefined}
	<!-- SSR / hydration: tenant not resolved yet, render nothing to avoid flash -->
{:else if $page.url.pathname.startsWith(PORTAL_PREFIX)}
	<slot />
{:else if PUBLIC_PATHS.includes($page.url.pathname)}
	<slot />
{:else if hasTenant === false}
	<Landing />
{:else if $page.url.pathname.startsWith('/login') || $page.url.pathname === '/change-password'}
	<slot />
{:else if auth.loggedIn && auth.user && !auth.user.must_change_password}
	<div class="app-shell">
		<!-- WCAG 2.4.1 Bypass Blocks: first focusable element jumps past the
		     sidebar nav straight to the page content. -->
		<a href="#main-content" class="skip-link">{m('shell.skipToMain')}</a>
		<Sidebar />
		<main
			id="main-content"
			tabindex="-1"
			class="main-content"
			style="margin-left: {sidebar.collapsed ? 60 : 220}px"
		>
			<SectionTabs />
			<slot />
		</main>
	</div>
{/if}

<Toast />

<!--
	Consent banner is mounted here, outside the routed `<slot />`, so it renders
	on every surface — the app shell, the no-tenant marketing landing, the
	signup/verify flow, and the supplier portal (whose `/portal/+layout.svelte`
	only owns the slot content; this root layout still wraps it). It governs
	non-essential/analytics storage only — essential JWT auth is exempt.
-->
<ConsentBanner />

<style>
	.app-shell {
		display: flex;
		min-height: 100vh;
		isolation: isolate;
	}
	/* The shell's ambient light: a faint wash of the tenant's accent from the
	   top of the page and a ruled grid that fades out below the header — the
	   same ledger ground the public pages stand on, held still. Fixed, and on a
	   pseudo-element, so it is composited once and never repaints on scroll;
	   `z-index: -1` inside the isolated shell keeps it under every page without
	   a page having to know it exists. No continuous motion, so the app shell
	   owes no WCAG 2.2.2 control. */
	.app-shell::before {
		content: '';
		position: fixed;
		inset: 0;
		z-index: -1;
		pointer-events: none;
		background:
			radial-gradient(1100px 480px at 72% -14%, var(--accent-wash), transparent 70%),
			linear-gradient(to right, rgba(226, 228, 234, 0.02) 1px, transparent 1px) 0 0 / 64px 64px,
			linear-gradient(to bottom, rgba(226, 228, 234, 0.02) 1px, transparent 1px) 0 0 / 64px 64px;
		mask-image: linear-gradient(to bottom, #000 0, #000 360px, transparent 720px);
	}

	.main-content {
		flex: 1;
		/* Without min-width: 0, a flex item's intrinsic minimum is its content
		   width — wide tables/grids would push the page wider than the
		   viewport, breaking the sidebar and clipping headers/buttons. */
		min-width: 0;
		transition: margin-left 0.2s ease;
	}
</style>
