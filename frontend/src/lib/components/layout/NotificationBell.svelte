<script lang="ts">
	import { goto } from '$app/navigation';
	import { notificationStore } from '#lib/stores/notifications.svelte.ts';
	import { timeAgo } from '#lib/utils/time.ts';
	import { EVENT_LABELS } from '#lib/types/notification.ts';
	import type { Notification } from '#lib/types/notification.ts';

	let { collapsed = false }: { collapsed?: boolean } = $props();

	let open = $state(false);
	let loading = $state(false);
	let triggerBtn = $state<HTMLButtonElement | null>(null);

	let unread = $derived(notificationStore.unread);
	let badgeLabel = $derived(unread > 99 ? '99+' : String(unread));
	// Newest first; the popover is a peek, not the archive — cap at 6.
	let recent = $derived(notificationStore.items.slice(0, 6));

	async function toggle() {
		open = !open;
		if (open) {
			loading = true;
			try {
				await notificationStore.fetchList();
			} catch {
				/* the popover degrades to an empty state; the page is the fallback */
			} finally {
				loading = false;
			}
		}
	}

	function close() {
		open = false;
	}

	async function openItem(n: Notification) {
		open = false;
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

	function viewAll() {
		open = false;
		goto('/notifications');
	}

	// Esc closes + restores focus to the bell, matching the entity/profile menus.
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			open = false;
			triggerBtn?.focus();
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="bell-wrapper" class:collapsed>
	{#if open}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="bell-backdrop" onclick={close} onkeydown={() => {}}></div>
		<div class="bell-popover" role="dialog" aria-label="Notifications">
			<div class="bell-head">
				<span class="bell-title">Notifications</span>
				<button class="bell-mark" onclick={markAll} disabled={unread === 0}>Mark all read</button>
			</div>
			<div class="bell-list">
				{#if loading && recent.length === 0}
					<p class="bell-empty">Loading…</p>
				{:else if recent.length === 0}
					<p class="bell-empty">No notifications yet.</p>
				{:else}
					{#each recent as n (n.id)}
						<button class="bell-item" class:unread={!n.read_at} onclick={() => openItem(n)}>
							<span class="bell-dot" class:on={!n.read_at} aria-hidden="true"></span>
							<span class="bell-item-text">
								<span class="bell-item-title">{n.title}</span>
								{#if n.body}<span class="bell-item-body">{n.body}</span>{/if}
								<span class="bell-item-meta">
									{EVENT_LABELS[n.event_type] ?? n.event_type} · {timeAgo(n.created_at)}
								</span>
							</span>
						</button>
					{/each}
				{/if}
			</div>
			<button class="bell-viewall" onclick={viewAll}>View all →</button>
		</div>
	{/if}

	<button
		bind:this={triggerBtn}
		class="bell-btn"
		title="Notifications"
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
		onclick={toggle}
	>
		<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
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
		background: rgba(99, 140, 255, 0.08);
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
		background: rgba(99, 140, 255, 0.08);
	}

	.bell-item.unread {
		background: rgba(99, 140, 255, 0.05);
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
		padding: 10px 12px;
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
		background: rgba(99, 140, 255, 0.08);
	}
</style>
