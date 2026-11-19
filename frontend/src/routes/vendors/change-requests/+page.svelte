<script lang="ts">
	// Vendor bank / tax change-approval queue — the UI half of the dual-control
	// (BEC / bank-redirect) gate. A staged `VendorChangeRequest` never touches
	// the vendor row until a SECOND user approves it here, so without this page
	// vendor banking cannot be changed through the app at all: `/vendors` stages
	// the request and the queue had no surface.
	//
	// Two different gates, honestly reflected:
	//   - READ  is role-gated admin | ap_manager (`GET /api/vendors/change-requests`
	//     → require_roles(ROLE_ADMIN, ROLE_AP_MANAGER); a CFO 403s, hence no cfo).
	//   - APPROVE is gated on the GRANULAR permission `vendor.bank_change.approve`
	//     (`require_permission`), which an org can split away from ap_manager. So
	//     an ap_manager without it sees the queue with a disabled Approve.
	//   - REJECT is role-gated like the list (admin | ap_manager) — refusing a
	//     change moves no money, so it isn't the splittable duty.
	//
	// Segregation of duties: the backend 403s an approver who is also the
	// proposer. That is knowable client-side (`requested_by_user_id`), so the
	// row says so instead of offering a button that can only fail — and the 403
	// is still mapped to the specific message, because the UI is not the gate.
	import { goto, replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { untrack } from 'svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import LoadMoreFooter from '#lib/components/ui/LoadMoreFooter.svelte';
	import { api, ApiError } from '#lib/api.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import { PERM_VENDOR_BANK_CHANGE_APPROVE } from '#lib/types/admin.ts';
	import { isAllowed, isOffered, refusalText } from '#lib/api/actionVerdicts.ts';
	import type {
		VendorChangeRequest,
		VendorChangeRequestCounts,
		VendorChangeRequestPage
	} from '#lib/types/vendor.ts';
	import { maskedProposalSummary, revealedProposalFields } from '#lib/types/vendor.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import FilterChips from '#lib/components/ui/FilterChips.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import type { BadgeTone } from '#lib/components/ui/badgeTone.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { errorMessage } from '#lib/utils/apiError.ts';

	const PAGE_SIZE = 20;

	// RBAC. Wait for `auth.user` to resolve before redirecting so we don't bounce
	// before /me lands (same race the api-keys / billing / audit pages document).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isManager);
	const canApprove = $derived(auth.can(PERM_VENDOR_BANK_CHANGE_APPROVE));

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	let items = $state<VendorChangeRequest[]>([]);
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(true);
	let loadingMore = $state(false);
	let errored = $state(false);
	// URL-backed (`?status=`) like the `/vendors` list it hangs off, so a
	// reload, Back, or a link shared with a second approver lands on the same
	// filter. `pending` is the default and is left out of the URL.
	const STATUS_KEYS = ['all', 'pending', 'approved', 'rejected'];
	const initialStatus = page.url.searchParams.get('status') ?? '';
	let statusFilter = $state(STATUS_KEYS.includes(initialStatus) ? initialStatus : 'pending');

	/** Writes the filter into the URL. A writer only: every read is untracked so
	 *  the filter `$effect` that calls it never depends on the URL it replaces. */
	function syncUrl() {
		untrack(() => {
			const url = new URL(page.url.href);
			if (statusFilter !== 'pending') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			if (url.search !== page.url.search) replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	// Whole-set tallies for the chips. `total` on the list response counts the
	// ACTIVE filter's result set, so rendering it on a chip would label the
	// filtered count with another status's name — and the Pending badge in
	// particular has to be the whole set, or a queue with 25 pending rows
	// reads as 20 once the page caps. Same reason `/api/vendors/counts` exists
	// for the vendor status chips.
	let counts = $state<VendorChangeRequestCounts | null>(null);
	// Latches on the first failure: one doomed request per visit, no toast, and
	// the chips fall back to label-only — exactly the pre-counts behaviour.
	let countsUnavailable = $state(false);

	let hasMore = $derived(items.length < total);

	// Armed two-click confirms, one id slot each (a row can't be mid-approve and
	// mid-reject at once, but the two must not share a slot or arming one would
	// silently arm the other).
	let approveArmedId = $state<string | null>(null);
	let rejectArmedId = $state<string | null>(null);

	/** Arm one commit and retract the other: two armed red buttons at once is
	 *  how a mis-click becomes the wrong irreversible decision (the same rule
	 *  `RunDetailModal` keeps for Execute / Cancel run). */
	function arm(kind: 'approve' | 'reject', id: string) {
		approveArmedId = kind === 'approve' ? id : null;
		rejectArmedId = kind === 'reject' ? id : null;
	}
	let busyId = $state<string | null>(null);

	// Detail modal — the selected request plus its REVEALED proposed value, which
	// the queue list deliberately masks. Verifying the full account before
	// approving is the callback control this gate exists to enable, and it comes
	// from the per-vendor endpoint (`reveal=True`), never from the list payload.
	let selected = $state<VendorChangeRequest | null>(null);
	let revealed = $state<VendorChangeRequest | null>(null);
	let revealLoading = $state(false);
	let revealError = $state(false);
	let reviewNote = $state('');

	// `null` = can't be flattened without losing a field → render raw JSON.
	const revealedFields = $derived(
		revealed ? revealedProposalFields(revealed.change_type, revealed.proposed_value) : null
	);

	// "All" first per the documented chip convention; `pending` is the ACTIVE
	// default because that is the backend's default and the only state an
	// approver has to act on.
	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all'), count: counts?.total },
		{
			key: 'pending',
			label: m('vendors.changeRequests.status.pending'),
			count: counts?.pending,
			// The red attention badge: an unreviewed bank change is work that
			// blocks a payee update, not a neutral tally.
			alert: (counts?.pending ?? 0) > 0
		},
		{
			key: 'approved',
			label: m('vendors.changeRequests.status.approved'),
			count: counts?.by_status?.approved
		},
		{
			key: 'rejected',
			label: m('vendors.changeRequests.status.rejected'),
			count: counts?.by_status?.rejected
		}
	]);

	const COLUMNS = $derived([
		{ label: m('vendors.col.vendor') },
		{ label: m('vendors.changeRequests.col.changeType') },
		{ label: m('vendors.changeRequests.col.proposed') },
		{ label: m('vendors.changeRequests.col.requestedBy') },
		{ label: m('vendors.changeRequests.col.requested') },
		{ label: m('vendors.col.status') },
		{ class: 'actions-col' }
	]);

	function typeLabel(changeType: string): string {
		if (changeType === 'bank_details') return m('vendors.changeRequests.type.bankDetails');
		if (changeType === 'tax_id') return m('vendors.changeRequests.type.taxId');
		return changeType;
	}

	/**
	 * Badge tone per change-request status. `pending` is amber because an
	 * unreviewed bank change is outstanding work on the BEC gate, not a
	 * neutral fact — the same reasoning the queue's red attention count uses.
	 */
	const STATUS_TONES: Record<string, BadgeTone> = {
		pending: 'warning',
		approved: 'success',
		rejected: 'danger'
	};

	function statusLabel(status: string): string {
		if (status === 'pending') return m('vendors.changeRequests.status.pending');
		if (status === 'approved') return m('vendors.changeRequests.status.approved');
		if (status === 'rejected') return m('vendors.changeRequests.status.rejected');
		return status;
	}

	/** True when the signed-in user is the AP requester — used only to LABEL
	 *  the requester column "You". Whether Approve is offered is the server's
	 *  `approve_refusal`, which also refuses the approver whose own provisioned
	 *  portal login staged the change — an axis this page never checked, so
	 *  that approver used to get an enabled Approve that 403'd (issue #575). */
	function isOwnRequest(r: VendorChangeRequest): boolean {
		return !!r.requested_by_user_id && r.requested_by_user_id === auth.user?.id;
	}

	/** The reader holds the approve permission but the server refuses THIS
	 *  reader on this request — a colleague must approve it. */
	function approveBlockedForMe(r: VendorChangeRequest): boolean {
		return isOffered(r.approve_refusal) && !isAllowed(r.approve_refusal);
	}

	function requesterLabel(r: VendorChangeRequest): string {
		if (isOwnRequest(r)) return m('vendors.changeRequests.requester.you');
		if (r.requested_by_vendor_user_id) return m('vendors.changeRequests.requester.supplier');
		return m('vendors.changeRequests.requester.apUser');
	}

	// Sequences `load`. Its triggers are the filter `$effect`, the header
	// Refresh, Load-more, and the post-decision refresh — so several can be in
	// flight at once. `applyDecision` rewrites a row in place with no fetch of
	// its own, so it retires whatever is in flight first: otherwise a list
	// response issued BEFORE an approval lands afterwards and puts the request
	// back to `pending` on screen, which on this queue reads as "the bank change
	// you just signed off didn't take". See `frontend/CLAUDE.md` § Sequencing
	// list fetches.
	const fetchSequence = createRequestSequencer();

	// Own sequencer: the tallies are an independent stream from the list, and a
	// shared counter would let a list load mark an in-flight counts response
	// un-committable and blank the chips.
	const countsSequence = createRequestSequencer();

	async function loadCounts() {
		if (countsUnavailable) return;
		const token = countsSequence.start();
		try {
			const res = await api.get<VendorChangeRequestCounts>(
				'/api/vendors/change-requests/counts'
			);
			if (!countsSequence.canCommit(token)) return;
			counts = res;
		} catch {
			// Deliberately silent. The tallies are an enhancement over a queue that
			// works without them, so a missing endpoint must not produce a toast on
			// every visit — it latches instead, and the chips render label-only.
			if (!countsSequence.isCurrentRequest(token)) return;
			countsUnavailable = true;
			counts = null;
		}
	}

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else {
			loading = true;
			// Retry unmounts `LoadError` back to the loading line (its contract).
			errored = false;
		}
		try {
			const params = new URLSearchParams({
				status: statusFilter,
				page: String(nextPage),
				page_size: String(PAGE_SIZE)
			});
			const res = await api.get<VendorChangeRequestPage>(
				`/api/vendors/change-requests?${params}`
			);
			// Superseded by a newer load, or by a local approve/reject.
			if (!fetchSequence.canCommit(token)) return;
			items = opts.append ? appendUnique(items, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
			errored = false;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// decision still failed, and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			errored = true;
			// **Clear the rows on a fresh load.** `errored` only reaches the
			// reader through `DataTable`'s `empty` message, which renders on
			// `isEmpty` alone — so a SECOND failed load (a chip click after one
			// good fetch) would otherwise leave the previous filter's rows on
			// screen as the answer to a filter that never ran, with nothing but
			// a toast that fades. On a bank-detail dual-control queue that is
			// the worst possible lie: the reader is deciding whether to approve
			// a change of where money goes. `total` goes with them, or the
			// "Showing all N" footer keeps reporting the stale count.
			//
			// An APPEND failure is the opposite case and must not clear: the
			// rows already loaded ARE the current filter's answer, and only the
			// next page failed to arrive. `/gl-accounts` and `/exceptions` set
			// the clearing precedent; neither of them paginates.
			//
			// A fresh-load failure is stated in place by `LoadError` (with a
			// Retry); only a failed Load-more, which keeps the rows shown, toasts.
			if (!opts.append) {
				items = [];
				total = 0;
			} else {
				toast(errorMessage(err, m('vendors.changeRequests.toast.loadFailed')), 'error');
			}
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	// Filter chip → server refetch. Only fires once the role gate has passed, so
	// a redirected clerk never issues a request that would 403.
	$effect(() => {
		statusFilter;
		if (!allowed) return;
		syncUrl();
		// `.catch` on both: neither is awaited here, and each already renders its
		// own failure (`errored` for the list, label-only chips for the tallies).
		load().catch(() => {});
		loadCounts().catch(() => {});
	});

	/** Write a decided request back into the list + the open modal. */
	function applyDecision(updated: VendorChangeRequest) {
		fetchSequence.supersedeInFlight();
		if (statusFilter !== 'all' && updated.status !== statusFilter) {
			// It no longer belongs in this view (the common case: a pending row
			// just approved while the Pending filter is on).
			items = items.filter((r) => r.id !== updated.id);
			total = Math.max(0, total - 1);
		} else {
			items = items.map((r) => (r.id === updated.id ? updated : r));
		}
		if (selected && selected.id === updated.id) {
			selected = updated;
			revealed = updated;
		}
		// A decision moves a row between statuses, so every chip's tally is now
		// wrong. Re-read rather than adjusting locally: the queue is shared, and
		// guessing the new numbers would drift the moment a second approver acts.
		loadCounts().catch(() => {});
	}

	/** Map the backend's real refusals onto messages an approver can act on. */
	function decisionErrorMessage(err: unknown, decision: 'approve' | 'reject'): string {
		if (err instanceof ApiError) {
			// Segregation of duties. The row disables Approve for a known
			// proposer, so reaching here means the server knew something the
			// client didn't — say what it was, not "Approve failed".
			if (err.status === 403) return m('vendors.changeRequests.toast.sod');
			// Already resolved by someone else; the row on screen is stale.
			if (err.status === 409) return m('vendors.changeRequests.toast.resolved');
		}
		return errorMessage(
			err,
			m(
				decision === 'approve'
					? 'vendors.changeRequests.toast.approveFailed'
					: 'vendors.changeRequests.toast.rejectFailed'
			)
		);
	}

	async function decide(r: VendorChangeRequest, decision: 'approve' | 'reject') {
		busyId = r.id;
		try {
			const body = reviewNote.trim() ? { review_note: reviewNote.trim() } : {};
			const updated = await api.post<VendorChangeRequest>(
				`/api/vendors/change-requests/${r.id}/${decision}`,
				body
			);
			applyDecision(updated);
			toast(
				m(
					decision === 'approve'
						? 'vendors.changeRequests.toast.approved'
						: 'vendors.changeRequests.toast.rejected'
				),
				'success'
			);
			closeDetail();
		} catch (err) {
			toast(decisionErrorMessage(err, decision), 'error');
			// A 409 means the row we are showing is stale — re-read so the queue
			// stops offering a decision that can no longer be made.
			if (err instanceof ApiError && err.status === 409) {
				load().catch(() => {});
				loadCounts().catch(() => {});
			}
		} finally {
			busyId = null;
			approveArmedId = null;
			rejectArmedId = null;
		}
	}

	async function openDetail(r: VendorChangeRequest) {
		selected = r;
		revealed = null;
		revealError = false;
		reviewNote = '';
		approveArmedId = null;
		rejectArmedId = null;
		revealLoading = true;
		try {
			// The per-vendor endpoint reveals the full proposed value so AP can
			// verify the new account before approving. Match by request id — a
			// vendor can carry several.
			const rows = await api.get<VendorChangeRequest[]>(
				`/api/vendors/${r.vendor_id}/change-requests`
			);
			if (selected?.id !== r.id) return; // modal moved on
			revealed = rows.find((row) => row.id === r.id) ?? null;
			if (!revealed) revealError = true;
		} catch {
			if (selected?.id !== r.id) return;
			revealError = true;
		} finally {
			if (selected?.id === r.id) revealLoading = false;
		}
	}

	function closeDetail() {
		selected = null;
		revealed = null;
		revealError = false;
		reviewNote = '';
	}

	/** Outside-click un-arms any pending armed confirm — the row actions and the
	 *  modal-footer decision buttons alike. */
	function onWindowClick(e: MouseEvent) {
		const target = e.target as Element | null;
		if (!target?.closest('.row-action, .btn-decide')) {
			if (approveArmedId) approveArmedId = null;
			if (rejectArmedId) rejectArmedId = null;
		}
	}
</script>

<svelte:window onclick={onWindowClick} />

<PageHeader title={m('vendors.changeRequests.title')}>
	{#snippet actions()}
		<button
			class="btn-secondary"
			onclick={() => {
				load().catch(() => {});
				loadCounts().catch(() => {});
			}}
			disabled={loading}
		>
			{loading ? m('vendors.changeRequests.refreshing') : m('vendors.changeRequests.refresh')}
		</button>
	{/snippet}

	<p class="intro">
		{m('vendors.changeRequests.intro')}
		<HelpTip term="bank-change-request" />
		<HelpTip term="segregation-of-duties" />
	</p>
	{#if !canApprove}
		<p class="no-perm-note">{m('vendors.changeRequests.noPermissionNote')}</p>
	{/if}

	<div class="filter-row">
		<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />
	</div>

	{#if errored && items.length === 0}
		<LoadError
			message={m('vendors.changeRequests.empty.errored')}
			onretry={() => {
				load().catch(() => {});
				loadCounts().catch(() => {});
			}}
			testId="change-requests-load-error"
		/>
	{:else}
	<DataTable
		columns={COLUMNS}
		isEmpty={items.length === 0}
		empty={loading
			? m('common.loading')
			: statusFilter === 'pending'
					? m('vendors.changeRequests.empty.pending')
					: m('vendors.changeRequests.empty.filtered')}
	>
		{#snippet body()}
			{#each items as r (r.id)}
				<tr
					class="clickable"
					data-testid="change-request-row"
					onclick={(e) => {
						if (isRowOpenClick(e)) openDetail(r);
					}}
				>
					<td class="vendor-name">
						<RowLink
							onclick={() => openDetail(r)}
							ariaLabel={m('vendors.changeRequests.row.open', {
								type: typeLabel(r.change_type),
								vendor: r.vendor_name ?? r.vendor_id
							})}
						>
							{r.vendor_name ?? '—'}
						</RowLink>
					</td>
					<td>{typeLabel(r.change_type)}</td>
					<td class="mono muted">{maskedProposalSummary(r.change_type, r.proposed_value) ?? '—'}</td>
					<td class="muted">{requesterLabel(r)}</td>
					<td class="muted">{formatDate(r.created_at)}</td>
					<td>
						<Badge tone={STATUS_TONES[r.status] ?? 'neutral'} variant={r.status}>{statusLabel(r.status)}</Badge>
					</td>
					<td class="actions">
						{#if r.status === 'pending'}
							{#if approveBlockedForMe(r)}
								<span class="sod-note">{refusalText(r.approve_refusal ?? null, m)}</span>
							{/if}
							<RowAction
								variant={approveArmedId === r.id ? 'danger' : 'success'}
								armed={approveArmedId === r.id}
								disabled={busyId === r.id || !isAllowed(r.approve_refusal)}
								title={!canApprove
									? m('vendors.changeRequests.row.needsPermission')
									: r.approve_refusal
										? refusalText(r.approve_refusal, m)
										: undefined}
								onclick={(e) => {
									e.stopPropagation();
									if (approveArmedId === r.id) decide(r, 'approve');
									else arm('approve', r.id);
								}}
							>
								{approveArmedId === r.id
									? m('vendors.changeRequests.row.confirmApprove')
									: m('vendors.changeRequests.row.approve')}
							</RowAction>
							<RowAction
								variant="danger"
								armed={rejectArmedId === r.id}
								disabled={busyId === r.id}
								onclick={(e) => {
									e.stopPropagation();
									if (rejectArmedId === r.id) decide(r, 'reject');
									else arm('reject', r.id);
								}}
							>
								{rejectArmedId === r.id
									? m('vendors.changeRequests.row.confirmReject')
									: m('vendors.changeRequests.row.reject')}
							</RowAction>
						{/if}
					</td>
				</tr>
			{/each}
		{/snippet}
	</DataTable>

	<LoadMoreFooter
		shown={items.length}
		{total}
		{hasMore}
		loading={loadingMore}
		onloadmore={() => load({ append: true })}
		moreKey="vendors.changeRequests.loadMore"
		allKey="vendors.changeRequests.showingAll"
	/>
	{/if}
</PageHeader>

<Modal
	open={selected !== null}
	ariaLabel={m('vendors.changeRequests.modal.aria')}
	width="md"
	onclose={closeDetail}
>
	{#if selected}
		<h2>{selected.vendor_name ?? '—'}</h2>

		<dl class="meta">
			<div>
				<dt>{m('vendors.changeRequests.col.changeType')}</dt>
				<dd>{typeLabel(selected.change_type)}</dd>
			</div>
			<div>
				<dt>{m('vendors.col.status')}</dt>
				<dd>{statusLabel(selected.status)}</dd>
			</div>
			<div>
				<dt>{m('vendors.changeRequests.col.requestedBy')}</dt>
				<dd>{requesterLabel(selected)}</dd>
			</div>
			<div>
				<dt>{m('vendors.changeRequests.modal.requested')}</dt>
				<dd>{formatDate(selected.created_at)}</dd>
			</div>
			{#if selected.reviewed_at}
				<div>
					<dt>{m('vendors.changeRequests.modal.reviewed')}</dt>
					<dd>{formatDate(selected.reviewed_at)}</dd>
				</div>
			{/if}
			{#if selected.review_note}
				<div>
					<dt>{m('vendors.changeRequests.modal.note')}</dt>
					<dd>{selected.review_note}</dd>
				</div>
			{/if}
		</dl>

		<h3>{m('vendors.changeRequests.modal.proposed')}</h3>
		{#if revealLoading}
			<LoadingState />
		{:else if revealError || !revealed}
			<p class="muted">{m('vendors.changeRequests.modal.revealFailed')}</p>
		{:else}
			{#if revealedFields}
				<dl class="proposed-fields" data-testid="proposed-value">
					{#each revealedFields as f (f.field)}
						<div>
							<dt>{f.labelKey ? m(f.labelKey) : f.fallback}</dt>
							<dd>{f.value}</dd>
						</div>
					{/each}
				</dl>
			{:else}
				<pre class="proposed" data-testid="proposed-value">{JSON.stringify(
						revealed?.proposed_value ?? {},
						null,
						2
					)}</pre>
			{/if}
		{/if}

		{#if selected.status === 'pending'}
			<p class="verify-hint">{m('vendors.changeRequests.modal.verifyHint')}</p>

			<label class="note-field">
				<span>{m('vendors.changeRequests.modal.reviewNote')}</span>
				<textarea
					rows="2"
					maxlength="1000"
					placeholder={m('vendors.changeRequests.modal.reviewNotePlaceholder')}
					bind:value={reviewNote}
				></textarea>
			</label>

			{#if approveBlockedForMe(selected)}
				<p class="sod-block" data-testid="sod-block">
					{refusalText(selected.approve_refusal ?? null, m)}
				</p>
			{:else if !canApprove}
				<p class="sod-block">{m('vendors.changeRequests.noPermissionNote')}</p>
			{/if}
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeDetail}>
				{m('vendors.modal.close')}
			</button>
			{#if selected.status === 'pending'}
				{@const current = selected}
				<!-- Footer-sized decision buttons rather than `RowAction` (which is
				     the per-ROW primitive and renders visibly smaller than
				     `.btn-cancel` beside it) — the same call `/vendors/screening`'s
				     modal makes for its block/re-screen pair. The armed two-click
				     and its outside-click un-arm are unchanged: both share the
				     row's `*ArmedId` slots, so arming here and arming the row
				     behind it can never disagree. The shared `.btn-danger` /
				     `.btn-primary` paint them (`.armed` included); `.btn-decide`
				     is only the hook the outside-click un-arm reads. -->
				<button
					type="button"
					class="btn-danger btn-decide reject"
					class:armed={rejectArmedId === current.id}
					disabled={busyId === current.id}
					onclick={(e) => {
						e.stopPropagation();
						if (rejectArmedId === current.id) decide(current, 'reject');
						else arm('reject', current.id);
					}}
				>
					{rejectArmedId === current.id
						? m('vendors.changeRequests.row.confirmReject')
						: m('vendors.changeRequests.row.reject')}
				</button>
				<button
					type="button"
					class="btn-primary btn-decide approve"
					class:armed={approveArmedId === current.id}
					disabled={busyId === current.id || !isAllowed(current.approve_refusal)}
					onclick={(e) => {
						e.stopPropagation();
						if (approveArmedId === current.id) decide(current, 'approve');
						else arm('approve', current.id);
					}}
				>
					{approveArmedId === current.id
						? m('vendors.changeRequests.row.confirmApprove')
						: m('vendors.changeRequests.row.approve')}
				</button>
			{/if}
		</div>
	{/if}
</Modal>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	.intro {
		margin: 0;
		font-size: 0.85rem;
		color: var(--text-muted);
		max-width: 78ch;
	}
	.no-perm-note {
		margin: 0;
		font-size: 0.82rem;
		color: var(--warning-on-tint);
		background: var(--warning-tint);
		border-radius: 6px;
		padding: 8px 12px;
		max-width: 78ch;
	}

	.filter-row {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.vendor-name {
		font-weight: 500;
	}

	.sod-note {
		font-size: 0.74rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	/* --- Detail modal --- */
	.meta {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 10px 24px;
		margin: 0 0 16px;
	}
	.meta div {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.meta dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	/* `minmax(0, 1fr)` tracks above + `anywhere` here: a long unbroken value
	   (a list name, an id) wraps inside its column instead of widening the
	   dialog past a 320px viewport (WCAG 1.4.10). */
	.meta dd {
		margin: 0;
		font-size: 0.9rem;
		color: var(--text);
		overflow-wrap: anywhere;
	}

	h3 {
		font-size: 0.95rem;
		margin: 0 0 8px;
	}
	.proposed {
		margin: 0 0 14px;
		padding: 10px 12px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: var(--font-mono);
		font-size: 0.8rem;
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 220px;
		overflow-y: auto;
	}

	.verify-hint {
		margin: 0 0 12px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.note-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 12px;
	}
	.note-field span {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.note-field textarea {
		padding: 7px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
		resize: vertical;
	}
	.note-field textarea:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
	}

	.proposed-fields {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 10px 24px;
		margin: 0 0 14px;
		padding: 10px 12px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
	}
	.proposed-fields div {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}
	.proposed-fields dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	.proposed-fields dd {
		margin: 0;
		font-family: var(--font-mono);
		font-size: 0.85rem;
		color: var(--text);
		word-break: break-word;
	}

	.sod-block {
		margin: 0 0 12px;
		font-size: 0.82rem;
		color: var(--warning-on-tint);
		background: var(--warning-tint);
		border-radius: 6px;
		padding: 8px 12px;
	}
</style>
