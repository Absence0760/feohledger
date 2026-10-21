<script lang="ts">
	import { tick } from 'svelte';
	import { goto } from '$app/navigation';
	import { notificationStore } from '#lib/stores/notifications.svelte.ts';
	import { timeAgo } from '#lib/utils/time.ts';
	import { EVENT_LABEL_KEYS } from '#lib/types/notification.ts';
	import type { Notification } from '#lib/types/notification.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { collapsed = false }: { collapsed?: boolean } = $props();

	const headingId = 'bell-popover-title';

	let open = $state(false);
	let loading = $state(false);
	// The last list load failed. Kept apart from "no rows": a failed fetch
	// used to read "No notifications yet." (#635).
	let errored = $state(false);
	let triggerBtn = $state<HTMLButtonElement | null>(null);
	let heading = $state<HTMLElement | null>(null);

	let unread = $derived(notificationStore.unread);
	let badgeLabel = $derived(unread > 99 ? '99+' : String(unread));
	// Newest first; the popover is a peek, not the archive — cap at 6.
	let recent = $derived(notificationStore.items.slice(0, 6));

	function eventLabel(type: string): string {
		const key = EVENT_LABEL_KEYS[type as keyof typeof EVENT_LABEL_KEYS];
		return key ? m(key) : type;
	}

	async function load() {
		loading = true;
		errored = false;
		try {
			await notificationStore.fetchList();
		} catch {
			errored = true;
		} finally {
			loading = false;
		}
	}

	async function toggle() {
		if (open) {
			close();
			return;
		}
		open = true;
		// Focus moves INTO the dialog on open, onto its heading — the one
		// element that exists before the list has loaded — and back to the
		// bell on close (WCAG 2.4.3).
		await tick();
		heading?.focus();
		await load();
	}

	function close() {
		open = false;
		triggerBtn?.focus();
	}

	async function retry() {
		// The Retry button unmounts while the list loads; keep focus inside.
		heading?.focus();
		await load();
	}

	async function openItem(n: Notification) {
		// Through `close()`, so focus is back on the bell before any navigation —
		// a non-invoice notification navigates nowhere, and the clicked button
		// is about to unmount.
		close();
		try {
			if (!n.read_at) await notificationStore.markRead(n.id);
		} catch {
			/* navigate regardless — mark-read is best-effort */
		}
		if (n.entity_type === 'invoice' && n.entity_id) {
			goto(`/invoices?id=${n.entity_id}`);
		}
	}

	async function markAll() {
		try {
			await notificationStore.markAllRead();
		} catch {
			/* non-critical */
		}
	}

	// A non-modal popover: when focus leaves the bell + popover pair, it
	// closes rather than staying open behind the reader (WCAG 2.4.3).
	function onWrapperFocusOut(e: FocusEvent) {
		const next = e.relatedTarget as Node | null;
		if (!open || !next) return;
		if ((e.currentTarget as HTMLElement).contains(next)) return;
		open = false;
	}

	// Esc closes + restores focus to the bell, matching the entity/profile menus.
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) close();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#snippet loadError()}
	<div class="bell-error" role="alert" data-testid="bell-error">
		<p>{m('notifications.empty.errored')}</p>
		<button type="button" class="bell-retry" onclick={retry}>{m('common.retry')}</button>
	</div>
{/snippet}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="bell-wrapper" class:collapsed onfocusout={onWrapperFocusOut}>
	{#if open}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="bell-backdrop" onclick={close} onkeydown={() => {}}></div>
		<div class="bell-popover" role="dialog" aria-labelledby={headingId} data-testid="bell-popover">
			<div class="bell-head">
				<h2 class="bell-title" id={headingId} tabindex="-1" bind:this={heading}>
					{m('notifications.title')}
				</h2>
				<button class="bell-mark" onclick={markAll} disabled={unread === 0}>
					{m('notifications.markAllRead')}
				</button>
			</div>
			<div class="bell-list">
				{#if loading && recent.length === 0}
					<p class="bell-empty" role="status">{m('common.loading')}</p>
				{:else if errored && recent.length === 0}
					{@render loadError()}
				{:else if recent.length === 0}
					<p class="bell-empty">{m('notifications.empty.all')}</p>
				{:else}
					<!-- A failed refresh over rows from an earlier load: the rows
					     stay, but they are not current, and the popover says so. -->
					{#if errored}{@render loadError()}{/if}
					{#each recent as n (n.id)}
						<button class="bell-item" class:unread={!n.read_at} onclick={() => openItem(n)}>
							<span class="bell-dot" class:on={!n.read_at} aria-hidden="true"></span>
							<span class="bell-item-text">
								<span class="bell-item-title">{n.title}</span>
								{#if n.body}<span class="bell-item-body">{n.body}</span>{/if}
								<span class="bell-item-meta">
									{eventLabel(n.event_type)} · {timeAgo(n.created_at)}
								</span>
							</span>
						</button>
					{/each}
				{/if}
			</div>
			<a class="bell-viewall" href="/notifications" onclick={() => (open = false)}>
				{m('notifications.bell.viewAll')}<span aria-hidden="true"> →</span>
			</a>
		</div>
	{/if}

	<button
		bind:this={triggerBtn}
		class="bell-btn"
		title={m('notifications.title')}
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-label={unread > 0
			? m('notifications.bell.ariaUnread', { count: badgeLabel })
			: m('notifications.title')}
		onclick={toggle}
	>
		<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
		{#if unread > 0}
			<span class="bell-badge" class:dot={collapsed}>{collapsed ? '' : badgeLabel}</span>
		{/if}
	</button>
</div>

<style>
	.bell-wrapper {
		position: relative;
		flex-shrink: 0;
	}

	.bell-btn {
		position: relative;
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 6px;
		border: none;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		transition: all 0.12s;
	}

	.bell-btn:hover {
		background: color-mix(in srgb, var(--accent) 8%, transparent);
		color: var(--text);
	}

	.bell-badge {
		position: absolute;
		top: 0;
		right: 0;
		min-width: 16px;
		height: 16px;
		padding: 0 4px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 8px;
		background: var(--danger-strong);
		color: #fff;
		font-size: 0.62rem;
		font-weight: 700;
		line-height: 1;
		border: 1px solid var(--surface);
	}

	/* Collapsed sidebar — a bare dot, no count. */
	.bell-badge.dot {
		min-width: 8px;
		width: 8px;
		height: 8px;
		padding: 0;
		top: 2px;
		right: 2px;
	}

	.bell-backdrop {
		position: fixed;
		inset: 0;
		z-index: 60;
	}

	.bell-popover {
		position: absolute;
		top: calc(100% + 8px);
		left: 0;
		width: 320px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		z-index: 61;
		display: flex;
		flex-direction: column;
		overflow: hidden;
	}

	/* Collapsed rail is only 60px wide — push the popover clear of it. */
	.bell-wrapper.collapsed .bell-popover {
		left: calc(100% + 8px);
		top: 0;
	}

	.bell-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 10px 12px;
		border-bottom: 1px solid var(--border);
	}

	.bell-title {
		margin: 0;
		font-size: 0.85rem;
		font-weight: 600;
		color: var(--text);
	}

	.bell-mark {
		border: none;
		background: none;
		color: var(--text-muted);
		font-size: 0.76rem;
		cursor: pointer;
		font-family: inherit;
	}

	.bell-mark:hover:not(:disabled) {
		color: var(--accent);
	}

	.bell-mark:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.bell-list {
		max-height: 360px;
		overflow-y: auto;
	}

	.bell-error {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 10px;
		padding: 20px 12px;
		text-align: center;
		color: var(--text-muted);
		font-size: 0.84rem;
	}

	.bell-error p {
		margin: 0;
	}

	.bell-retry {
		min-height: 24px;
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: none;
		color: var(--text);
		font: inherit;
		cursor: pointer;
	}

	.bell-retry:hover {
		border-color: var(--accent);
	}

	.bell-empty {
		padding: 24px 12px;
		text-align: center;
		color: var(--text-muted);
		font-size: 0.84rem;
	}

	.bell-item {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		width: 100%;
		padding: 10px 12px;
		border: none;
		border-bottom: 1px solid var(--border);
		background: none;
		text-align: left;
		cursor: pointer;
		font-family: inherit;
	}

	.bell-item:last-child {
		border-bottom: none;
	}

	.bell-item:hover {
		background: color-mix(in srgb, var(--accent) 8%, transparent);
	}

	.bell-item.unread {
		background: color-mix(in srgb, var(--accent) 5%, transparent);
	}

	.bell-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		margin-top: 5px;
		flex-shrink: 0;
		background: transparent;
	}

	.bell-dot.on {
		background: var(--accent);
	}

	.bell-item-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.bell-item-title {
		font-size: 0.84rem;
		font-weight: 600;
		color: var(--text);
	}

	.bell-item-body {
		font-size: 0.78rem;
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.bell-item-meta {
		font-size: 0.68rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.bell-viewall {
		display: block;
		padding: 10px 12px;
		text-decoration: none;
		border: none;
		border-top: 1px solid var(--border);
		background: none;
		color: var(--accent);
		font-size: 0.82rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
		text-align: center;
	}

	.bell-viewall:hover {
		background: color-mix(in srgb, var(--accent) 8%, transparent);
	}
</style>
