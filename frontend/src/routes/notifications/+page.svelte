<script lang="ts">
	import { notificationStore } from '#lib/stores/notifications.svelte.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { untrack } from 'svelte';
	import { page as pageStore } from '$app/state';
	import { afterNavigate, goto, replaceState } from '$app/navigation';
	import { formatDate, timeAgo } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { EVENT_LABEL_KEYS } from '#lib/types/notification.ts';
	import type { Notification } from '#lib/types/notification.ts';

	// The relative "When" cell carries the exact instant in its title, in the
	// active locale — never the raw ISO string.
	const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
		month: 'short',
		day: 'numeric',
		year: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	};

	type Filter = 'all' | 'unread';

	/**
	 * The one READER of `?filter=` (the counterpart of `syncUrl()`), clamped to
	 * the chips' own keys so a hand-edited value cannot leave no chip pressed.
	 * Used at mount and by `afterNavigate` for a navigation that reuses this
	 * page (Back/Forward between two filtered views).
	 */
	function readUrlFilter(params: Pick<URLSearchParams, 'get'>): Filter {
		return params.get('filter') === 'unread' ? 'unread' : 'all';
	}

	let filter = $state<Filter>(readUrlFilter(pageStore.url.searchParams));
	let errored = $state(false);
	let initialLoaded = $state(false);

	let items = $derived(notificationStore.items);
	// Two different numbers from two different fields: the chips tally the WHOLE
	// inbox (they have to — an "All" chip that changed when you filtered would
	// be meaningless), while the footer counts only the set the active filter
	// selects. Reading one field for both made the All chip echo the unread
	// count whenever the Unread filter was on.
	let inboxTotal = $derived(notificationStore.inboxTotal);
	let filteredTotal = $derived(notificationStore.filteredTotal);
	let unread = $derived(notificationStore.unread);
	let loading = $derived(notificationStore.loading);
	let hasMore = $derived(notificationStore.hasMore);

	/** The one WRITER of this route's query string. Untracked: it is called from
	 *  the filter effect and must never become one of its dependencies. */
	function syncUrl() {
		untrack(() => {
			const qs = filter === 'unread' ? '?filter=unread' : '';
			replaceState(`${pageStore.url.pathname}${qs}`, {});
		});
	}

	$effect(() => {
		// Re-fetch whenever the filter changes, and keep the address bar on the
		// view so reload / Back / a shared link land on the same filter.
		filter;
		syncUrl();
		void reload();
	});

	afterNavigate(({ from, to, type }) => {
		if (type === 'enter' || !from || !to || from.route.id !== to.route.id) return;
		const next = readUrlFilter(to.url.searchParams);
		if (next !== filter) filter = next;
	});

	async function reload() {
		errored = false;
		try {
			await notificationStore.fetchList({ unreadOnly: filter === 'unread' });
		} catch {
			errored = true;
		} finally {
			initialLoaded = true;
		}
	}

	async function loadMore() {
		try {
			await notificationStore.loadMore({ unreadOnly: filter === 'unread' });
		} catch {
			toast(m('notifications.loadMoreFailed'), 'error');
		}
	}

	async function open(n: Notification) {
		// Mark read first so the badge + row update, then navigate to the invoice.
		try {
			if (!n.read_at) await notificationStore.markRead(n.id);
		} catch {
			/* navigation should still happen even if mark-read failed */
		}
		if (n.entity_type === 'invoice' && n.entity_id) {
			goto(`/invoices?id=${n.entity_id}`);
		}
	}

	async function markAll() {
		try {
			const updated = await notificationStore.markAllRead({ unreadOnly: filter === 'unread' });
			toast(
				updated > 0
					? m('notifications.markedRead', { n: updated })
					: m('notifications.nothingToMark'),
				'success'
			);
		} catch {
			toast(m('notifications.markAllFailed'), 'error');
		}
	}

	let chips = $derived([
		{ key: 'all', label: m('notifications.filter.all'), count: inboxTotal },
		{ key: 'unread', label: m('notifications.filter.unread'), count: unread },
	]);

	let COLUMNS = $derived([
		{ label: m('notifications.col.notification') },
		{ label: m('notifications.col.when') },
	]);

	let emptyMessage = $derived(
		filter === 'unread' ? m('notifications.empty.unread') : m('notifications.empty.all')
	);
</script>

<PageHeader title={m('notifications.title')}>
	{#snippet actions()}
		<button class="btn-mark-all" onclick={markAll} disabled={unread === 0}>
			{m('notifications.markAllRead')}
		</button>
	{/snippet}

	<FilterChips {chips} bind:active={filter} />

	{#if errored}
		<!-- A failed load is not an empty inbox: say so in place, with a way back. -->
		<LoadError message={m('notifications.empty.errored')} onretry={reload} testId="notifications-error" />
	{:else if !initialLoaded && loading}
		<p class="state-msg">{m('common.loading')}</p>
	{:else}
		<DataTable columns={COLUMNS} isEmpty={items.length === 0} empty={emptyMessage} colspan={2}>
			{#snippet body()}
				{#each items as n (n.id)}
					<tr
						class="clickable"
						class:unread-row={!n.read_at}
						onclick={(e) => { if (isRowOpenClick(e)) open(n); }}
					>
						<td>
							<div class="notif-cell">
								<span class="notif-dot" class:on={!n.read_at} aria-hidden="true"></span>
								<div class="notif-text">
									<RowLink onclick={() => open(n)} ariaLabel={m('notifications.open', { title: n.title })}>
										<span class="notif-title">{n.title}</span>
									</RowLink>
									{#if n.body}<span class="notif-body">{n.body}</span>{/if}
									<span class="notif-event">
										{EVENT_LABEL_KEYS[n.event_type] ? m(EVENT_LABEL_KEYS[n.event_type]) : n.event_type}
									</span>
								</div>
							</div>
						</td>
						<td class="muted-cell" title={formatDate(n.created_at, '', DATETIME_OPTS)}>{timeAgo(n.created_at)}</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>

		{#if hasMore}
			<div class="load-more-row">
				<button class="btn-load-more" onclick={loadMore} disabled={loading}>
					{loading
						? m('common.loading')
						: m('notifications.loadMore', { shown: items.length, total: filteredTotal })}
				</button>
			</div>
		{:else if filteredTotal > 0}
			<div class="load-more-row">
				<span class="load-more-end">{m('notifications.showingAll', { total: filteredTotal })}</span>
			</div>
		{/if}
	{/if}
</PageHeader>

<style>
	.btn-mark-all {
		padding: 8px 14px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-mark-all:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-mark-all:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.state-msg {
		color: var(--text-muted);
		padding: 12px 4px;
	}

	tbody tr.unread-row td {
		background: color-mix(in srgb, var(--accent) 5%, transparent);
	}

	.notif-cell {
		display: flex;
		align-items: flex-start;
		gap: 10px;
	}

	.notif-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		margin-top: 6px;
		flex-shrink: 0;
		background: transparent;
	}

	.notif-dot.on {
		background: var(--accent);
	}

	.notif-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.notif-title {
		font-weight: 600;
		color: var(--text);
	}

	.notif-body {
		font-size: 0.84rem;
		color: var(--text-muted);
	}

	.notif-event {
		font-size: 0.7rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.muted-cell {
		color: var(--text-muted);
		white-space: nowrap;
	}
</style>
