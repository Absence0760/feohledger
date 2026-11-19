<script lang="ts">
	import {
		portalApi,
		listPortalInvoices,
		resubmitPortalInvoice,
		PORTAL_PAGE_SIZE,
		type PortalInvoiceListItem,
	} from '#lib/portalApi.ts';
	import { portalAuth } from '#lib/stores/portalAuth.svelte.ts';
	import LoadMoreFooter from '#lib/components/ui/LoadMoreFooter.svelte';
	import { page } from '$app/state';
	import { replaceState } from '$app/navigation';
	import { onMount, untrack } from 'svelte';
	import { formatDate } from '#lib/utils/time.ts';
	import { appendUnique } from '#lib/utils/pagination.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import {
		portalInvoiceStatusLabelKey,
		portalInvoiceStatusTone,
		PORTAL_INVOICE_PHASES,
		type PortalInvoicePhase,
	} from '#lib/types/portalStatus.ts';
	import PortalListFilters from '#lib/components/portal/PortalListFilters.svelte';
	import EmptyState from '#lib/components/ui/EmptyState.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import SupplierChatThread from '#lib/components/chat/SupplierChatThread.svelte';
	import type { PortalChatThread } from '#lib/types/supplierChat.ts';
	import {
		getPortalChatThread,
		postPortalChatMessage,
		uploadPortalChatAttachment,
	} from '#lib/portalChat.ts';

	type PortalInvoice = PortalInvoiceListItem;

	// `waiting_on` bucket → localized "what is this waiting on" line. Typed so
	// `m()` stays checked (frontend/CLAUDE.md — dynamic keys go through a map).
	const WAITING_ON_KEY = {
		review: 'portal.invoices.waitingOn.review',
		processing: 'portal.invoices.waitingOn.processing',
		erp: 'portal.invoices.waitingOn.erp',
	} as const;

	let items = $state<PortalInvoice[]>([]);
	// `total` is the server's count of ALL the vendor's invoices, not just the
	// loaded page — the list is paged, so the footer can only claim "showing
	// all" once every row has actually been fetched.
	let total = $state(0);
	let pageNum = $state(1);
	let loading = $state(false);
	let loadingMore = $state(false);
	let uploading = $state(false);
	let error = $state('');
	// A failed FIRST-page read, kept apart from `error` (an action's failure):
	// with no rows on screen the list would otherwise fall through to "no
	// invoices yet, submit your first", which is a lie when the read failed.
	let loadError = $state('');
	let message = $state('');
	let downloadingFileId = $state<string | null>(null);
	let downloadingEinvoiceId = $state<string | null>(null);
	let resubmittingId = $state<string | null>(null);
	// Ref to the header's hidden file input, so the onboarding EmptyState's
	// action can open the same file picker.
	let uploadInput: HTMLInputElement | undefined = $state();
	// One hidden picker serves every rejected row's Resubmit button; the button
	// records which invoice it is for and opens it. A picker wrapped in a
	// `<label>` with `display: none` was unreachable by keyboard (WCAG 2.1.1).
	let resubmitInput: HTMLInputElement | undefined = $state();
	let resubmitTarget: string | null = null;

	const hasMore = $derived(items.length < total);

	/* URL-backed filter state. The portal home deep-links here (e.g.
	 * `?phase=rejected` from "these need your attention"), and a supplier who
	 * bookmarks or reloads a filtered list keeps it — the same treatment
	 * `/invoices`, `/payments` and `/vendors` already have. `phase` is validated
	 * against the known chips so a hand-edited URL cannot select a bucket the
	 * backend would not recognise.
	 *
	 * The value is the phase ID (`rejected`), never its label: the ids are
	 * stable across locales, so a bookmarked link keeps working when the
	 * supplier switches language — which a label-keyed URL could not do. */
	const seededPhase = (() => {
		const raw = page.url.searchParams.get('phase');
		if (!raw) return null;
		return PORTAL_INVOICE_PHASES.some((c) => c.phase === raw) ? raw : null;
	})();
	const initialFilters = {
		phase: seededPhase,
		search: page.url.searchParams.get('search') ?? '',
		dateFrom: page.url.searchParams.get('date_from') ?? '',
		dateTo: page.url.searchParams.get('date_to') ?? '',
	};

	// --- Filters (PortalListFilters owns the phase chips + debounced search and
	// hands back a resolved {phase, search}; the child's debounce means load()
	// is never reached from a reactive effect here). `phase` is a vendor-facing
	// bucket that expands to the InvoiceStatus values behind it, sent as
	// repeated `?status=`.
	let activePhase = $state<PortalInvoicePhase | null>(initialFilters.phase as PortalInvoicePhase | null);
	let activeSearch = $state(initialFilters.search);
	let activeDateFrom = $state(initialFilters.dateFrom);
	let activeDateTo = $state(initialFilters.dateTo);
	let filtersEl = $state<{ reset: () => void } | undefined>();
	const filtered = $derived(
		activePhase !== null || activeSearch.trim() !== '' || activeDateFrom !== '' || activeDateTo !== ''
	);

	function phaseStatuses(p: PortalInvoicePhase | null): string[] | undefined {
		if (p === null) return undefined;
		return PORTAL_INVOICE_PHASES.find((c) => c.phase === p)?.statuses;
	}

	function syncUrl() {
		untrack(() => {
			const url = new URL(page.url.href);
			const set = (k: string, v: string) =>
				v ? url.searchParams.set(k, v) : url.searchParams.delete(k);
			set('phase', activePhase ?? '');
			set('search', activeSearch.trim());
			set('date_from', activeDateFrom);
			set('date_to', activeDateTo);
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	function applyFilters(f: {
		phase: string | null;
		search: string;
		dateFrom: string;
		dateTo: string;
	}) {
		activePhase = f.phase as PortalInvoicePhase | null;
		activeSearch = f.search;
		activeDateFrom = f.dateFrom;
		activeDateTo = f.dateTo;
		syncUrl();
		load();
	}

	// Sequences `load` so a slow first page can't land after a Load-more (or
	// after the post-upload refresh) and drop rows. Nothing here edits the list
	// in place — submitting an invoice re-reads through this same loader — so
	// there is no `supersedeInFlight()` call. See `frontend/CLAUDE.md`
	// § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	async function load(opts: { append?: boolean } = {}) {
		const nextPage = opts.append ? pageNum + 1 : 1;
		const token = fetchSequence.start();
		if (opts.append) loadingMore = true;
		else loading = true;
		error = '';
		if (!opts.append) loadError = '';
		try {
			const res = await listPortalInvoices({
				page: nextPage,
				page_size: PORTAL_PAGE_SIZE,
				status: phaseStatuses(activePhase),
				search: activeSearch.trim() || undefined,
				date_from: activeDateFrom || undefined,
				date_to: activeDateTo || undefined,
			});
			if (!fetchSequence.canCommit(token)) return;
			items = opts.append ? appendUnique(items, res.items) : res.items;
			total = res.total;
			pageNum = nextPage;
		} catch (err) {
			// `isCurrentRequest`, not `canCommit`: a superseded load still failed,
			// and no newer load is coming to report it.
			if (!fetchSequence.isCurrentRequest(token)) return;
			const msg = err instanceof Error ? err.message : m('portal.invoices.loadFailed');
			if (opts.append) {
				error = msg;
			} else {
				// Drop the previous filter's rows: they no longer answer the
				// question the supplier just asked.
				items = [];
				total = 0;
				loadError = msg;
			}
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	/** Reload from page 1 (mount, and after a successful submit). */
	function refresh() {
		return load();
	}

	async function handleUpload(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		uploading = true;
		error = '';
		message = '';
		try {
			await portalApi.upload<{ message: string }>('/api/portal/invoices', file);
			message = m('portal.invoices.submitted');
			input.value = '';
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.invoices.uploadFailed');
		} finally {
			uploading = false;
		}
	}

	function pickResubmitFile(invoiceId: string) {
		resubmitTarget = invoiceId;
		resubmitInput?.click();
	}

	async function handleResubmit(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		const invoiceId = resubmitTarget;
		if (!file || !invoiceId) return;
		resubmittingId = invoiceId;
		error = '';
		message = '';
		try {
			await resubmitPortalInvoice(invoiceId, file);
			message = m('portal.invoices.resubmitted');
			input.value = '';
			await refresh();
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.invoices.resubmitFailed');
		} finally {
			resubmittingId = null;
			resubmitTarget = null;
			input.value = '';
		}
	}

	// --- Per-row expandable chat panel. Clicking a row toggles its thread;
	// the thread loads lazily on first expand and caches per invoice id. ---
	let expandedId = $state<string | null>(null);
	let chatThreads = $state<Record<string, PortalChatThread | null>>({});
	let chatLoading = $state<Record<string, boolean>>({});
	// A failed thread read, per invoice. Kept apart from `chatThreads` so a
	// failure is never dressed up as an empty conversation ("No messages yet")
	// — which would tell the supplier nobody has written when the read failed.
	let chatErrors = $state<Record<string, string>>({});

	async function toggleChat(invoiceId: string) {
		if (expandedId === invoiceId) {
			expandedId = null;
			return;
		}
		expandedId = invoiceId;
		if (chatThreads[invoiceId] === undefined) {
			await loadChat(invoiceId);
		}
	}

	async function loadChat(invoiceId: string) {
		chatLoading = { ...chatLoading, [invoiceId]: true };
		chatErrors = { ...chatErrors, [invoiceId]: '' };
		try {
			chatThreads = { ...chatThreads, [invoiceId]: await getPortalChatThread(invoiceId) };
		} catch {
			// Leave `chatThreads[invoiceId]` unset so re-expanding the row reads
			// again, and say the read failed with a way to retry it.
			chatErrors = { ...chatErrors, [invoiceId]: m('portal.invoices.chatLoadFailed') };
		} finally {
			chatLoading = { ...chatLoading, [invoiceId]: false };
		}
	}

	function chatSender(invoiceId: string) {
		return async (body: string, _mentions: string[], file?: File) => {
			if (file) {
				await uploadPortalChatAttachment(invoiceId, file, body || undefined);
			} else {
				await postPortalChatMessage(invoiceId, { body });
			}
			await loadChat(invoiceId);
		};
	}

	async function chatDownload(fileUrl: string, filename: string) {
		// `file_url` is "/api/portal/invoices/{id}/chat/file/{file_key}". The
		// download helper rebuilds that path from id + key, so pass the bytes
		// path straight through the portal client.
		const blob = await portalApi.download(fileUrl);
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.click();
		URL.revokeObjectURL(url);
	}

	/** Re-download the source file THIS vendor submitted for `inv`, via the
	 * vendor-scoped `GET /api/portal/invoices/{id}/file` proxy — the
	 * employee-only `/api/invoices/file/{key}` route (what an internal
	 * `file_url` used to always point at) rejects a vendor JWT outright, so
	 * a supplier had no way to ever re-view a document they themselves
	 * uploaded. */
	async function downloadInvoiceFile(inv: PortalInvoice) {
		if (!inv.file_url) return;
		downloadingFileId = inv.id;
		error = '';
		try {
			const blob = await portalApi.download(inv.file_url);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `invoice-${inv.invoice_number || inv.id.slice(0, 8)}`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.invoices.fileDownloadFailed');
		} finally {
			downloadingFileId = null;
		}
	}

	/** Download the structured (UBL 2.1) e-invoice for one of this vendor's own
	 * invoices, via the vendor-scoped `GET /api/portal/invoices/{id}/einvoice`.
	 *
	 * Distinct from "Download file" above: that returns the document the
	 * supplier themselves uploaded, this GENERATES a standards-compliant XML
	 * from the invoice as the customer now holds it — what a supplier's own
	 * accounting system can ingest, and the record of what the customer
	 * actually booked. Unlike the AP-side export it never 422s the supplier on
	 * a tax soft-warning; the UBL is always returned. Same blob-download shape
	 * as the remittance + source-file downloads. */
	async function downloadEinvoice(inv: PortalInvoice) {
		downloadingEinvoiceId = inv.id;
		error = '';
		try {
			const blob = await portalApi.download(`/api/portal/invoices/${inv.id}/einvoice`);
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `einvoice-${inv.invoice_number || inv.id.slice(0, 8)}.xml`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (err) {
			error = err instanceof Error ? err.message : m('portal.invoices.einvoiceDownloadFailed');
		} finally {
			downloadingEinvoiceId = null;
		}
	}

	onMount(refresh);
</script>

<div class="page">
	<header>
		<h1>{m('portal.invoices.title')}</h1>
		<input
			type="file"
			accept="application/pdf,image/*"
			bind:this={uploadInput}
			onchange={handleUpload}
			hidden
		/>
		<button
			type="button"
			class="upload-btn"
			data-testid="portal-invoice-upload"
			disabled={uploading}
			onclick={() => uploadInput?.click()}
		>
			{uploading ? m('portal.invoices.submitting') : m('portal.invoices.submit')}
		</button>
	</header>
	<input
		type="file"
		accept="application/pdf,image/*"
		bind:this={resubmitInput}
		onchange={handleResubmit}
		hidden
	/>

	{#if error}<div class="error" role="alert">{error}</div>{/if}
	{#if message}<div class="msg" role="status" aria-live="polite">{message}</div>{/if}

	<PortalListFilters
		bind:this={filtersEl}
		chips={PORTAL_INVOICE_PHASES.map((c) => ({ key: c.phase, label: m(c.labelKey) }))}
		allLabel={m('portal.invoices.filterAll')}
		groupLabel={m('portal.invoices.col.status')}
		searchLabel={m('portal.invoices.searchLabel')}
		searchPlaceholder={m('portal.invoices.searchPlaceholder')}
		dateFromLabel={m('portal.invoices.dateFromLabel')}
		dateToLabel={m('portal.invoices.dateToLabel')}
		initial={initialFilters}
		onchange={applyFilters}
	/>

	{#if loading && !items.length}
		<LoadingState label={m('portal.common.loading')} />
	{:else if loadError}
		<LoadError message={loadError} onretry={refresh} testId="portal-invoices-load-error" />
	{:else if !items.length}
		{#if filtered}
			<div class="empty">
				<p>{m('portal.invoices.emptyFiltered')}</p>
				<button type="button" class="link-btn" onclick={() => filtersEl?.reset()}
					>{m('portal.invoices.clearFilters')}</button
				>
			</div>
		{:else}
			<EmptyState
				icon="📄"
				heading={m('portal.invoices.empty')}
				description={m('portal.invoices.emptyHint')}
				actionLabel={m('portal.invoices.submit')}
				onaction={() => uploadInput?.click()}
				testId="portal-invoices-empty-state"
			/>
		{/if}
	{:else}
		<!-- DataTable, not a bare <table>: seven columns cannot reflow at 320px,
		     so the table scrolls inside its own named, keyboard-focusable region
		     (WCAG 1.4.10 / 2.1.1) instead of pushing the whole page sideways. -->
		<DataTable
			ariaLabel={m('portal.invoices.tableAria')}
			columns={[
				{ label: m('portal.invoices.col.invoiceNumber') },
				{ label: m('portal.invoices.col.submitted') },
				{ label: m('portal.invoices.col.invoiceDate') },
				{ label: m('portal.invoices.col.due') },
				{ label: m('portal.invoices.col.amount'), class: 'right' },
				{ label: m('portal.invoices.col.status') },
				{ class: 'actions-col' }
			]}
		>
			{#snippet body()}
				{#each items as inv (inv.id)}
					<tr
						class="clickable"
						class:expanded={expandedId === inv.id}
						onclick={(e) => {
							// Pointer enhancement: clicking anywhere on the row toggles the
							// chat disclosure, except when the click lands on the in-cell
							// toggle button, or on one of the file / resubmit controls
							// (each handles its own click).
							if (
								(e.target as HTMLElement).closest('.row-toggle, .resubmit-btn, .file-btn')
							)
								return;
							toggleChat(inv.id);
						}}
					>
						<td>
							<button
								type="button"
								class="row-toggle"
								aria-expanded={expandedId === inv.id}
								onclick={() => toggleChat(inv.id)}
							>
								<span class="row-caret" aria-hidden="true"
									>{expandedId === inv.id ? '▾' : '▸'}</span
								>
								{inv.invoice_number || m('portal.invoices.pendingExtraction')}
							</button>
						</td>
						<td>{formatDate(inv.submitted_at, m('portal.common.dash'))}</td>
						<td>{formatDate(inv.invoice_date, m('portal.common.dash'))}</td>
						<td>{formatDate(inv.due_date, m('portal.common.dash'))}</td>
						<td class="right"><Money amount={inv.amount} currency={inv.currency} mono /></td>
						<td>
							<Badge tone={portalInvoiceStatusTone(inv.status)} variant="status-badge {inv.status}"
								>{m(portalInvoiceStatusLabelKey(inv.status))}</Badge
							>
							{#if inv.waiting_on}
								<div class="waiting-on">
									{m(WAITING_ON_KEY[inv.waiting_on])}{#if (inv.waiting_on_days ?? 0) > 0}
										&nbsp;· {m('portal.invoices.waitingOnDays', { days: inv.waiting_on_days ?? 0 })}{/if}
								</div>
							{/if}
							{#if inv.status === 'rejected'}
								{#if inv.rejection_reason}
									<div class="reject-reason">
										<span class="reject-label">{m('portal.invoices.rejectionReasonLabel')}</span>
										{inv.rejection_reason}
									</div>
								{/if}
								<button
									type="button"
									class="resubmit-btn"
									disabled={resubmittingId !== null}
									onclick={() => pickResubmitFile(inv.id)}
								>
									{resubmittingId === inv.id
										? m('portal.invoices.resubmitting')
										: m('portal.invoices.resubmit')}
								</button>
							{/if}
						</td>
						<td class="actions">
							{#if inv.file_url}
								<button
									type="button"
									class="file-btn"
									disabled={downloadingFileId === inv.id}
									onclick={(e) => {
										e.stopPropagation();
										downloadInvoiceFile(inv);
									}}
								>
									{downloadingFileId === inv.id
										? m('portal.invoices.preparing')
										: m('portal.invoices.downloadFile')}
								</button>
							{/if}
							<button
								type="button"
								class="file-btn"
								data-testid="portal-einvoice-download"
								disabled={downloadingEinvoiceId === inv.id}
								onclick={(e) => {
									e.stopPropagation();
									downloadEinvoice(inv);
								}}
							>
								{downloadingEinvoiceId === inv.id
									? m('portal.invoices.preparing')
									: m('portal.invoices.downloadEinvoice')}
							</button>
						</td>
					</tr>
					{#if expandedId === inv.id}
						<!-- svelte-ignore a11y_no_static_element_interactions -->
						<tr class="chat-row" onclick={(e) => e.stopPropagation()}>
							<td colspan="7">
								<SupplierChatThread
									surface="vendor"
									thread={chatThreads[inv.id] ?? null}
									currentUserId={portalAuth.user?.id}
									members={[]}
									templates={[]}
									loading={chatLoading[inv.id] ?? false}
									error={chatErrors[inv.id] || undefined}
									onretry={() => loadChat(inv.id)}
									onsend={chatSender(inv.id)}
									ondownload={chatDownload}
								/>
							</td>
						</tr>
					{/if}
				{/each}
			{/snippet}
		</DataTable>

		<LoadMoreFooter
			shown={items.length}
			{total}
			{hasMore}
			loading={loadingMore}
			onloadmore={() => load({ append: true })}
			moreKey="portal.invoices.loadMore"
			allKey="portal.invoices.showingAll"
			loadingKey="portal.common.loading"
		/>
	{/if}
</div>

<style>
	.page {
		max-width: 1100px;
		margin: 0 auto;
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 20px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
	}
	.upload-btn {
		display: inline-block;
		background: var(--accent-strong);
		color: #fff;
		border: none;
		padding: 8px 14px;
		border-radius: 4px;
		cursor: pointer;
		font: inherit;
		font-size: 0.85rem;
	}
	.upload-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.link-btn {
		background: none;
		border: none;
		padding: 0;
		margin-top: 6px;
		font: inherit;
		font-size: 0.82rem;
		color: var(--accent);
		cursor: pointer;
		text-decoration: underline;
	}
	/* Table chrome (padding, borders, header, hover, `.clickable`, `.right`,
	   `td.actions`) is app.css's `.grid-container`, owned by DataTable. */
	tr.expanded td {
		background: var(--bg);
	}
	.row-toggle {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: none;
		border: none;
		padding: 0;
		margin: 0;
		font: inherit;
		color: inherit;
		cursor: pointer;
		text-align: left;
	}
	.row-caret {
		display: inline-block;
		width: 14px;
		color: var(--text-muted);
		font-size: 0.75rem;
	}
	tr.chat-row td {
		background: var(--bg);
		cursor: default;
		padding: 14px 16px;
	}
	.reject-reason {
		margin-top: 6px;
		max-width: 22rem;
		font-size: 0.78rem;
		color: var(--text-muted);
		line-height: 1.35;
	}
	.waiting-on {
		margin-top: 4px;
		font-size: 0.76rem;
		color: var(--text-muted);
	}
	.reject-label {
		display: block;
		font-weight: 600;
		color: var(--danger);
		text-transform: uppercase;
		font-size: 0.68rem;
		letter-spacing: 0.03em;
	}
	.resubmit-btn {
		display: inline-block;
		margin-top: 8px;
		padding: 4px 10px;
		font-size: 0.78rem;
		border: 1px solid var(--accent);
		border-radius: 4px;
		color: var(--accent);
		background: transparent;
		font: inherit;
		font-size: 0.78rem;
		cursor: pointer;
	}
	.resubmit-btn:hover:not(:disabled) {
		background: var(--accent-strong);
		border-color: var(--accent-strong);
		color: #fff;
	}
	.resubmit-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	/* The two per-row downloads are spaced by app.css's `td.actions > * + *`. */
	.file-btn {
		padding: 4px 12px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font-size: 0.8rem;
		cursor: pointer;
	}
	.file-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.file-btn:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.empty,
	.empty .hint {
		font-size: 0.82rem;
	}
	.error {
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
	.msg {
		background: color-mix(in srgb, var(--success) 12%, transparent);
		border: 1px solid color-mix(in srgb, var(--success) 35%, transparent);
		color: var(--success);
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 12px;
	}
</style>
