import { api } from '#lib/api.ts';
import { hasToken } from '#lib/api.ts';
import type {
	Notification,
	NotificationListResponse,
	NotificationPrefs,
} from '#lib/types/notification.ts';
import { appendUnique } from '#lib/utils/pagination.ts';
import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

const PAGE_SIZE = 20;
const POLL_INTERVAL_MS = 60_000;

function createNotificationStore() {
	let items = $state<Notification[]>([]);
	// Three counts, deliberately not one. The list envelope's `total` counts the
	// set the request was FILTERED to — with `unread_only=true` it IS the unread
	// count — so folding it into a single field made the All chip display the
	// unread tally whenever the Unread filter was active (both chips showing the
	// same number, one of them mislabelled), and left the footer claiming
	// "Showing all N" for a set no row matched after a mark-all.
	//
	//   filteredTotal — the server's count of the ACTIVE filter's set. Owns
	//                   `hasMore` and the footer's "Showing all N"
	//                   (`frontend/CLAUDE.md` § Pagination + Load more).
	//   inboxTotal    — the server's count of the WHOLE inbox, filter-
	//                   independent. Owns the All chip. Same shape `/vendors`
	//                   uses: whole-set chip tallies come from their own read,
	//                   never from the filtered page's envelope.
	//   unread        — the server's count of the whole inbox's UNREAD rows.
	//                   Owns the Unread chip and the sidebar badge; the envelope
	//                   already reports this one filter-independently, per
	//                   `backend/app/api/notifications.py::list_notifications`.
	let filteredTotal = $state(0);
	let inboxTotal = $state(0);
	let unread = $state(0);
	let page = $state(1);
	let loading = $state(false);
	let prefs = $state<NotificationPrefs | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	let hasMore = $derived(items.length < filteredTotal);

	// Three sequencers, deliberately not one. `listSequence` guards the list
	// `load()` (a filter flip or a load-more resolving out of order),
	// `countSequence` guards the 60s badge poll, and `inboxSequence` guards the
	// whole-inbox tally read. Sharing a single counter would let a poll tick
	// landing mid-load mark that load un-committable and blank the list — these
	// are independent reads of the same server state, not successive versions of
	// one request. `markRead`/`markAllRead` supersede the first two: each writes
	// `unread`, so either could restore the pre-edit count. `inboxSequence` is
	// left out of that supersede on purpose — it writes only `inboxTotal`, which
	// no local read-state edit changes (marking a row read doesn't remove it
	// from the inbox), so retiring it would drop a refresh for nothing. See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const listSequence = createRequestSequencer();
	const countSequence = createRequestSequencer();
	const inboxSequence = createRequestSequencer();

	async function load(opts: { append?: boolean; nextPage?: number; unreadOnly?: boolean } = {}) {
		const nextPage = opts.nextPage ?? 1;
		const token = listSequence.start();
		loading = true;
		try {
			const params = new URLSearchParams();
			params.set('page', String(nextPage));
			params.set('page_size', String(PAGE_SIZE));
			if (opts.unreadOnly) params.set('unread_only', 'true');
			const res = await api.get<NotificationListResponse>(`/api/notifications?${params}`);
			// Superseded by a newer load, or by a local mark-read.
			if (!listSequence.canCommit(token)) return;
			items = opts.append ? appendUnique(items, res.items) : res.items;
			filteredTotal = res.total;
			unread = res.unread;
			page = nextPage;
			// Refresh the whole-inbox tally the All chip shows. An UNFILTERED
			// response already IS that tally, so take it for free; a filtered
			// one is not, so pay for the dedicated read instead. Only on a
			// fresh load — a load-more changes neither total.
			if (!opts.append) {
				if (opts.unreadOnly) void refreshInboxTotal();
				else inboxTotal = res.total;
			}
		} finally {
			if (listSequence.isCurrentRequest(token)) loading = false;
		}
	}

	/** Read the whole-inbox count the All chip shows, independent of whichever
	 *  filter the list is currently on.
	 *
	 *  There is no `GET /api/notifications/counts` (the `/vendors` chips have
	 *  one), so this asks the list endpoint for the smallest possible
	 *  *unfiltered* page and keeps only its `total` — one row over the wire, and
	 *  only when the list itself is filtered. It deliberately does NOT write
	 *  `unread`: that field is owned by the list load and the badge poll, both
	 *  of which a local mark-read supersedes, and this read is not part of that
	 *  protocol. Non-fatal — on failure the chip keeps its last known tally,
	 *  which is stale at worst rather than wrong. */
	async function refreshInboxTotal() {
		const token = inboxSequence.start();
		try {
			const res = await api.get<NotificationListResponse>(
				`/api/notifications?page=1&page_size=1`
			);
			if (!inboxSequence.canCommit(token)) return;
			inboxTotal = res.total;
		} catch {
			/* chip keeps its last known value — a stale count beats a wrong one */
		}
	}

	async function fetchList(opts: { unreadOnly?: boolean } = {}) {
		await load({ unreadOnly: opts.unreadOnly });
	}

	async function loadMore(opts: { unreadOnly?: boolean } = {}) {
		await load({ append: true, nextPage: page + 1, unreadOnly: opts.unreadOnly });
	}

	async function fetchUnreadCount() {
		if (!hasToken()) return;
		const token = countSequence.start();
		try {
			const res = await api.get<{ unread: number }>('/api/notifications/unread-count');
			// A poll issued before a mark-read carries the pre-edit count and
			// would flip the badge back on. Discard it.
			if (!countSequence.canCommit(token)) return;
			unread = res.unread;
		} catch {
			/* badge is non-critical — ignore transient failures */
		}
	}

	/** Retire every in-flight read of server state — both the list load and the
	 *  badge poll carry a pre-edit `unread`, and the list load a pre-edit
	 *  `read_at`. Call immediately before applying a local read-state edit. */
	function supersedeReads() {
		listSequence.supersedeInFlight();
		countSequence.supersedeInFlight();
	}

	async function markRead(id: string) {
		await api.post(`/api/notifications/${id}/read`, {});
		supersedeReads();
		// Reflect locally without a refetch.
		const target = items.find((n) => n.id === id);
		if (target && !target.read_at) {
			target.read_at = new Date().toISOString();
			unread = Math.max(0, unread - 1);
		}
		// Reassign to trigger reactivity on the array.
		items = [...items];
	}

	/** Mark every notification read. Pass the filter the caller is displaying:
	 *  under the Unread filter a mark-all empties the filtered set, and the
	 *  footer's "Showing all N" must not survive that. */
	async function markAllRead(opts: { unreadOnly?: boolean } = {}) {
		const res = await api.post<{ updated: number }>('/api/notifications/read-all', {});
		supersedeReads();
		const now = new Date().toISOString();
		items = items.map((n) => (n.read_at ? n : { ...n, read_at: now }));
		unread = 0;
		if (opts.unreadOnly) {
			// Every displayed row just stopped matching the Unread filter, so the
			// filtered count is zero. Set it synchronously, before any await: the
			// footer must not keep describing a set nothing is in even if the
			// reconciling reload below never lands.
			filteredTotal = 0;
			// Best-effort reconcile of the rows themselves (and, via the filtered
			// path in `load`, of `inboxTotal`). Swallowed: the mark-all itself
			// succeeded, and a blipped refresh must not turn the caller's success
			// toast into a failure one.
			await load({ unreadOnly: true }).catch(() => {});
		}
		return res.updated;
	}

	async function fetchPrefs() {
		prefs = await api.get<NotificationPrefs>('/api/notifications/preferences');
	}

	async function updatePrefs(changes: Partial<NotificationPrefs>) {
		prefs = await api.patch<NotificationPrefs>('/api/notifications/preferences', changes);
	}

	/** Start the unread-count poll for the sidebar badge. Idempotent. */
	function startPolling() {
		if (pollTimer || typeof setInterval === 'undefined') return;
		fetchUnreadCount();
		pollTimer = setInterval(fetchUnreadCount, POLL_INTERVAL_MS);
	}

	function stopPolling() {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	return {
		get items() {
			return items;
		},
		/** The server's count of the set the ACTIVE filter selects — what the
		 *  Load-more label and the "Showing all N" footer describe. */
		get filteredTotal() {
			return filteredTotal;
		},
		/** The server's count of the WHOLE inbox, whatever the list is filtered
		 *  to — what the All chip shows. */
		get inboxTotal() {
			return inboxTotal;
		},
		get unread() {
			return unread;
		},
		get loading() {
			return loading;
		},
		get hasMore() {
			return hasMore;
		},
		get prefs() {
			return prefs;
		},
		fetchList,
		loadMore,
		fetchUnreadCount,
		markRead,
		markAllRead,
		fetchPrefs,
		updatePrefs,
		startPolling,
		stopPolling,
	};
}

export const notificationStore = createNotificationStore();
