<script lang="ts">
	import { page } from '$app/state';
	import { sidebar } from '$lib/stores/sidebar.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { brand } from '$lib/stores/brand.svelte';
	import { entityStore } from '$lib/stores/entity.svelte';
	import EntitySwitcher from '$lib/components/layout/EntitySwitcher.svelte';
	import NotificationBell from '$lib/components/layout/NotificationBell.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import { NAV, groupHref, isEntryActive, isEntryVisible, type NavEntry } from '$lib/nav';
	import { m } from '$lib/i18n/store.svelte';

	let collapsed = $derived(sidebar.collapsed);
	let mark = $derived(brand.mark);

	// Load the tenant's entities once so the switcher can render (it hides
	// itself for single-entity tenants — see EntitySwitcher).
	$effect(() => {
		entityStore.ensureLoaded();
	});
	let showProfile = $state(false);
	let profileBtn = $state<HTMLButtonElement | null>(null);

	// Esc closes the profile popover and returns focus to its trigger, matching
	// the backdrop-click dismissal. Without this the only way out is a click —
	// a keyboard user who opened the menu was stranded in it.
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showProfile) {
			showProfile = false;
			profileBtn?.focus();
		}
	}

	// Role/permission checks bound to the auth store; reads reactive state so
	// the visible-entry list recomputes if the user's roles or permissions
	// change.
	const has = (...roles: string[]) => auth.hasAnyRole(...roles);
	const can = (perm: string) => auth.can(perm);

	// The primary nav (`$lib/nav`) is the single source of truth, shared with
	// the per-page section sub-tab bar. High-traffic destinations are top-level
	// links; the rest are folded into groups (Procurement / Billing / Insights /
	// Settings) that open a sub-tabbed page. Filter to what this role/permission
	// set can see.
	let entries = $derived(NAV.filter((e) => isEntryVisible(e, has, can)));
	let pathname = $derived(page.url.pathname);

	// A group's row navigates to its first accessible child (links use their own
	// href). `entries` already dropped groups with no visible child, so the
	// fallback is unreachable.
	function hrefFor(entry: NavEntry): string {
		return entry.kind === 'link' ? entry.href : (groupHref(entry, has, can) ?? '#');
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<aside class="sidebar" class:collapsed>
	<div class="sidebar-header" class:collapsed>
		<div class="logo">
			{#if mark.kind === 'logo'}
				<!-- White-label logo; alt is the (branded) product name. -->
				<img class="logo-img" src={mark.src} alt={brand.productName} />
			{:else if mark.kind === 'platform'}
				<!-- Named only when it stands alone; expanded, the product name follows. -->
				<BrandMark size={24} label={collapsed ? brand.productName : ''} />
			{:else}
				<!-- Renamed without a logo: their initial, never FeohLedger's mark beside
				     someone else's product name (docs/decisions.md §171). -->
				<span
					class="logo-monogram"
					role={collapsed ? 'img' : undefined}
					aria-label={collapsed ? brand.productName : undefined}
					aria-hidden={collapsed ? undefined : 'true'}>{mark.letter}</span
				>
			{/if}
			{#if !collapsed}<span class="logo-text">{brand.productName}</span>{/if}
		</div>
		<NotificationBell {collapsed} />
	</div>

	<EntitySwitcher {collapsed} />

	<!-- WCAG 1.3.1 / 4.1.2: name the landmark so it's distinguishable from the
	     section sub-tab nav and the portal nav. -->
	<nav class="nav-main" aria-label={m('shell.primaryNav')}>
		{#each entries as entry, i (entry.label)}
			{#if entry.kind === 'group' && entries[i - 1]?.kind === 'link'}
				<!-- Divider sets the folded section areas off from the direct links. -->
				<div class="nav-group-divider"></div>
			{/if}
			<a
				href={hrefFor(entry)}
				class="nav-item"
				class:active={isEntryActive(entry, pathname)}
				title={collapsed ? m(entry.labelKey) : ''}
			>
				<span class="nav-icon">
					{#if entry.icon === 'dashboard'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
					{:else if entry.icon === 'invoices'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
					{:else if entry.icon === 'payments'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
					{:else if entry.icon === 'vendors'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>
					{:else if entry.icon === 'exceptions'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
					{:else if entry.icon === 'cart'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg>
					{:else if entry.icon === 'receipt'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 2v20l2-1.5L8 22l2-1.5L12 22l2-1.5L16 22l2-1.5L20 22V2l-2 1.5L16 2l-2 1.5L12 2l-2 1.5L8 2 6 3.5 4 2z"/><line x1="8" y1="8" x2="16" y2="8"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
					{:else if entry.icon === 'assistant'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
					{:else if entry.icon === 'settings'}
						<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
					{/if}
				</span>
				{#if !collapsed}
					<span class="nav-label">{m(entry.labelKey)}</span>
				{/if}
			</a>
		{/each}
	</nav>

	<div class="profile-wrapper">
		{#if showProfile}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="profile-backdrop" onclick={() => (showProfile = false)} onkeydown={() => {}}></div>
			<div class="profile-popover">
				<div class="profile-info">
					<div class="profile-name">{auth.user?.full_name ?? '—'}</div>
					<div class="profile-email">{auth.user?.email ?? '—'}</div>
				</div>
				<a class="profile-action" href="/profile" onclick={() => (showProfile = false)}>
					{m('shell.profileAndSecurity')}
				</a>
				<button class="profile-logout" onclick={() => auth.logout()}>{m('shell.logOut')}</button>
			</div>
		{/if}
		<button
			bind:this={profileBtn}
			class="profile-btn"
			class:collapsed
			title={collapsed ? m('shell.profile') : ''}
			aria-label={m('shell.profileMenu')}
			aria-haspopup="menu"
			aria-expanded={showProfile}
			onclick={() => (showProfile = !showProfile)}
		>
			<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
			{#if !collapsed}
				<span class="profile-label">{auth.user?.full_name ?? m('shell.profile')}</span>
			{/if}
		</button>
	</div>

	<button class="collapse-btn" class:collapsed onclick={() => sidebar.toggle()} aria-label={collapsed ? m('shell.expandSidebar') : m('shell.collapseSidebar')}>
		<span class="nav-icon">
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class:flipped={collapsed}>
				<polyline points="15 18 9 12 15 6" />
			</svg>
		</span>
		{#if !collapsed}
			<span class="collapse-label">{m('shell.collapse')}</span>
		{/if}
	</button>
</aside>

<style>
	.sidebar {
		position: fixed;
		top: 0;
		left: 0;
		bottom: 0;
		width: 220px;
		background: var(--surface);
		border-right: 1px solid var(--border);
		display: flex;
		flex-direction: column;
		padding: 12px 8px;
		z-index: 50;
		transition: width 0.2s ease;
	}

	.sidebar.collapsed {
		width: 60px;
	}

	.sidebar-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 4px;
		padding: 8px 4px 20px 8px;
	}

	/* Collapsed rail (60px) — stack the mark over the bell so both fit. */
	.sidebar-header.collapsed {
		flex-direction: column;
		gap: 12px;
		padding: 8px 0 16px;
	}

	.logo {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
	}

	/* A renamed tenant's initial, on the mark's 24px footprint and the tenant's
	   own accent. --accent-strong's one contract is that white text sits on it. */
	.logo-monogram {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		border-radius: 6px;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.8rem;
		font-weight: 800;
		flex-shrink: 0;
	}

	/* White-label logo image — capped to the rail height so a tall logo can't
	   blow out the header; object-fit keeps the aspect ratio. */
	.logo-img {
		height: 24px;
		max-width: 130px;
		object-fit: contain;
		flex-shrink: 0;
	}

	/* Sized to clear the header bell within the 220px rail without clipping. */
	.logo-text {
		font-size: 0.88rem;
		font-weight: 600;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.nav-main {
		display: flex;
		flex-direction: column;
		gap: 2px;
		/* Absorb the free vertical space and scroll internally when the nav
		 * list is taller than the viewport (many groups + a long-tail of
		 * role-gated items). Keeps the profile + collapse footer pinned and
		 * always reachable — without this the list overflows and pushes the
		 * Log Out button off-screen on short viewports. `min-height: 0` lets
		 * a flex child shrink below its content height so overflow engages. */
		flex: 1 1 auto;
		min-height: 0;
		overflow-y: auto;
	}

	.profile-wrapper {
		position: relative;
		margin-bottom: 4px;
	}

	.profile-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 9px 10px;
		border-radius: 6px;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		font-family: inherit;
		transition: all 0.12s;
	}

	.profile-btn.collapsed {
		justify-content: center;
		padding: 9px 0;
	}

	.profile-btn:hover {
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	.profile-label {
		font-size: 0.88rem;
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.profile-backdrop {
		position: fixed;
		inset: 0;
		z-index: 60;
	}

	.profile-popover {
		position: absolute;
		bottom: 100%;
		left: 8px;
		margin-bottom: 8px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		padding: 12px;
		min-width: 200px;
		z-index: 61;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.profile-info {
		padding-bottom: 10px;
		border-bottom: 1px solid var(--border);
		margin-bottom: 10px;
	}

	.profile-name {
		font-size: 0.88rem;
		font-weight: 600;
		color: var(--text);
	}

	.profile-email {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin-top: 2px;
	}

	.profile-logout {
		width: 100%;
		padding: 7px 12px;
		border-radius: 5px;
		border: 1px solid var(--danger);
		background: rgba(224, 64, 64, 0.1);
		color: var(--danger);
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		transition: background 0.15s;
	}

	.profile-logout:hover {
		background: rgba(224, 64, 64, 0.2);
	}

	.profile-action {
		display: block;
		box-sizing: border-box;
		width: 100%;
		padding: 7px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		font-weight: 500;
		text-align: center;
		text-decoration: none;
		cursor: pointer;
		font-family: inherit;
	}

	.profile-action:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.nav-group-divider {
		height: 1px;
		background: var(--border);
		margin: 8px 10px;
		opacity: 0.5;
	}

	.nav-item {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 9px 10px;
		border-radius: 6px;
		color: var(--text-muted);
		text-decoration: none;
		font-size: 0.88rem;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		transition: all 0.12s;
	}

	.nav-item:hover {
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	.nav-item.active {
		background: rgba(99, 140, 255, 0.12);
		color: var(--accent);
	}

	.nav-icon {
		display: grid;
		place-items: center;
		flex-shrink: 0;
		width: 20px;
		height: 20px;
		position: relative;
	}

	.nav-label {
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.collapse-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		margin-top: 8px;
		padding: 9px 10px;
		border-radius: 6px;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		font-family: inherit;
		transition: all 0.12s;
	}

	.collapse-btn.collapsed {
		justify-content: center;
		padding: 9px 0;
	}

	.collapse-btn:hover {
		background: rgba(99, 140, 255, 0.08);
		color: var(--text);
	}

	.collapse-label {
		font-size: 0.88rem;
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.collapse-btn svg {
		transition: transform 0.2s ease;
	}

	.collapse-btn svg.flipped {
		transform: rotate(180deg);
	}
</style>
