<script lang="ts">
	import { api } from '$lib/api';
	import { appendUnique, fetchAllPages, type PagedResponse } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { untrack } from 'svelte';
	import { page as urlStore } from '$app/stores';
	import { replaceState } from '$app/navigation';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import VendorPicker from '$lib/components/ui/VendorPicker.svelte';
	import Badge, { type BadgeTone } from '$lib/components/ui/Badge.svelte';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	import { formatDate } from '$lib/utils/time';
	import { currencyOptions } from '$lib/utils/money';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';

	// Create / apply / void are all `require_roles(ADMIN, AP_MANAGER)` on the
	// backend, while the LIST is open to all four roles. The page carried no
	// role check at all: a CFO — who reaches it through nav.ts — completed the
	// create modal, or armed the two-click Void, and only then got a 403. Read
	// stays open, which is also what lets the nav row admit a clerk.
	const canMutate = $derived(auth.isManager);

	/**
	 * Badge tone per credit-memo status.
	 *
	 * `void` keeps the flat `neutral` chip it already had (a voided memo is the
	 * absence of a credit, not a state to scan for), and `open` stays amber
	 * against `applied`'s green — the two the row-muting rule relies on to
	 * explain WHY a row is de-emphasised (`tests-e2e/a11y/deemphasised-rows`).
	 */
	const STATUS_TONES: Record<string, BadgeTone> = {
		open: 'warning',
		applied: 'success',
		void: 'neutral'
	};

	/**
	 * The badge used to print `memo.status` verbatim — the column VALUE, not a
	 * label — so every locale rendered the English enum (`open` / `applied` /
	 * `void`) inside an otherwise translated page. The chips have always keyed
	 * off these three; the badge now shares them. An unknown status (a future
	 * backend state this build predates) falls back to the raw value rather
	 * than rendering a missing key.
	 */
	const STATUS_LABEL_KEYS: Record<string, MessageKey> = {
		open: 'creditMemos.status.open',
		applied: 'creditMemos.status.applied',
		void: 'creditMemos.status.void'
	};

	/** The statuses `?status=` may carry — anything else falls back to `all`. */
	const STATUSES = ['open', 'applied', 'void'];

	const STATUS_CHIPS = $derived([
		{ key: 'all', label: m('common.all') },
		{ key: 'open', label: m('creditMemos.status.open') },
		{ key: 'applied', label: m('creditMemos.status.applied') },
		{ key: 'void', label: m('creditMemos.status.void') }
	]);

	const COLUMNS = $derived([
		{ label: m('creditMemos.col.memoNumber') },
		{ label: m('creditMemos.col.vendor') },
		{ label: m('creditMemos.col.amount'), class: 'right' },
		{ label: m('creditMemos.col.issued') },
		{ label: m('creditMemos.col.appliedTo') },
		// The create form captures a reason and nothing in the product ever
		// showed it again — the one field that says WHY a supplier owes this
		// credit. Reuses the form's own label rather than minting a second
		// catalogue entry for the same word. It sits AFTER the money columns
		// deliberately: the table scrolls horizontally on a narrow viewport, and
		// putting free text ahead of the amount pushed the figure off a phone.
		{ label: m('creditMemos.createModal.reason') },
		{ label: m('creditMemos.col.status') },
		{ class: 'actions-col' }
	]);

	interface CreditMemo {
		id: string;
		memo_number: string;
		vendor_id: string;
		vendor_name: string | null;
		invoice_id: string | null;
		invoice_number: string | null;
		amount: number;
		currency: string;
		issued_date: string | null;
		reason: string | null;
		status: string;
		applied_at: string | null;
		applied_by: string | null;
		created_at: string;
	}

	interface Invoice {
		id: string;
		invoice_number: string;
		vendor: string;
		vendor_id: string | null;
	}

	let memos = $state<CreditMemo[]>([]);
	let invoices = $state<Invoice[]>([]);
	let loading = $state(true);
	// Distinguishes "the list is empty" from "we never found out" — without it
	// a failed fetch left the table asserting there are no credit memos, which
	// on a payables surface reads as "this vendor owes you nothing".
	let errored = $state(false);
	// The invoice select is the Apply dialog's whole set of valid targets, so a
	// failed load has to say so: an empty select otherwise claims the vendor has
	// no creditable invoice.
	let invoicesErrored = $state(false);
	// Round-trips through `?status=` like the other ~24 list routes, so back /
	// forward / reload / a pasted link reproduce the view. An unrecognised value
	// falls back to `all` rather than silently filtering to nothing.
	let statusFilter = $state<string>(
		STATUSES.includes($urlStore.url.searchParams.get('status') ?? '')
			? ($urlStore.url.searchParams.get('status') as string)
			: 'all'
	);
	let showCreate = $state(false);
	let applyTargetId = $state<string | null>(null);

	const PAGE_SIZE = 20;
	let total = $state(0);
	let page = $state(1);
	let loadingMore = $state(false);

	let newMemoNumber = $state('');
	// Seeded from the org's reporting currency once it loads, and only while
	// the user hasn't chosen one — otherwise a late `ensureLoaded()` would
	// overwrite a deliberate pick mid-form.
	let newCurrency = $state('');
	let currencyTouched = $state(false);
	let newVendorId = $state('');
	let newAmount = $state('');
	let newReason = $state('');
	let saving = $state(false);

	let applyInvoiceId = $state('');
	let applying = $state(false);

	// Sequences every `loadMemos` call — mount, status chip, load-more; one
	// counter, latest-issued wins — so a page-1 replace and a page-2 append
	// can't land out of order. Load more, then switch the chip: the replace
	// landed first and the append then pushed the OLD filter's page-2 rows onto
	// the new list and overwrote `total`/`page` with them. Create / apply / void
	// all re-fetch through this loader rather than editing a row in place, so no
	// `supersedeInFlight()` call is needed. See `frontend/CLAUDE.md`
	// § Sequencing list fetches.
	const fetchSequence = createRequestSequencer();

	// The shortlist always contains the org's own reporting currency, so the
	// picker can never be unable to express the currency the tenant reports in.
	const CURRENCY_OPTIONS = $derived(currencyOptions(orgCurrency.currency));

	$effect(() => {
		orgCurrency.ensureLoaded().catch(() => {
			/* degrades to DEFAULT_CURRENCY by design — see orgSettings.svelte.ts */
		});
	});

	// Seed the select once the org currency resolves, unless the user already
	// picked. `untrack` on the write so this effect depends on the store, not
	// on its own output.
	$effect(() => {
		const ccy = orgCurrency.currency;
		if (untrack(() => currencyTouched)) return;
		newCurrency = ccy;
	});

	// The mount effect loads all three lists ONCE. It must not depend on
	// `statusFilter`: `loadMemos` reads it synchronously (before its first
	// await), and Svelte tracks reads transitively through called functions, so
	// a plain read there made this effect a second status-filter subscriber —
	// every chip click fired it AND the effect below, two unsequenced page-1
	// requests racing with whichever landed last winning (and the vendor /
	// invoice selects needlessly refetched). `untrack` inside `loadMemos` still
	// reads the CURRENT filter, it just stops the read registering as the
	// caller's dependency.
	$effect(() => {
		loadAll();
	});

	// Status chip → refetch. Skips its own mount-time run: a Svelte `$effect`
	// always fires once immediately whether or not its tracked value actually
	// changed, so without the guard this queued a second page-1 request on top
	// of the mount load above.
	let statusEffectRan = false;
	$effect(() => {
		statusFilter;
		if (!statusEffectRan) {
			statusEffectRan = true;
			return;
		}
		syncUrl();
		loadMemos();
	});

	// A WRITER of URL state, never a dependency source — every read inside is
	// untracked so calling it from the status effect can't make that effect
	// depend on `$urlStore` and re-fire itself (the pattern `/vendors` and
	// `/expenses` settled; see ui-patterns.md § Sequencing list fetches).
	function syncUrl() {
		untrack(() => {
			const url = new URL($urlStore.url);
			if (statusFilter !== 'all') url.searchParams.set('status', statusFilter);
			else url.searchParams.delete('status');
			replaceState(`${url.pathname}${url.search}`, {});
		});
	}

	async function loadAll() {
		await Promise.all([loadMemos(), loadInvoices()]);
	}

	async function loadMemos(opts: { append?: boolean; nextPage?: number } = {}) {
		const token = fetchSequence.start();
		// `loading` is what the table renders instead of "No credit memos" while
		// a fetch is out. `loadMemos` never touched it, so a status-chip change
		// sat on the previous filter's rows with no spinner until the response
		// landed.
		if (opts.append) loadingMore = true;
		else loading = true;
		try {
			const nextPage = opts.nextPage ?? 1;
			const params = new URLSearchParams();
			const status = untrack(() => statusFilter);
			if (status !== 'all') params.set('status', status);
			params.set('page', String(nextPage));
			params.set('page_size', String(PAGE_SIZE));
			const data = await api.get<{ items: CreditMemo[]; total: number }>(
				`/api/credit-memos?${params}`
			);
			// Superseded by a newer load — discard rather than clobber.
			if (!fetchSequence.canCommit(token)) return;
			memos = opts.append ? appendUnique(memos, data.items) : data.items;
			total = data.total;
			page = nextPage;
			errored = false;
		} catch {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!fetchSequence.isCurrentRequest(token)) return;
			errored = true;
			toast(m('creditMemos.toast.loadFailed'), 'error');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) {
				loading = false;
				loadingMore = false;
			}
		}
	}

	async function loadMoreMemos() {
		await loadMemos({ append: true, nextPage: page + 1 });
	}

	let hasMore = $derived(memos.length < total);

	// The vendor list is no longer fetched here: `ui/VendorPicker` searches it
	// server-side, so the create modal reaches every supplier without this page
	// walking one request per 100 of them on mount.
	//
	// The INVOICE list still is, and for the reason the vendor one used to be:
	// its options ARE the set of valid choices, so a truncated fetch is not a
	// shorter list — it is the invoice the operator wants to credit, missing,
	// with no search inside a native `<select>` to reach it. A bare
	// `api.get('/api/invoices')` returns the server's DEFAULT_PAGE_SIZE of 20,
	// and raising `page_size` only moves the cliff (the server caps it at
	// MAX_PAGE_SIZE), so `fetchAllPages` walks the envelope's own `total`.
	// Giving it the picker's treatment needs an invoice-shaped combobox of its
	// own — tracked separately rather than half-done here.
	async function loadInvoices() {
		try {
			invoices = await fetchAllPages<Invoice>((page, pageSize) =>
				api.get<PagedResponse<Invoice>>(`/api/invoices?page=${page}&page_size=${pageSize}`)
			);
			invoicesErrored = false;
		} catch {
			// Not fatal to the list, but the Apply dialog must not present an
			// empty select as "this vendor has no creditable invoice".
			invoicesErrored = true;
		}
	}

	async function handleCreate() {
		if (!newMemoNumber.trim() || !newVendorId || !newAmount) return;
		saving = true;
		try {
			await api.post('/api/credit-memos', {
				memo_number: newMemoNumber.trim(),
				vendor_id: newVendorId,
				amount: parseFloat(newAmount),
				// Sent explicitly. The backend resolves an omitted currency from the
				// named invoice, then the org's reporting currency — but this form
				// creates an UNLINKED memo (there is no invoice field; linking
				// happens later in the Apply dialog), so there is nothing to inherit
				// from and the org default would be the only answer. A mixed-currency
				// tenant issuing a EUR credit against a USD-reporting org needs to
				// say so here, and there is no PATCH on credit memos to fix it after.
				currency: newCurrency || orgCurrency.currency,
				reason: newReason.trim() || null
			});
			toast(m('creditMemos.toast.created'), 'success');
			showCreate = false;
			newMemoNumber = '';
			newVendorId = '';
			newAmount = '';
			newReason = '';
			// Back to the org default for the next memo — a one-off foreign-currency
			// credit shouldn't become sticky for every memo after it.
			currencyTouched = false;
			newCurrency = orgCurrency.currency;
			await loadMemos();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('creditMemos.toast.createFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function handleApply() {
		if (!applyTargetId || !applyInvoiceId) return;
		applying = true;
		try {
			await api.post(`/api/credit-memos/${applyTargetId}/apply`, {
				invoice_id: applyInvoiceId
			});
			toast(m('creditMemos.toast.applied'), 'success');
			applyTargetId = null;
			applyInvoiceId = '';
			await loadMemos();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('creditMemos.toast.applyFailed'), 'error');
		} finally {
			applying = false;
		}
	}

	// Void is irreversible — arm on first click, commit on the second (the
	// app-wide destructive-action pattern), and guard against a double-submit.
	let confirmVoidId = $state<string | null>(null);
	let voidingId = $state<string | null>(null);

	async function handleVoid(id: string) {
		if (confirmVoidId !== id) {
			confirmVoidId = id;
			return;
		}
		confirmVoidId = null;
		voidingId = id;
		try {
			await api.post(`/api/credit-memos/${id}/void`, {});
			toast(m('creditMemos.toast.voided'), 'success');
			await loadMemos();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('creditMemos.toast.voidFailed'), 'error');
		} finally {
			voidingId = null;
		}
	}

	// The memo the Apply dialog is acting on. Named in the dialog so the
	// operator can see WHICH credit they are about to consume — the amount and
	// the vendor are the two facts that decide whether the invoice they pick is
	// the right one, and neither was on screen once the dialog covered the row.
	let applyMemo = $derived(memos.find((cm) => cm.id === applyTargetId) ?? null);

	// Only this memo's own vendor's invoices are valid targets. An invoice with
	// no resolved `vendor_id` is NOT a wildcard — its vendor can't be proven, so
	// the backend refuses the apply (409) and offering it here would only invite
	// the error. Resolve the invoice's vendor first (re-save it on the invoice).
	let invoicesForVendor = $derived.by(() => {
		const memo = applyMemo;
		if (!memo) return [];
		return invoices.filter((i) => i.vendor_id === memo.vendor_id);
	});

	/**
	 * Four distinct answers, never one message doing duty for all of them:
	 * still loading, the fetch failed, a filter matched nothing, or this tenant
	 * genuinely has no credit memos. The third is the one this page used to get
	 * wrong — "No credit memos." under an active Void chip is false whenever
	 * the tenant has open ones.
	 */
	let emptyMessage = $derived(
		loading
			? m('common.loading')
			: errored
				? m('common.loadFailed')
				: statusFilter !== 'all'
					? m('creditMemos.empty.filtered')
					: m('creditMemos.empty')
	);

	// Only the true zero-data case gets the illustrated onboarding block: a
	// filter that matched nothing keeps the plain in-table line, since "create
	// your first credit memo" is a non-sequitur when the tenant already has
	// some and the chip is simply narrow.
	let showOnboarding = $derived(
		!loading && !errored && total === 0 && memos.length === 0 && statusFilter === 'all'
	);
</script>

<svelte:window
	onclick={(e) => {
		// Outside-click un-arms a pending Void confirmation.
		if (confirmVoidId && !(e.target as HTMLElement)?.closest?.('.row-action')) {
			confirmVoidId = null;
		}
	}}
/>

<PageHeader title={m('creditMemos.title')}>
	{#snippet actions()}
		{#if canMutate}
			<button class="btn-primary" onclick={() => (showCreate = true)}>{m('creditMemos.new')}</button>
		{/if}
	{/snippet}

	<FilterChips chips={STATUS_CHIPS} bind:active={statusFilter} />

	{#if showOnboarding}
		<EmptyState
			art="documents"
			testId="credit-memos-empty-state"
			heading={m('creditMemos.onboarding.heading')}
			description={m('creditMemos.onboarding.description')}
			actionLabel={canMutate ? m('creditMemos.new') : undefined}
			onaction={canMutate ? () => (showCreate = true) : undefined}
		/>
	{:else}
		<DataTable columns={COLUMNS} isEmpty={memos.length === 0} empty={emptyMessage}>
			{#snippet body()}
				{#each memos as memo (memo.id)}
					<tr
						class:applied={memo.status === 'applied'}
						class:void={memo.status === 'void'}
						class:row-muted={memo.status === 'applied' || memo.status === 'void'}
					>
						<td class="mono">{memo.memo_number}</td>
						<td>{memo.vendor_name ?? '—'}</td>
						<td class="right mono"><Money amount={memo.amount} currency={memo.currency} /></td>
						<td class="muted">{formatDate(memo.issued_date)}</td>
						<!-- When the credit was consumed is recorded and was shown
						     nowhere; the applied date rides along with the invoice it
						     was applied to. -->
						<td class="mono muted" title={memo.applied_at ? formatDate(memo.applied_at) : undefined}>
							{memo.invoice_number ?? '—'}
						</td>
						<!-- `title` carries the untruncated reason; it is the memo's own
						     text, not copy, so it needs no catalogue entry. -->
						<td class="reason muted" title={memo.reason ?? undefined}>{memo.reason ?? '—'}</td>
						<td>
							<Badge tone={STATUS_TONES[memo.status] ?? 'neutral'} variant={memo.status}>
								{STATUS_LABEL_KEYS[memo.status] ? m(STATUS_LABEL_KEYS[memo.status]) : memo.status}
							</Badge>
						</td>
						<td class="actions">
							{#if canMutate && memo.status === 'open'}
								<RowAction onclick={() => { applyTargetId = memo.id; applyInvoiceId = ''; }}>{m('creditMemos.row.apply')}</RowAction>
								<RowAction
									variant="danger"
									armed={confirmVoidId === memo.id}
									disabled={voidingId === memo.id}
									onclick={() => handleVoid(memo.id)}
								>
									{confirmVoidId === memo.id ? m('creditMemos.row.confirm') : m('creditMemos.row.void')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}

	{#if hasMore}
		<div class="load-more-row">
			<button class="btn-load-more" onclick={loadMoreMemos} disabled={loadingMore}>
				{loadingMore ? m('common.loading') : m('creditMemos.loadMore', { shown: memos.length, total })}
			</button>
		</div>
	{:else if total > 0}
		<div class="load-more-row">
			<span class="load-more-end">{m('creditMemos.showingAll', { total })}</span>
		</div>
	{/if}
</PageHeader>

<Modal
	open={showCreate}
	ariaLabel={m('creditMemos.createModal.aria')}
	title={m('creditMemos.createModal.title')}
	width="sm"
	onclose={() => (showCreate = false)}
>
	<form onsubmit={(e) => { e.preventDefault(); handleCreate(); }}>
		<label>
			<span>{m('creditMemos.createModal.memoNumber')} <em class="required">*</em></span>
			<input type="text" bind:value={newMemoNumber} required />
		</label>
		<VendorPicker
			bind:value={newVendorId}
			label={m('creditMemos.createModal.vendor')}
			placeholder={m('creditMemos.createModal.selectVendor')}
			required
			disabled={!canMutate}
		/>
		<label>
			<span>{m('creditMemos.createModal.amount')} <em class="required">*</em></span>
			<input type="number" min="0.01" step="0.01" bind:value={newAmount} required />
		</label>
		<label>
			<span>{m('creditMemos.createModal.currency')} <em class="required">*</em></span>
			<select
				value={newCurrency}
				onchange={(e) => {
					currencyTouched = true;
					newCurrency = (e.currentTarget as HTMLSelectElement).value;
				}}
				required
				aria-describedby="cm-currency-hint"
			>
				{#each CURRENCY_OPTIONS as ccy (ccy)}
					<option value={ccy}>{ccy}</option>
				{/each}
			</select>
			<!-- `aria-describedby`, not a bare child of the `<label>`: a hint
			     inside the label is folded into the control's accessible NAME, so
			     a screen reader announces the whole sentence every time the field
			     is focused. A hint is a description, not a name. `aria-hidden`
			     removes it from the NAME computation; `aria-describedby` still
			     resolves its text, so nothing is lost to a screen reader. -->
			<small id="cm-currency-hint" class="field-hint" aria-hidden="true">
				{m('creditMemos.createModal.currencyHint')}
			</small>
		</label>
		<label>
			<span>{m('creditMemos.createModal.reason')}</span>
			<textarea bind:value={newReason} rows="2" placeholder={m('creditMemos.createModal.reasonPlaceholder')}></textarea>
		</label>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (showCreate = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={saving}>{saving ? m('common.saving') : m('creditMemos.createModal.create')}</button>
		</div>
	</form>
</Modal>

<Modal
	open={applyTargetId !== null}
	ariaLabel={m('creditMemos.applyModal.aria')}
	title={m('creditMemos.applyModal.title')}
	width="sm"
	onclose={() => (applyTargetId = null)}
>
	{#if applyMemo}
		<!-- The subject of the action, kept on screen while the dialog covers the
		     row it came from: which credit, how much, and whose. All three are
		     memo data, so none of it is copy. -->
		<p class="modal-subject">
			<span class="mono">{applyMemo.memo_number}</span>
			<Money amount={applyMemo.amount} currency={applyMemo.currency} />
			<span class="modal-subject-vendor">{applyMemo.vendor_name ?? '—'}</span>
		</p>
	{/if}
	<p class="modal-hint">{m('creditMemos.applyModal.hint')}</p>
	<form onsubmit={(e) => { e.preventDefault(); handleApply(); }}>
		<label>
			<span>{m('creditMemos.applyModal.invoice')} <em class="required">*</em></span>
			<select bind:value={applyInvoiceId} required>
				<option value="">{m('creditMemos.applyModal.selectInvoice')}</option>
				{#each invoicesForVendor as inv}
					<option value={inv.id}>{inv.invoice_number} — {inv.vendor}</option>
				{/each}
			</select>
		</label>
		{#if invoicesForVendor.length === 0}
			<!-- An empty select has two causes and only one of them is "there is
			     nothing to credit". Saying the vendor has no eligible invoice when
			     the invoice fetch simply failed sends the operator off to re-save
			     a vendor link that was never the problem. -->
			<p class="modal-hint warn">
				{invoicesErrored ? m('common.loadFailed') : m('creditMemos.applyModal.noEligible')}
			</p>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (applyTargetId = null)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={applying || invoicesForVendor.length === 0}>{applying ? m('creditMemos.applyModal.applying') : m('creditMemos.applyModal.apply')}</button>
		</div>
	</form>
</Modal>

<style>
	/* Page-specific bits not covered by the global design-system CSS in app.css. */
	/* An applied or voided memo is de-emphasised by the shared `.row-muted`
	   recipe in app.css (a muted colour token). It used to be `opacity: 0.6` on
	   these cells, which composited the row's date + invoice-number cells
	   (--text-muted) to 2.77:1 and its status badge to 2.59–2.91:1 on --surface.
	   `.applied` / `.void` stay as the semantic classes — `tr.applied` is an e2e
	   selector (tests-e2e/credit-memos/credit-memos.spec.ts) — and now carry no
	   colour of their own. */
	/* Explains an empty apply-target list — the memo's vendor has no invoice
	   whose vendor link is resolved and matching, so there is nothing to credit. */
	/* Sub-label under the currency select. Muted on `--surface` clears 4.5:1;
	   do NOT add `opacity` here — the token has already done that job and a
	   fade only spends contrast (see frontend/CLAUDE.md § Colour tokens). */
	.field-hint {
		display: block;
		margin-top: 4px;
		font-size: 0.78rem;
		color: var(--text-muted);
		font-weight: 400;
	}

	.modal-hint.warn {
		color: #d4940a;
		margin: -6px 0 0;
	}

	/* Free-text, so it is the one cell that can be arbitrarily long. Capped and
	   ellipsised (the full text is on the `title`) so one verbose reason can't
	   set the column width and push the money/status columns off the row —
	   ragged columns cost more scanability than the reason buys. No colour of
	   its own: `.muted` in app.css owns that, and `.row-muted` overrides it on
	   an applied/voided row. */
	.reason {
		max-width: 22rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* The Apply dialog's subject line — memo number, amount, vendor. `--text`
	   rather than a muted token: this is the fact the operator is checking, not
	   a caption. */
	.modal-subject {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 4px 10px;
		margin: 0 0 10px;
		font-size: 0.92rem;
		font-weight: 600;
		color: var(--text);
	}
	.modal-subject-vendor {
		font-weight: 400;
		color: var(--text-muted);
	}
</style>
