# UI patterns and design system

The full pattern library for the SvelteKit app — page layout, `DataTable`,
sortable headers, `SearchBox`, `BulkBar`, pagination, the request sequencer,
status-filter chips, modals, per-row actions, clickable rows, class-name
conventions, accessibility patterns and the colour tokens.

Extracted from `frontend/CLAUDE.md` so that file stays small enough to load
cheaply into every conversation that touches the frontend. **Read the relevant
section here before building or restyling a page** — guard rail 9 (reusable
components) is enforced against these patterns, and the accessibility and colour
sections carry WCAG 2.2 AA obligations that regression tests in
`frontend/tests-e2e/a11y/` will fail you on.


Reuse these patterns instead of inventing new ones. Reach for the
existing component first; only deviate with a written justification.

### Page layout

Wrap every authenticated route in **`<PageHeader title="…">`**
(`#lib/components/ui/PageHeader.svelte`) — it renders the `.workspace`
shell, the `.toolbar` header with the `<h1>` title, and an optional
`{#snippet actions()}` for right-aligned primary actions (e.g.
`+ Invite User`, `+ Upload Invoices`). The page body goes in `children`.
Don't hand-roll `<div class="workspace"><header class="toolbar">` any
more. The shell still produces this layout:

```css
.workspace {
    max-width: 1800px;
    margin: 0 auto;
    padding: 24px 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-height: 100vh;
}
```

This is what produces the consistent left/right gap between sidebar
and content across pages — do not change `max-width` or `padding`
per-route. A new route must use these exact values. The 1800px cap
is wide enough for grid pages on 1920–2560px monitors without leaving
half the viewport empty; on a 13″ laptop the natural body width
constrains it before the cap kicks in.

**Data that lands after first paint must not move what is already drawn.**
A page mounts, draws its shell, and fills in from responses that arrive a
round trip later. Anything those responses *add above the table* drops the
table under the reader on every visit — on a deployed backend the round trip
is long enough to see, and it reads as "a glitch whenever I change pages".
So:

- **Draw the slot from first paint.** A KPI / summary strip renders its cards
  with the shared no-figure dash (`kpiDisplayValue`, or `<KpiCard>` with a
  nullish `value`) and `aria-busy` while pending, never `{#if summary}` around
  the whole strip (`/payments`).
- **A chip row whose roster comes from a response holds its place while that
  response is pending** — render it with just its "All" chip until the roster
  lands (`/exceptions`' type row).
- **Don't put data-sized controls inline beside fixed ones on a wrapping row.**
  `/invoices`' status chips depend on the active workflow and their width on
  the tallies; inline beside the search box the row fitted on one line at
  mount and wrapped once they landed. They take a line of their own
  (`flex-basis: 100%`). Likewise a `<select>` filled from a response gets a
  fixed `width`, since a select sizes itself to its widest option.

`tests-e2e/reactivity/layout-stability.spec.ts` holds the late responses
behind a gate and asserts the table (or tab bar) sits at the same `y` before
and after — add the page there when you put something above a table that
loads.

### Navigating a long page

Three primitives, and which one a page gets depends on **why its sections share
the page** — not on how long the page is. `/payments` is the longest route file
in the tree and needs none of this; it has three sections and a table.

| Sections are… | Treatment | Reach for |
|---|---|---|
| **Alternatives** — you came to change exactly one | One panel at a time, `?section=<slug>` in the URL | `ui/SettingsRail.svelte` |
| **One continuous text** — you came to read or cite it | All of it rendered, with a contents page over it | `lib/legal/LegalPage.svelte` derives its own |
| **Stages of one workflow** | Leave them stacked — the scroll is the narrative | nothing |

Roughly: **eight or more unrelated sections** earns panels, **a document of any
length** earns a TOC, and below about **seven coherent** sections neither is
worth the indirection. `/organization` (fifteen panels in five groups) and
`/profile` (seven, flat) are the worked examples; reasoning in
`docs/decisions.md` §205.

Do not reach for panels on a document. Hiding clauses behind a picker breaks
Ctrl-F across the text, printing, and citation by section — which is most of
what a legal document is for.

`SettingsRail` is distinct from the two navigation components it sits beside,
and picking the wrong one is the common mistake:

- `layout/SectionTabs.svelte` — between **routes** in a nav group, driven by
  `lib/nav.ts`. Not for sections of one page.
- `ui/Tabs.svelte` — horizontal `role="tablist"` over **local view state**, for
  a few views of one dataset (`/expenses`, `/payments`, `/audit`). Buttons, not
  addresses.
- `ui/SettingsRail.svelte` — between **sections of one page**, with the section
  in the URL. Anchors + `aria-current`, so it owns no arrow-key contract.

Five things a panelised page has to get right:

1. **The section goes in the URL**, like every other filter/sort/selection state
   (see the rules above). An unrecognised slug falls back to the default rather
   than rendering an empty page. A slug is an address — it reaches bookmarks and
   docs, so renaming one is a breaking change, and any anchor the page was
   previously navigated by keeps resolving. Resolve a legacy anchor **inside the
   derivation** (query first, then the anchor map) rather than by rewriting the
   URL in an effect: `replaceState` is documented to throw before the router has
   initialised, so a rewrite puts a router-timing precondition on the path that
   most needs to be reliable. Deriving it needs neither the import nor the
   effect.
2. **Panel-specific fetches become lazy** — fire them when their panel is first
   shown, `once`-guarded so returning does not refetch, and keep any existing
   role gate. Leave genuinely page-wide reads eager. This is most of the value:
   `/organization` went from seven requests on arrival to two.
3. **Field state stays at page level.** Only the markup is conditional, so an
   unsaved edit survives a panel switch and needs no confirm-on-leave dialog.
   Moving state into per-panel components silently breaks that.
4. **`grid-template-columns: <rail> minmax(0, 1fr)`**, never `1fr`. A grid item's
   default `min-width: auto` is its content width, so the widest form in any
   panel pushes the track past the viewport and scrolls the document sideways
   (1.4.10). Pair it with `align-items: start` — a stretched item fills the row
   and leaves a sticky rail nothing to stick within.
5. **A rail group label is not a heading.** The panels use `<h2>` for their own
   titles, so a heading in the rail interleaves with the outline a screen-reader
   user navigates by. `SettingsRail` hides the label from assistive tech and
   re-attaches it as the list's accessible name.

### Data tables (`DataTable`)

Use **`<DataTable>`** (`#lib/components/ui/DataTable.svelte`) for every
grid page instead of hand-rolling `<div class="grid-container"><table>`:

```svelte
<DataTable columns={COLUMNS} isEmpty={items.length === 0} empty="No items.">
    {#snippet body()}
        {#each items as item (item.id)}
            <tr class:row-selected={selected.has(item.id)}>
                <td>…</td>
                <td class="actions"><RowAction …>Edit</RowAction></td>
            </tr>
        {/each}
    {/snippet}
</DataTable>
```

- `columns = [{label?, class?}]` builds the `<thead>`. For a select-all
  checkbox or sortable headers, pass `{#snippet header()}<tr>…</tr>{/snippet}`
  + `colspan={N}` instead of `columns`.
- The `body` snippet renders the rows; the page keeps full control of
  `<tr>`/`<td>` markup + classes (so bespoke cell styling stays page-scoped).
- `isEmpty` + `empty` render the centred `td.empty` row, tagged
  `data-testid="table-empty"` so e2e specs can assert *which* empty state is
  showing.
- **`empty` must distinguish loading / errored / genuinely-empty.** A list that
  renders its "nothing here" copy while a fetch is in flight — or forever after
  a failed one — is asserting something it never established. Compose it:
  `empty={loading ? m('common.loading') : errored ? m('…empty.errored') : normalEmpty}`,
  and set both flags in the fetch's `try`/`catch`/`finally`. `/notifications`,
  `/exceptions` and the dashboard are the reference implementations; on
  `/exceptions` in particular the empty copy ("Everything looks good!") is a
  claim about open fraud flags and compliance holds, so getting this wrong is a
  correctness bug, not a polish one. **When the fetch lives in a store, the
  store owns the flag** — `invoiceStore` / `paymentStore` / `contractStore` /
  `expenseStore` each expose `errored`, set in the loader's `catch` under
  `isCurrentRequest` (the same rule the `loading` flag uses) and cleared on the
  next success. The loader still **re-throws**, so a caller that awaits a
  refresh keeps its own handling (`/invoices`' post-upload "Uploaded, but the
  list could not be refreshed" toast is exactly that, and
  `tests-e2e/invoices/upload-refetch-failure.spec.ts` guards it).
  `tests-e2e/reactivity/list-load-failure.spec.ts` stubs a 500 on all four
  lists and asserts the error copy, not the "nothing matched" copy.
- Opt-in `fixed` (`table-layout: fixed`, pair with `<th>` widths) and
  `stickyHeader`. These two MUST be props (they target DataTable-owned
  `<table>`/`<thead>`, which a page-scoped selector can't reach).
- **A `fixed` table needs widths, or every column gets an equal share.**
  Before this rule, `/vendors` gave its checkbox column as much room as Email,
  and a long address ran under the Status badge. Give the predictable columns
  (checkbox, codes, badges, counts, actions) fixed widths sized to their widest
  content, and leave the one or two text columns without one, so they share
  the rest. Add a `min-width` on the table so those columns stay readable; past
  it, the table scrolls inside its region. A long single-line value
  (email, code) gets `overflow: hidden; text-overflow: ellipsis; white-space:
  nowrap` plus a `title` with the full value. Reference: `/vendors`, whose
  widths are scoped under a page wrapper because `SortableHeader` renders the
  `<th>` from another component.
- **Never make a `<td>` / `<th>` `display: flex` (or grid).** It stops being a
  table cell: the column no longer sizes it, so content overflows the table,
  and its border drifts out of line with the row. Lay out inline children with
  margins (the global `td.actions` does), or flex an element *inside* the cell.
  Guard: `src/lib/a11y/tableCellDisplay.test.ts`.
- `ariaLabel` names the scroll region (see § Accessibility patterns →
  DataTable); omit it and the generic `common.tableRegion` applies.

### Column sort (`SortableHeader`)

`#lib/components/ui/SortableHeader.svelte` renders one clickable, sortable
`<th>` for use inside a `DataTable`'s `header` snippet (see the note in
*Data tables* above). Pairs with the pure `#lib/utils/sort.ts::toggleSort`
helper — click an inactive column to sort it ascending, click the active
one again to flip direction:

```svelte
<script lang="ts">
    import SortableHeader from '#lib/components/ui/SortableHeader.svelte';
    import { toggleSort, type SortOrder } from '#lib/utils/sort.ts';

    let sortField = $state<string | null>($page.url.searchParams.get('sort'));
    let sortOrder = $state<SortOrder>(($page.url.searchParams.get('order') as SortOrder) ?? 'desc');

    function handleSort(field: string) {
        const next = toggleSort({ field: sortField, order: sortOrder }, field);
        sortField = next.field;
        sortOrder = next.order;
        syncUrl(); // fold sort into the page's existing URL sync, or a dedicated syncSortUrl()
        store.fetch(buildParams());
    }
</script>

<SortableHeader field="amount" label={m('…col.amount')} active={sortField === 'amount'} order={sortOrder} onsort={handleSort} />
```

- The backend validates `sort=` against a per-endpoint allowlist
  (`backend/app/api/sorting.py`) — an out-of-list value is a 422, so only
  pass field keys the endpoint actually declares.
- `null` field = the backend's own default order; only send `sort`/`order`
  in `buildParams()` when `sortField` is set (`untrack()` the read the same
  way `search` is — `buildParams()` is called from filter effects too).
- Persist the choice to the URL the same way the page's other filter state
  is persisted (mirror `/expenses`' `syncUrl()`, or a page-local
  `syncSortUrl()` when the page has no existing filter→URL sync).
- Shipped on `/invoices`, `/vendors`, `/payments` (History tab), `/expenses`,
  and `/contracts` — the five primary list pages — and on `/exceptions`
  (Sev, Age, Due).
- Also on `/credit-memos` (`memo_number` / `amount` / `issued_date`, issue #443).
- **`aria-sort` states the order of the values the column SHOWS**, which is not
  always the order of the key sent. `/exceptions`' Age column sorts
  `created_at`, but the oldest row has the largest age, so ascending age is
  `created_at desc`: the page passes `SortableHeader` the flipped order and
  maps its clicks back (`handleAgeSort`). Any column rendered as elapsed time
  over a timestamp key needs the same flip.
- **A sort can always be cleared.** Pass `onclear` and the sorted column
  shows a × ("Clear sort by Vendor") that returns the list to the default
  order. Write each page's `handleClearSort` as a copy of its `handleSort`
  with `clearSort()` in place of `toggleSort()`, so it keeps that page's
  exact URL sync and refetch path. All seven sortable lists pass it, and a
  new one must too: a sort that can only be flipped has no way back.
  `<th>` carries `aria-label={label}`, so the × isn't read into every
  cell's header. In specs, pick the sort button by name inside a header
  (`.getByRole('button', { name: label, exact: true })`). The sorted column
  holds two buttons. Guard: `tests-e2e/invoices/sort.spec.ts`.

### Search (`SearchBox`)

Pill-shaped search input with a magnifier-glass SVG. Single component:

```svelte
<script lang="ts">
    import SearchBox from '#lib/components/ui/SearchBox.svelte';
    let search = $state('');
</script>

<SearchBox
    bind:value={search}
    placeholder="Search invoices..."
    ariaLabel="Search invoices"
/>
```

- Debounce search before fetching (250–300ms is the convention; see
  `routes/admin/+page.svelte` and `routes/invoices/+page.svelte`).
- Server-side filter via `?search=` param. Backend uses ILIKE on the
  most natural fields for that entity (e.g. name + email, or
  invoice_number + vendor_name).
- Clearing the input must re-fire the request without `?search=`,
  not just visually clear.
- **Never filter the loaded rows instead.** A client-side `.filter()` over
  what the page has already fetched silently hides every match living on a
  later page — the user searches and is told nothing matched. `/expenses` and
  `/requisitions` both shipped that way and needed an honest "searched only
  the N rows loaded so far" empty state to avoid lying; the fix was the
  backend `search` leg, not better copy. If the endpoint has no `search`
  parameter yet, add it — don't approximate it in the browser.
- **A term that hits the network needs the same discipline as a chip**:
  debounce, the page/store `createRequestSequencer` (see below), and a record
  of the term the newest issued request carried. The two pages above keep an
  `appliedSearch` `$state`, written where the request is issued and read by
  the debounce effect, which schedules nothing when the term already matches
  it. That is what stops the effect's FIRST run (mount, including a
  bookmarked `?search=`) firing a duplicate load 300ms behind the status
  effect's — and it cancels a pending debounce when a chip click has already
  loaded with the typed term.
- **Read `search` via `untrack(() => search)` inside the loader / params
  builder.** Any function the status-filter `$effect` calls *synchronously* is
  still inside that effect's tracking scope — Svelte registers reads
  transitively — so a plain read there makes the status effect depend on the
  term and every keystroke fires its own immediate request. `untrack` still
  reads the live value; it only stops the read becoming a dependency. This is
  issue #168, and it has now been reintroduced twice through a *different*
  function than the one previously fixed (`syncUrl` first, then the loader),
  so treat it as a property of the call site, not of one function:
  `routes/vendors/+page.svelte` is the reference. A `fill()`-based e2e cannot
  catch it — one state write, one term, and it passes either way. Guard it by
  typing: `pressSequentially` inside the debounce window, assert nothing
  fired, then exactly one request for the final term
  (`tests-e2e/{requisitions,expenses}/search-scope.spec.ts`, and the
  parameterized `tests-e2e/reactivity/search-debounce-race.spec.ts`).
- Do NOT re-implement the search-box markup inline. If you find
  yourself writing `<svg ...><circle .../><path .../></svg>` next to
  an `<input>`, you are diverging from the pattern.

### Bulk selection (`BulkBar` + `BulkDeleteButton`)

Floating, fixed-position bar at the bottom of the viewport that
appears when one or more rows are selected:

```svelte
<script lang="ts">
    import BulkBar from '#lib/components/ui/BulkBar.svelte';
    import BulkDeleteButton from '#lib/components/ui/BulkDeleteButton.svelte';

    let selected = $state<Set<string>>(new Set());
</script>

<BulkBar count={selected.size} onclear={() => (selected = new Set())}>
    {#snippet actions()}
        <BulkDeleteButton
            onconfirm={handleBulkDelete}
            disabled={busy}
            label={m('things.bulk.delete', { n: selected.size })}
            confirmLabel={m('things.bulk.confirmDelete')}
        />
        <!-- additional .bulk-action-btn buttons go here -->
    {/snippet}
</BulkBar>
```

**Required behaviours:**
- Selection lives in a `Set<string>` keyed by row id.
- Header checkbox toggles select-all over the *selectable* subset
  (e.g. excluding the current user, the default workflow, or
  immutable-status invoices). Items that can't be selected render
  their `<td class="checkbox-col">` empty rather than disabled.
- Delete is always armed-confirm (one click arms; outside-click or
  second click un-arms or commits). `BulkDeleteButton` does this.
  Pass `confirmLabel` (the armed label) from the catalogue too — its
  fallback, `"Confirm " + label`, is English-only. `title` sets a native
  tooltip; wrap the button in a `<span title>` instead when the tooltip
  must show while it is `disabled` (`/invoices` does). Never restyle a
  local `.bulk-delete-btn` copy — `/invoices` carried one until it drifted.
- Bulk endpoints return a partial-success shape — `{deleted: [],
  failed: [{id, reason, ...}]}` — and the page surfaces the per-row
  reason in a toast. See `bulk_delete_users` in `backend/app/api/admin.py`
  for the canonical contract.

**The one exception:** `/payments` queue uses a non-floating
`<div class="pay-bar">` because it's a payment-run *builder*
(selection drives the next step's UI, not row actions). Don't copy
this pattern elsewhere. Its "select all N matching" resolves the whole
selectable set via `GET /api/payments/queue/ids` (which also returns a
per-currency money breakdown, so the pay-bar's subtotals + mixed-currency
guard stay honest without holding every row); while in matching mode the
prune effect is skipped and the money totals come from that backend
breakdown, not the loaded page — mirrors `/invoices`' `selectedAllMatching`.

Shipped on `/invoices`, `/expenses`, `/vendors` (bulk verify/reject via
`POST /api/vendors/bulk/status`, bulk re-screen via `.../bulk/screen`, CSV
export via `.../bulk/export`; gated to `vendor.manage`) and `/contracts`
(bulk activate/terminate/cancel via `POST /api/contracts/bulk/status`,
routed through the same `_transition` helper the single-row lifecycle
buttons use; CSV export) and the `/payments` **Queue** tab (payment-run
builder, `GET /api/payments/queue/ids`) — the "select all N matching"
affordance on each resolves the whole filtered set via that resource's
`GET .../ids` sibling endpoint (`getVendorIds`/`getContractIds`/`getExpenseIds`)
rather than only the currently-loaded page.

### Pagination + Load more

Default page size is **20** across all list endpoints. Backend
returns `{items, total, page, page_size}`; the front-end renders the
items, then a centred Load More button below the table:

```svelte
{#if store.hasMore}
    <div class="load-more-row">
        <button class="btn-load-more" onclick={loadMore} disabled={store.loading}>
            {store.loading ? 'Loading…' : `Load more (${store.items.length} of ${store.total})`}
        </button>
    </div>
{:else if store.total > 0}
    <div class="load-more-row">
        <span class="load-more-end">Showing all {store.total} <thing>s</span>
    </div>
{/if}
```

- Append, don't replace. `loadMore` issues `page=N+1` and appends the new
  items **via `appendUnique` (`#lib/utils/pagination.ts`)** — never a raw
  `[...existing, ...res.items]` spread. Offset pagination can re-surface a
  row when the underlying set shifts between fetches (a new row inserted, a
  notification arriving), and a duplicated id crashes the keyed
  `{#each ... (id)}` with Svelte 5's `each_key_duplicate`. `appendUnique`
  drops incoming duplicates (existing row wins, order preserved); every
  load-more site — the list stores and the inline route/component loaders —
  uses it.
- "Showing all N" is the empty-string-of-pagination state — confirms
  for the user that they've reached the end. It is **only ever rendered
  behind `{:else if total > 0}`**, never on its own: `total` is the
  server's count of the whole filtered set, so a list that asks for one
  capped page and then states "Showing all {total}" is asserting that
  rows it never fetched do not exist. Six lists (budgets, intake,
  catalogs, requisitions, and the `/expenses` Reports + Cards sub-lists)
  shipped that way — 50 rows under a footer reading "Showing all 87",
  with no control to reach the other 37. `src/lib/utils/pagedListFooter.test.ts`
  is the guard: any file referencing a `<list>.showingAll` message must
  also reference the matching `<list>.loadMore`.
- Stores expose `total`, `page`, `hasMore`, and any mutating actions
  (create / delete / bulk-delete) keep `total` in sync without a
  refetch.
- **A list page over an unpaginated endpoint gets a plain count, not this
  footer.** `GET /api/gl-accounts` is the one deliberate exception to the
  envelope (a bounded reference collection whose pickers need every row — see
  its docstring and `backend/tests/test_pagination.py::test_gl_accounts_stays_unpaginated`),
  so `/gl-accounts` renders `m('glAccounts.count', { n })` in a `.count-row` /
  `.count-line` and has no Load-more control at all. That is not the defect the
  `showingAll` rule is about: the rows on screen ARE every row matching the
  active filters, so no claim is being made about rows that were never fetched.
  Note what follows for the message keys — such a page must NOT mint a
  `<list>.showingAll`, since `pagedListFooter.test.ts` would then (correctly)
  demand a `<list>.loadMore` for a page that has nothing more to load.

### Sequencing list fetches (`createRequestSequencer`)

Every list surface that can have a request in flight while something else
changes the list wires **`createRequestSequencer()`**
(`#lib/utils/requestSequence.ts`). This is now the whole app, not a handful of
pages: the list stores (`invoices`, `payments`, `contracts`, `expenses`,
`notifications`, `admin`, `workflows`), the list routes (`vendors`,
`vendors/screening`, `discounts`, `positive-pay`, `recurring`, `budgets`,
`intake`, `requisitions`, `catalogs`, `vendor-statements`, the four sub-lists
on `expenses`, the `workflows/[id]` builder canvas), the **detail-modal /
drill-down fetches** that a second click can re-issue (`/goods-receipts`'
receipt detail + its inspections — the reference implementation;
`/vendors/screening`'s screening-history timeline; `/audit`'s per-invoice
signature drill-down; `/admin/api-keys`' per-key usage; `/experiments`' results
readout), `InvoiceModal`'s line-item editor, and the three **analytics** surfaces whose loads are keyed
off a control rather than a filter — `/cfo` (its `$effect` fired three
unsequenced requests per keystroke on the two free-text money inputs, so the
cash-position curve and the "below minimum balance" breach banner could settle
on the figures for a *prefix* of what the field showed), `CfoMetrics` (the
30/90/180/365 horizon buttons) and `/tax` (the 1099 year selector). **A new
list surface wires it too** — don't hand-roll a second mechanism, and don't
leave it out because the page "only" edits a row after the first load has
landed (see the create/prepend note below). "Not a list" is not an exemption
either: any state written from a response that a control can re-issue needs
it, and a free-text control needs the debounce beside it (issue #168 /
`docs/decisions.md` §53 — anything an effect calls synchronously is inside its
tracking scope, so `load()` must `untrack` the free-text reads).

It answers two separate questions about a response — and takes one call that
retires in-flight requests. Conflating the two questions is a bug both ways:

```ts
const fetchSequence = createRequestSequencer();

async function fetch(params) {
    const token = fetchSequence.start();   // synchronously, before firing
    loading = true;
    try {
        const res = await api.get(`/api/things?${qs}`);
        if (!fetchSequence.canCommit(token)) return;   // stale → discard
        things = res.items;
    } finally {
        // NOT canCommit — see below.
        if (fetchSequence.isCurrentRequest(token)) loading = false;
    }
}
```

- **`canCommit(token)`** — may this response be written into state? False
  once a later `start()` has happened (the classic "search `acm` resolves
  after `acme`" race) **or** once a local edit superseded it.
- **`isCurrentRequest(token)`** — is this still the newest request I
  issued? Use it in the `finally` for the `loading` flag and for any
  load-error toast. Reading `canCommit` there leaves the spinner stuck on
  forever after a local edit, because no newer request exists to clear it.
- **`wasSupersededByEdit(token)`** — did a *local edit* retire this request?
  The third question, and only a **write** asks it. A save that PUTs the list
  and then re-reads it takes a token too, but its post-condition is narrower
  than a read's: only an edit invalidates what it sent. Reading `canCommit`
  there makes an unrelated newer read look like a conflict — which is how
  `InvoiceModal.saveLineItems` first shipped, leaving its dirty flag (and so
  the Save button) stuck on whenever an extraction poll's own reload landed
  mid-save.
- **`supersedeInFlight()`** — call it **immediately before** any helper
  that edits the list in place with no fetch of its own
  (`invoiceStore.update` / `patchLocal`, the vendors page's
  `applyVendorUpdate`). Without it the counter never moves, so an
  already-in-flight fetch resolves afterwards holding a pre-edit snapshot
  and silently reverts the edit — a user watching their approve, or the
  payment block they just lifted, undo itself. Requests issued *after* the
  edit are unaffected; they read server state that already includes it.

The superseded response is discarded, never merged — see
`docs/decisions.md` §23 for why re-applying the edit on top of it isn't
sound. A store with no local-mutation helper (every mutation re-fetches
through the sequencer, like `paymentStore`) needs no `supersedeInFlight`
call; say so in a comment rather than leaving the next reader to derive it.

Three things the sweep across the app settled, worth not re-deriving:

- **A create/prepend path needs no existing row.** "The mount fetch must have
  landed before there's a row to mutate" closes the race for edit and delete
  but *not* for New/Add, which is live while the first GET is still out. Every
  `upsert()` that can prepend an unseen row — `createUser`, `createFromTemplate`,
  a generated Positive Pay file — supersedes for that reason alone.
- **One sequencer per independent list, never one shared counter.** A page or
  store holding several lists (the `admin` store's users vs roles, the
  `notifications` store's list vs its 60s unread-count poll, `expenses`' four
  tabs, `discounts`' offers vs KPI dashboard) gives each its own. Sharing one
  would let an unrelated request mark another list's in-flight response
  un-committable and blank it. A local edit that writes state BOTH lists load
  (a mark-read, which moves `unread`) supersedes both.
- **A detail modal is a list of one, and it is the dangerous case.** "Open A,
  close it, open B" re-issues the same request with a different subject, so an
  unguarded fetch renders A's data under B's name — indistinguishably, because
  the heading comes from the click and the body from the response. Every such
  fetch takes its own sequencer, and the panel carries BOTH ids in the DOM (the
  subject it was opened for, and the id the response claims) so a mismatch is
  assertable rather than inferred: `verify-drill`'s `data-invoice-id` /
  `data-report-for`, `experiment-results`' `data-experiment-id` /
  `data-results-for`, `api-key-usage`'s `data-key-id` / `data-usage-for`,
  `screening-history`'s `data-vendor-id`. On
  `/vendors/screening` this was not cosmetic — the modal also **acts**
  (Block/Unblock payments), so a reviewer read vendor A's sanctions timeline
  while the control blocked vendor B. Its actions now capture the vendor at
  click time and everything after the first await reads that capture, never the
  live `selected`; `busy` is keyed to that vendor too, so an action on one
  vendor can't disable another's controls. Guards:
  `tests-e2e/vendors/screening-modal-identity.spec.ts`,
  `tests-e2e/audit/signature-drill-identity.spec.ts`,
  `tests-e2e/experiments/results-identity.spec.ts`, and the usage-identity case
  in `tests-e2e/admin/api-keys.spec.ts` — each drives the race with a held
  response released by hand, never a sleep.
- **An editor over a fetched list is the same surface.** The `workflows/[id]`
  canvas and `InvoiceModal`'s line-item table hold unsaved user edits, so a
  load resolving mid-edit doesn't just revert a row — it wipes work while the
  dirty flag stays set on something the user can no longer see. Both route
  every edit through a `markDirty()` that supersedes first.

**Related, and the other half of the same bug:** a filter `$effect` that calls
a `buildParams()` / `syncUrl()` helper reading `search` ends up depending on
`search` (Svelte tracks reads transitively through called functions), so every
keystroke fires an immediate un-debounced load *alongside* the debounced one.
Read `search` via `untrack(() => search)` in the params-builder, and untrack
`syncUrl()` wholesale — it is a writer of URL state, never a dependency
source. `tests-e2e/reactivity/search-debounce-race.spec.ts` is the guard.

**A debounce `$effect` must return its own teardown.** A `$effect` that arms a
timer and returns nothing leaves it armed when the component is destroyed, so
the callback runs against a page the user already left: `syncUrl()` rewrites
the address bar (SvelteKit's `replaceState` doesn't care which route is
mounted), and a list-store reload writes a pre-navigation snapshot into a
module-level store the *next* page shares. Always close the effect with

```ts
return () => clearTimeout(searchTimer);
```

Svelte also runs that teardown before each re-run, so it subsumes the
`clearTimeout` at the top of the body rather than fighting it. Enforced across
the tree by `src/lib/utils/effectTimerCleanup.test.ts`, a source scan that
fails any `$effect` body containing `setTimeout(` / `setInterval(` without a
matching `return () => clear…` — a deliberate static guard, because the
symptom only shows inside a sub-second window that no non-flaky e2e can pin.

**A debounce `$effect` must also skip its own mount-time run.** A `$effect`
runs once at mount whether or not `search` changed, and the page's load effect
has already fetched the list by then — so an unguarded `search;
debouncedFetch()` loads it a second time 300ms later on every visit (it did on
`/invoices`, `/vendors`, `/payments` and `/bank-reconciliation`), flipping the
page's loading state while the reader is looking at the finished table. Open
the body with the first-run skip:

```ts
let searchEffectRan = false;
$effect(() => {
    search;
    if (!searchEffectRan) {
        searchEffectRan = true;
        return;
    }
    debouncedFetch();
    return () => clearTimeout(searchTimer);
});
```

The same source scan enforces it: an `$effect` whose first statement is
`search;` and which arms a timer (or calls a `debounced…()` helper) must
`return;` before it does.

**Reading the URL once at mount is only half of the round trip.** SvelteKit
reuses the mounted component for a navigation within the same route — the
sidebar row clicked while on a filtered view, a `goto('/invoices?…')`, or
Back/Forward between two real history entries of the route — so state seeded
from `$page.url` at mount never sees it: the address bar changes and the view
does not. Give the page ONE reader (`readUrlState(params)`), seed from it at
mount, and call it again from `afterNavigate` when `from.route.id ===
to.route.id`, assigning only what changed so an unchanged filter doesn't
refetch (a sort change needs its own fetch — no effect tracks it). Shallow
`replaceState` writes never fire `afterNavigate`, so the single writer
(`syncUrl()`) cannot loop back through the reader. Shipped on `/invoices` and
`/exceptions`; guarded by `tests-e2e/invoices/url-state.spec.ts` and
`tests-e2e/exceptions/view-url-state.spec.ts` (§ same-route navigation).

### Status filter chips

Use **`<FilterChips>`** (`#lib/components/ui/FilterChips.svelte`) for the
pill-shaped status filter above the table:

```svelte
<FilterChips
    chips={[
        { key: 'all', label: 'All', count: total },
        ...STATUSES.map((s) => ({ key: s, label: m(STATUS_LABEL_KEYS[s]), count: statusCount(s) }))
    ]}
    bind:active={statusFilter}
/>
```

- `chips = [{key, label, count?, alert?}]`. Omit `count` for label-only
  chips; `alert: true` renders the red attention badge (`.count.alert`).
- The "All" chip comes first; the active chip uses `var(--accent-strong)` +
  white — **not** `var(--accent)`, which is only 3.12:1 against white. See
  *Colour tokens and contrast* below.
- **Single-select only.** For a multi-select status filter (e.g. `/invoices`,
  whose filter is an array) keep an inline `<nav class="filters">` chip
  nav — it still uses the global `.filter-chip` / `.count` CSS, so the
  visible text/counts (and the `/^All\s+\d+/` e2e selectors) stay identical.
- **Quick subset, not every status.** When the lifecycle has many statuses
  (`/invoices` has 12), the inline row shows only a small, high-traffic
  *quick subset* — the stages people triage daily (`new`, `ready_for_review`,
  `approved`, `failed`), gated by the active workflow. The **full** set lives
  in the Advanced Search modal. The two share **one** selection array: opening
  the modal seeds its Status section from the live chip selection, and Apply
  writes it back — so they never fight over `params.status` (do *not*
  reintroduce a second status param in `buildParams`). Any status selected only
  in the modal is appended to the rendered chips so an active filter is never
  invisible (`chipStatuses` = quick subset ∪ active). See
  `routes/invoices/+page.svelte` and `tests-e2e/invoices/advanced-status.spec.ts`.
- **Several chip rows over one table are faceted.** `/exceptions` stacks status,
  type and severity rows above a search box. Each row's counts honour every
  OTHER selected filter and the search term, but never their own row's
  selection — so every chip reads what the table would show if it were
  clicked, and the pressed chip in each row equals the table's total. The
  tallies come from one endpoint taking the list's filters
  (`GET /api/exceptions/summary`, `docs/decisions.md` §188); the page builds
  the list, summary and select-all params in one `filterParams()` so the three
  cannot describe different sets. A row whose roster is small and fixed
  (severity) renders every chip, zeros included — a chip that disappeared at 0
  would take its pressed state with it.

### Modals

Use **`<Modal>`** (`#lib/components/ui/Modal.svelte`) — backdrop +
centred dialog with backdrop-click + Esc to close:

```svelte
<Modal open={showCreate} ariaLabel="<Action>" title="<Heading>" width="sm" onclose={() => (showCreate = false)}>
    <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        <!-- labelled fields -->
        <div class="modal-footer">
            <button type="button" class="btn-cancel" onclick={() => (showCreate = false)}>Cancel</button>
            <button type="submit" class="btn-primary" disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        </div>
    </form>
</Modal>
```

- `ariaLabel` becomes the dialog's `aria-label` — **e2e specs select
  modals by this exact string**; never change a label on an existing modal.
- Keep the page's own `<form>` **including the `.modal-footer`** inside
  the children so submit still works (Modal does not own the footer).
- `width="sm|md|lg"` = 440/480/820px. Custom heading markup →
  `{#snippet header()}…{/snippet}` instead of `title`. If the body
  dereferences a nullable var, gate `open={x !== null}` and wrap the
  children in `{#if x}…{/if}`.
- Cancel sits left of the primary action. Required-field markers use
  `<em class="required">*</em>`.
- **A long dialog keeps its decisions out of the scroll.** When a dialog's body
  runs past one screen (the invoice dialog's right pane: summary, fields, line
  items, activity, chat, e-invoice), split the `<form>` into a scrolling region
  and an action footer **beside it in the flex column** — `form { display: flex;
  flex-direction: column; min-height: 0 }`, the body `flex: 1; min-height: 0;
  overflow-y: auto`, the footer `flex-shrink: 0`. Never `position: sticky` the
  footer over the content: a pinned strip hides the focused field under it
  (2.4.11) and intercepts Playwright's scroll-then-click (`docs/decisions.md`
  §181, § Shared visual layer below). The footer stays inside the `<form>` so
  Save still submits, holds every forward action (Cancel, Save, Submit, and
  Approve / Reject with the reject-reason flow), and caps itself at
  `max-height: 50%` with its own `overflow-y: auto` so a 320px dialog never
  squeezes the fields to nothing. A primary action disabled for a reason the
  reader can fix says so beside it, wired with `aria-describedby` (the invoice
  dialog's "Pick an approver to submit"). Reference:
  `components/modals/InvoiceModal.svelte` (`.form-scroll` / `.form-actions`);
  guards: `tests-e2e/invoices/review-verdict.spec.ts` (in view at 1280×720, no
  spill at 320px) and `approver-picker.spec.ts` (the hint).
- **Never hand-roll a modal shell.** Every dialog — including the feature
  dialogs in `#lib/components/modals/` — wraps its body in `<Modal>`. Do
  not write your own `.backdrop` / `div.modal[role="dialog"]`, your own
  Esc / backdrop-click handlers, or a `<svelte:window onkeydown>` to close
  — `Modal` already owns all of that, and a private copy drifts (the
  `AdvancedSearchModal` overflow + scrollbar bug came from a hand-rolled
  shell that never picked up the global `.modal` CSS). "Bespoke internals"
  means the dialog's *body* (custom field grids, pickers, footers) is
  feature-specific — the shell, backdrop, and close behaviour are not.
  If you find yourself writing `position: fixed; inset: 0` or
  `role="dialog"` in a component, stop and use `<Modal>`.

### Unsaved changes (`guardUnsavedChanges`)

A page with an editable form that can be left half-done calls the shared
guard **once**, during component init:

```svelte
import { guardUnsavedChanges } from '#lib/stores/unsavedChanges.svelte.ts';
guardUnsavedChanges(() => dirty && !saving);
```

While that returns true, any in-app navigation away is held, and the one
`UnsavedChangesDialog` (mounted in the root layout) asks **Stay on page**
or **Leave without saving**. That covers sidebar and tab links, `goto`, and
Back/Forward. Leaving replays the exact navigation, including the same
history step for Back. A reload, a tab close or a typed URL gets the
browser's own prompt, the only one browsers allow there.

- Never use `confirm()` or a page-local dialog for this. One dialog, one
  copy.
- Exclude an in-flight save from the predicate, so a save that redirects
  is never held.
- Users today: the workflow editor, `/admin/retention`, and the Report
  Builder (an edited saved report). Guard: `tests-e2e/unsaved-changes.spec.ts`.

### Per-row actions

Use the shared `<RowAction>` component (`#lib/components/ui/RowAction.svelte`)
for every per-row button across every grid page. Variants:
- `default` — neutral border, accent on hover (Edit, Apply, link buttons)
- `success` — green border + text (Verify)
- `danger` — neutral by default, red on hover; pass `armed` for the
  filled-red two-click confirm (Delete, Reject, Void)

Renders as `<a>` when given `href`, otherwise `<button>`. Never copy
the `padding: 4px 12px; ...` recipe inline — use the component.

An action the server would refuse for a reason the reader can act on is
rendered `disabled`, not hidden, with the reason as its accessible
description: pass `describedby` naming a `.visually-hidden` element that holds
the localized reason (and `title` for pointer users). The payment queue's
"Record as paid" is the worked example (decisions §319).

The actions cell is **always the last column** (right side of the row),
preceded by a header `<th class="actions-col"></th>`. The `<td>` uses
`class="actions"` with the standard left-aligned flex layout:

```css
.actions {
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
}
```

Buttons inside align left within the cell — do not use
`justify-content: flex-end`.

For the destructive armed-confirm pattern, outside-click un-arms by
adding a `<svelte:window onclick>` that clears `confirmDeleteId` when
the click target is not within `.row-action`. See
`routes/admin/+page.svelte` for the canonical implementation.

### Whether to offer an action: read the server's verdict (issue #575)

A page must not decide whether to show Approve, Execute, Void, Resolve, … by
**restating the endpoint's rule** — a status list, a role list,
`uploaded_by_id !== me`. Every such copy drifted from the server in its own
way: the uploader was offered an Approve that 403'd, a run's creator an Execute
that 403'd, an unverified vendor's invoice no way to be recorded paid at all
(#573), an `active` vendor no Reject that both endpoints accept.

Instead the backend ships a **verdict** beside the row, computed by running the
same gate function the endpoint raises from, so the two cannot disagree:
`null` when the endpoint would accept the caller, else the coded refusal
(`{code, message, params}`) it would answer with. Read it through
`#lib/api/actionVerdicts.ts`:

- `isAllowed(verdict)` — the control may be pressed (`verdict === null`).
- `isOffered(verdict)` — the control is rendered at all: false while the verdict
  has not arrived (`undefined` — **fail closed**) and for `role_not_permitted`,
  a reason the reader cannot act on.
- `refusalText(verdict, m)` — the reason, localized from its code through
  `api/codedRefusals.ts` (falling back to the server's English). Render it
  beside a disabled control when the reader can act on it ("a colleague must
  approve this").

Never add a second check beside the verdict — that is the copy it replaces.
Org-level configuration the server states elsewhere (the execution mode, read
once from `GET /api/payments/execution-mode`) is not a row property and stays a
separate, documented gate. A mutation response that lacks the verdict must not
be read as "allowed": carry the row's verdict only while what it judged is
unchanged, otherwise drop it until the next list read
(`routes/vendors/+page.svelte::applyVendorUpdate`).

Where each verdict lives (backend gate → field):

| Page | Field | Gate(s) the endpoint raises from |
|---|---|---|
| Payment queue | `record_outside_refusal` | `services/external_payment.record_outside_refusals` (#573) |
| Payment history | `void_refusal`, `release_refusal`, `dismiss_refusal`, `accept_settlement_refusal` | `payment_controls.payment_void_refusal`, `.compliance_hold_refusal`, `.settlement_accept_refusal` |
| Payment runs list | `actions.sync_erp` | `payment_controls.run_sync_erp_refusal` |
| Run dialog | `actions.{approve,execute,cancel,retry_failed}` | `payment_controls.run_{approve,execute,cancel,retry_failed}_refusal` |
| Run dialog (paying outside) | `actions.{record_outside,nacha_export,nacha_regenerate,nacha_void}` | `payment_controls.run_record_outside_refusal`, `.run_nacha_export_refusal`, `.run_nacha_void_refusal` |
| Invoice modal | `GET /api/invoices/{id}/review-actions` → `approve`, `reject` | `review.review_refusals` (the approve gates, on a scratch chain) |
| Invoice modal (Submit / Send to ERP, Retry ERP) | same read → `complete`, `retry_erp` (and `send_to_erp`) | `api/workflow.workflow_action_refusals`: `complete_status_refusal`, `complete_entry_refusal`, `erp_send.erp_send_refusal` (plan, status, ERP export gate). `invoice_not_completable` / `invoice_not_erp_retryable` hide the control (about the status); the required-fields check stays a form check |
| Exception queue | `decision_refusals.{resolve,dismiss,escalate}` | `api/exceptions.actionable_refusal` + `exception_lifecycle.segregation_refusal` |
| Credit memos | `actions.{apply,edit,void}` | `_assert_applicable` / `_assert_editable` / `_assert_voidable` |
| Vendors | `status_actions.{verify,reject}` | `api/vendors.vendor_status_refusal` |
| Bank-change queue | `approve_refusal` | `api/vendors.change_request_approve_refusal` |
| Expense reports (list + detail) | `actions.{submit,approve,reject,compose}` | `services/expense_controls.report_{submit,approve,reject,compose}_refusal` |
| Expense card transactions | `actions.{match,create_expense,unmatch,ignore}` | `services/expense_controls.card_txn_{link,unmatch,ignore}_refusal` |

Each is pinned by a backend parity test that, for every seeded role and row
state, asks both sides — the verdict and the real POST — and fails unless they
agree (`tests/test_payment_action_verdicts.py`,
`tests/test_queue_action_verdicts.py`, `tests/test_review_actions_parity.py`,
`tests/test_payment_queue_blocked.py`, `tests/test_expense_action_verdicts.py`).
A new verdict ships with one.

Which refusals a page *explains* beside a disabled control, rather than hiding
the control, is a presentation choice keyed on the verdict's `code` — never a
restated rule. The expenses page explains only `expense_cfo_required` (Approve
stays visible, disabled, naming the CFO gate) and hides every other refusal,
the reader's own report included.

### Clickable rows (`RowLink` + `isRowOpenClick`)

A list row that has a detail/edit destination opens it **by clicking the
row**, not via a separate "Edit"/"View" button. Two layers make this
accessible:

1. **Primary cell** (the id / name / number — `invoice_number`,
   `po_number`, workflow name, user name) wraps its content in
   **`<RowLink>`** (`#lib/components/ui/RowLink.svelte`). RowLink renders
   a real `<button>` (pass `onclick` — opens a modal) or `<a>` (pass
   `href` — navigates), styled to look like plain cell text but
   focusable, keyboard-operable, and announced by screen readers. This
   is the canonical, a11y-correct affordance — a table `<tr>` must keep
   its implicit `row` role (overriding it to `button` breaks column-header
   semantics), so the focusable control lives in the cell, not on the row.
   **Always pass a row-specific `ariaLabel`** (e.g. `Edit invoice INV-42`)
   — e2e specs select on it.

   **The `ariaLabel` must contain the control's visible text, as one
   unbroken run** (WCAG 2.5.3 Label in Name — axe's
   `label-content-name-mismatch`, enforced by every axe spec since
   axe-core 4.14). A row action reading "Compare routes" may be named
   "Compare routes for invoice INV-42", never "Compare payment routes for
   invoice INV-42": a voice-control user says what they see. Build the
   label from the visible string rather than retyping it — the catalogue
   template takes it as a parameter, so every locale keeps the match:
   `m('payments.quotes.openAria', { action: m('payments.quotes.open'), invoice })`
   with `'{action} for invoice {invoice}'`. Keep a badge or other extra
   text *beside* the link, not inside it, or it becomes visible text the
   label lacks (the `/workflows` "Default" badge).

2. **Whole row** carries `class="clickable"` + an `onclick` that opens
   the same destination, gated by **`isRowOpenClick(e)`**
   (`#lib/utils/rowNav.ts`). The guard bails when the click lands on a
   button, link, input, or the `.checkbox-col` / `.actions` cells — so the
   bulk-select checkbox and the kept **Delete** (and other per-row action)
   buttons still work. This is the Gmail/Linear "click anywhere except the
   controls" pattern.

```svelte
<tr class="clickable" class:row-selected={selected.has(row.id)}
    onclick={(e) => { if (isRowOpenClick(e)) editing = row; }}>
    <td class="checkbox-col"><input type="checkbox" … /></td>
    <td class="mono">
        <RowLink onclick={() => (editing = row)} ariaLabel={`Edit ${row.number}`}>
            {row.number}
        </RowLink>
    </td>
    …
    <td class="actions">
        <!-- no Edit/View button — the row opens the editor. Keep Delete: -->
        <RowAction variant="danger" armed={…} onclick={…}>Delete</RowAction>
    </td>
</tr>
```

- **Don't** add a separate Edit/View `RowAction` when the row is
  clickable — the row IS the affordance. Keep destructive / state-changing
  actions (Delete, Void, Verify, Activate…) as `RowAction`s in the
  `.actions` cell.
- **Don't** put `role="button"` / `tabindex` / `onkeydown` on the `<tr>`
  — that's the wrong fix (kills table semantics, conflicts with nested
  controls). The in-cell `RowLink` is the keyboard/AT path; the row
  `onclick` is a pointer-only enhancement.
- Lists with **no per-row detail view** (vendors, exceptions, credit
  memos, payments history/cards) keep their existing conditional
  `RowAction` buttons — there's no single "open" destination to wire.
- **Never `stopPropagation()` on a link's click.** SvelteKit's router
  catches link clicks by delegation higher up the document, so a stopped
  click falls through to the browser and reloads the whole app. RowLink's
  `<a>` used to do exactly that. The row handler doesn't need the help:
  `isRowOpenClick` already ignores clicks that start on an `<a>`. Guard:
  `tests-e2e/workflows/row-link-client-nav.spec.ts`.

### Contextual help (`HelpTip`, the page help link)

Every `PageHeader` already links to **How this page works**; a page needs no
wiring beyond its row in `lib/help/pages.ts`. Beyond that, put an ⓘ
`<HelpTip term="…" />` (decisions §244, `docs/help-centre.md`) where a reader
would reasonably ask "what does this mean?": AP jargon (three-way match,
settlement, Positive Pay), a control that is about to stop them (a CFO gate, a
payment-blocking exception, segregation of duties), a setting whose effect is
not obvious. A handful per page, not one per label. A page full of ⓘ icons is
noise, and the reader stops opening them.

Placement keeps every accessible name the app and the specs rely on:

- **Beside, never inside**, a `<label>`, `<button>`, `<a>`, `<legend>`,
  `<summary>`, a `SortableHeader`, or anything with an `onclick`. Inside, the
  tip's "About …" name would become part of that control's name, and the click
  would hit both.
- **A heading gets a row, not a child.** Wrap it in the global `.help-row`:
  `<div class="help-row"><h2>…</h2><HelpTip term="…" /></div>`. The heading's
  text is unchanged. Don't hand-write the flex row — it had grown ~20 private
  copies at drifting gaps before it was consolidated. `.help-row` zeroes the
  heading's margin so the tip centres on the text; spacing the heading used to
  carry moves to a local class beside it (`class="help-row po-match-title-row"`
  with only that margin). The reset is low-specificity on purpose, so a scoped
  heading margin still wins — zero it locally (`.card-title > h2 { margin: 0 }`)
  when the heading has one. A field label's row may set a tighter `gap: 4px`
  locally; nothing else about the row is local.
- **KPI cards and table columns take a prop**: `<KpiCard helpTerm="…">` and
  `columns={[{ label, help: '…' }]}`. A column tip joins the `<th>`'s
  accessible name ("Screening About Sanctions screening"), so don't add one to
  a column a spec selects with `exact: true`.
- **Not inside a `role="alert"` region.** An alert reads out its whole content,
  and the tip's "About …" label becomes part of the alarm. Put the tip next to
  the alert instead.
- A form field whose caption is a `<label>` gets the tip after the label
  element, in the same `.help-row`, never inside it.

`term` is a literal glossary id. When it varies, use an expression over literal
ids (`term={pm.match_type === '2-way' ? 'two-way-match' : 'three-way-match'}`).
`content.test.ts` reads every one, including `helpTerm=` and column `help:`
values, and fails on an id with no glossary entry or no guide to read next.

### App surfaces — depth, accent and motion (decisions §181)

The signed-in app shares one visual layer, set in `src/app.css` and the shell
components. A page inherits it by using the shared classes and primitives; a
page that restyles locally should reach for these tokens rather than literals.

| Token | Use |
|---|---|
| `--radius-sm` / `--radius` / `--radius-lg` | 8 / 12 / 16px: controls / cards and tables / dialogs and popovers |
| `--hairline` | a divider *inside* a card (row rules, footers) — quieter than `--border`, which outlines |
| `--shadow-card` / `--shadow-float` | resting cards and tables / dialogs and popovers |
| `--accent-glow` / `--accent-wash` | glows and hover washes derived from the tenant accent |
| `--ease-out` | the one easing curve for entrances and hovers |

Rules, each one a constraint this layer had to satisfy:

- **Accent effects derive from `var(--accent)`**, written as
  `color-mix(in srgb, var(--accent) N%, transparent)` or the two tokens above.
  `--accent` / `--accent-strong` are the tokens a white-label tenant overrides,
  so a glow typed as `rgba(99, 140, 255, …)` stays blue under a red brand.
- **Fills behind badges stay `--surface`.** Tables, modals and KPI cards keep
  it even where glass would look nicer, because every `<Badge>` tint pair is
  calibrated composited over `--surface`.
- **Accent never carries small text on the rail.** The sidebar's current page
  keeps its label on `--text` and puts the accent on the icon, an edge bar and a
  wash: a tenant's brand colour is chosen for a button, not for 4.5:1 as 14px
  text.
- **Primary buttons lift and glow on hover; they never lighten.** White on
  `--accent-strong` is calibrated at rest, and a brighter fill spends that
  margin. Chips change border and text on hover, never fill — a tint plus a text
  colour in one rule is `<Badge>`'s recipe and `badgeAudit` counts it.
- **Nothing in the shell is sticky or continuously animated.** A sticky strip
  intercepts the clicks Playwright scrolls to and hides focused controls (2.4.11);
  continuous motion would breach 2.2.2, which this product meets by never moving
  for more than five seconds rather than with a pause control (§ Motion, rule 4).
  The ambient background is a
  still, fixed pseudo-element; entrances are one-shot (modal rise, popover rise,
  tab indicator, empty-state illustration) and follow § Motion.
- **axe scans run at rest.** `tests-e2e/a11y/axe-helper.ts` emulates reduced
  motion before every scan, so a dialog caught mid-entrance cannot report (or
  hide) a contrast result.

### Class-name conventions

The class names below are the shared contract (e2e specs select on
them). Their CSS lives globally in `src/app.css`; the markup comes from
the `ui/` primitive in the Source column.

| Pattern | Class | Source |
|---|---|---|
| Page wrapper + header | `.workspace` / `.toolbar` / `<h1>` | `ui/PageHeader.svelte` |
| Data table | `.grid-container` + `table`/`th`/`td`/`.empty` | `ui/DataTable.svelte` |
| Search input | `.search-box` | `ui/SearchBox.svelte` |
| Bulk bar | `.bulk-bar` | `ui/BulkBar.svelte` |
| Bulk delete | `.bulk-delete-btn` (+ `.armed`) | `ui/BulkDeleteButton.svelte` |
| Bulk action | `.bulk-action-btn` | per-route, but always inside a BulkBar |
| Per-row action | `<RowAction>` (variant + armed) | `ui/RowAction.svelte` |
| Clickable-row open control | `<RowLink>` + `.clickable` row + `isRowOpenClick` | `ui/RowLink.svelte` / `utils/rowNav.ts` |
| Filter pill | `.filter-chip` (+ `.active`, `.count`) | `ui/FilterChips.svelte` |
| Tab bar | `.tab-row` / `.tab` (+ `.active`) | `ui/Tabs.svelte` |
| Load more | `.btn-load-more` / `.load-more-row` / `.load-more-end` | per-route, copy /admin |
| Modal dialog | `.modal[role="dialog"]` + `.backdrop` | `ui/Modal.svelte` |
| KPI card | `.kpi` / `.kpi-value` / `.kpi-label` | `ui/KpiCard.svelte` |
| Status badge (invoice) | `<StatusBadge>` (tone from `INVOICE_STATUS_TONES`) | `ui/StatusBadge.svelte` |
| Tinted badge (any tone) | `<Badge tone=…>` | `ui/Badge.svelte` |
| Money / currency | `<Money>` / `formatMoney` | `ui/Money.svelte` / `utils/money.ts` |
| Field-level advisory | `.field-warning` (`role="status"`) | `ui/FieldWarning.svelte` |
| Zero-data onboarding block | `.empty-state` (+ `data-testid`) | `ui/EmptyState.svelte` |
| Setup steps with done states | `.setup-checklist` (`li[data-done]`) | `ui/SetupChecklist.svelte` |
| Failed load + Retry (page, or whole screen) | `.load-error` (`role="alert"` in a page) | `ui/LoadError.svelte` |
| Heading / label + ⓘ HelpTip | `.help-row` (+ a local class for spacing only) | `src/app.css` (see § Contextual help) |
| Checkbox / radio / file | `input[type='checkbox'\|'radio'\|'file']` (global base) | `src/app.css` |

**Native form controls** are themed globally in `src/app.css` so a
bare `<input>` fits the theme — either theme — without per-route CSS:
- **Checkbox / radio** — `appearance: none` + a drawn mark (white check
  / inset accent dot) so *both* states are themed — empty = subtle
  outline on `--bg`, on = `--accent` fill, plus
  `:indeterminate` (checkbox) / `:focus-visible` / `:disabled`.
- **File input** — `::file-selector-button` restyled to match the
  secondary (`.btn-cancel`) button.
- **Select** — `appearance: none` + a drawn chevron, and the single
  source of truth for the whole select look (border / radius / surface /
  padding / type). Per-component scoped rules override only what differs
  per context (padding, `border-radius`, `font-size`, `width`,
  `--surface` background) and **must not** re-declare the shared
  border/colour/font or use a `background:` shorthand — the shorthand
  resets the chevron's `background-image`. Scoped rules keep ~30px of
  right padding so text clears the chevron. (Because no rule competes on
  `background-image`, the chevron needs **no `!important`** — earlier it
  did, before the scoped rules were collapsed onto this recipe.)
- **Range** — `accent-color: var(--accent)` (the modern cross-browser
  approach; no pseudo-element rebuild).
- `color-scheme` is set **per palette** (`dark` on `:root`, `light` in the
  light blocks — § Themes) so the remaining native controls (date pickers,
  scrollbars) follow whichever theme is showing; scrollbars are further
  thinned + tinted (`--scrollbar-thumb`), the date-picker indicator gets a
  hover, and `::selection` is accent-tinted.

Don't re-add per-route `accent-color` rules on checkboxes/radios — they
only tint the *checked* state and are redundant no-ops under the global
`appearance: none`.

(All Source paths are under `#lib/components/`.) If a shared style is
missing, add it to `src/app.css` (class-scoped) — not a per-route
`<style>`. If you need a brand-new pattern, add a component under
`#lib/components/ui/` and document it here. **Do not** invent a new
class name for an existing pattern, and **do not** re-introduce a
per-route copy of the table/modal/chip/shell CSS.

### Accessibility patterns (WCAG 2.2 AA)

The shared web foundation carries the baseline a11y so route pages
inherit it for free. Reuse these; don't re-solve them per page.

- **Skip link** (`.skip-link`, app.css; WCAG 2.4.1) — the first
  focusable element in both the app shell (`routes/+layout.svelte`)
  and the supplier portal (`routes/portal/+layout.svelte`). Off-screen
  until focused, then a high-contrast pill. Targets `#main-content` —
  the `<main>` element, which carries `id="main-content" tabindex="-1"`
  so the jump lands focus there. A new top-level shell must keep this
  pairing.
- **Landmarks** (WCAG 1.3.1) — the sidebar nav is `<nav aria-label="Primary">`,
  the section sub-tabs `<nav aria-label="<group> sections">`, the portal
  nav `<nav aria-label="Supplier portal">`. Name every nav landmark so
  multiple navs are distinguishable. Page `<h1>` lives in `PageHeader`.
- **Global focus ring** (app.css; WCAG 2.4.7) — a `:focus-visible`
  accent outline covers `a / button / [role=button|tab|option] /
  [tabindex] / input / select / textarea / summary`. Never set
  `outline: none` without replacing it with a visible ring (checkbox /
  radio do this — accent box-shadow). This is the floor; component-local
  `:focus-visible` (RowLink) layers on top.
- **Reduced motion** (app.css end; WCAG 2.3.3) — a global
  `@media (prefers-reduced-motion: reduce)` block near-zeroes all
  animation/transition durations **and zeroes their delays**. Don't gate
  functionality on a transition finishing. See **Motion** below for what
  that implies about how an animation must be written.
- **Motion** (the marketing page + auth shell; decisions §180). Five rules,
  each one learned from a defect:
  1. **The last keyframe is the resting state.** Under reduced motion the
     final frame is the whole experience, so an entrance ends visible and a
     loop ends on its finished picture — never on the blank frame it starts
     from, never off-screen.
  2. **A hidden starting state is set by script, never by a stylesheet.**
     `use:reveal` (`#lib/actions/reveal.ts`) adds `.reveal` only once it has
     attached, and not at all under reduced motion or without
     `IntersectionObserver` — so a failed bundle is a normal page, not a blank
     one. `use:countUp` likewise leaves the real figure in the markup.
  3. **Delays count.** An entrance with a delay and `backwards` fill sits on its
     first frame (usually opacity 0) for the whole delay; that is why the
     reduced-motion rule zeroes delays. Guard:
     `tests-e2e/a11y/reduced-motion.spec.ts`.
  4. **All motion ends within 5 s (WCAG 2.2.2).** The criterion covers motion
     that starts on its own and lasts more than five seconds, and this product
     answers it by never doing that — not with a pause control (the landing
     page's `MotionToggle` was removed; decisions §279). So: no `infinite`; a
     finite iteration count with `delay + duration × count ≤ 5s`; `forwards`
     fill so the animation rests on its last keyframe, which (rule 1) must
     show everything. Something that cannot fit — a marquee — is rendered
     static instead (`marketing/AdapterRail.svelte`). The OS preference does
     not discharge 2.2.2. Guards: `src/lib/a11y/marketingMotion.test.ts` (no
     `infinite`, literal timing within budget) and
     `tests-e2e/marketing/landing.spec.ts` (every running animation's
     `getComputedTiming().endTime` ≤ 5000 ms).
  5. **Fade with colour, not `opacity`, at rest.** A decorative watermark takes
     an `rgba()` colour; `opacity` composites a whole subtree
     (`opacityAudit.test.ts`). `opacity: 0` / `1` in a reveal or keyframe is fine.

  Playwright's `toBeVisible()` does **not** treat opacity 0 as hidden, so a
  motion test reads computed opacity up the ancestor chain
  (`tests-e2e/marketing/landing.spec.ts`), and the axe scans of these pages run
  under `emulateMedia({ reducedMotion: 'reduce' })` so contrast is measured at
  rest rather than mid-fade. That emulation does not shorten a transition
  already running, so `expectNoA11yViolations` also waits for
  `document.getAnimations()` to finish first (`tests-e2e/README.md`
  § Accessibility guard).

  **Name the properties a hover transition animates; never `transition: all`.**
  `all` also animates the colour tokens on a theme change, so text fades
  through sub-4.5:1 values for the length of the transition (#635). Transition
  `border-color`, or `background-color` where the resting background is
  `none` or transparent, and let colours swap at once.
- **Modal / focus trap** (`ui/Modal.svelte` + `#lib/actions/focusTrap.ts`;
  WCAG 2.1.2 / 2.4.3) — `use:focusTrap={{ onEscape }}` on a dialog box
  (with `tabindex="-1"`) moves focus in on open, traps Tab / Shift+Tab
  with wrap-around, closes on Esc, and restores focus to the trigger on
  close. The restore runs **after** the render flush, not during teardown: a
  dialog's own action can remove its trigger (approving a request from its
  dialog filters the row out of the Pending list), and a trigger focused
  synchronously is then lost to `<body>` when it unmounts. If the trigger is
  gone or now disabled, focus lands on `<main id="main-content">` instead; if
  something else already took focus (a second dialog), it is left alone.
  Guard: `tests-e2e/a11y/screen-reader.spec.ts`. `ui/Modal` uses it, and so do the four pre-existing hand-rolled
  feature shells (`InvoiceModal`, `RunDetailModal`, `BulkRecodeGLModal`,
  portal discount-accept) so every dialog gets identical focus management.
  Prefer `ui/Modal` for new dialogs; if you must hand-roll a shell, add
  `use:focusTrap` rather than re-implementing it.
- **Reorder controls** (WCAG 2.5.7 Dragging Movements) — any drag-to-reorder
  needs a single-pointer + keyboard alternative. The workflow-builder
  `StepNode` pairs its drag handle with per-node Move ↑ / Move ↓ buttons
  (`onmoveup`/`onmovedown` over the canvas `onreorder`). Mirror this for any
  new drag interaction; don't ship drag as the only path.
- **Toast** (`ui/Toast.svelte`; WCAG 4.1.3, 2.2.1) — `role="region"` named
  "Status messages" (`toast.region`; not "Notifications", which is the sidebar
  bell's name) + two persistent live containers (`aria-live="assertive"` for
  errors, `"polite"` for the rest). Each toast has a real 24px `<button>`
  dismiss (`toast.dismiss`). **Timing (decisions §313, superseding the fixed
  5-second timer this entry used to call deliberate):** an **error stays until
  dismissed** — an explicit `duration` does not change that; every other toast
  times out (5 s, or the caller's explicit `duration`), but its clock **stops
  while the pointer is over a toast or focus is inside the region**, and runs
  on with the time it had left. At most three errors stay on screen (the
  oldest goes), and the same error text arriving again replaces its old copy —
  a fresh insertion, so it is announced again. When a focused toast leaves
  (dismissed, evicted by the cap, replaced), focus moves to the next toast's
  dismiss, or back to where it came from when it was the last (`<main>` if that
  element is gone). The region rises above a showing bulk-action bar
  (`.bulk-bar`) and the consent banner, and a toast is never wider than
  `100vw − 32px` (2.4.11, 1.4.10). **A toast never covers any part of an open
  modal dialog** (a `role="dialog"` with `aria-modal="true"` — every dialog here
  carries both): when the corner stack would touch the dialog's box, the dialog
  **yields a band** at the bottom of the viewport and the stack sits in it,
  beside the dialog (`utils/toastPlacement.ts`, #635, #636). The backdrop's
  bottom padding grows to the band and the dialog's height is capped to what is
  left; its own scrolling takes the rest, each scroller keeping its distance from
  its bottom (so the footer the reader retries from stays in view) and the
  focused control scrolled into view. The stack takes at most 40% of the
  viewport's height and scrolls past that. Once yielded the band is kept, and
  only grows, until the dialog closes: a dialog moving back as a toast left
  would slide its footer under the pointer that just dismissed it (on a phone,
  an armed "Confirm cancel"). A click in the band does not close the dialog
  (it is backdrop, which every shell reads as "close"), a new viewport
  (rotation, zoom) recomputes the cap, and the band is only taken from a dialog
  whose parent is a full-viewport fixed backdrop. A dialog the corner does not
  touch never moves.
  Steering the stack around the dialog's contents was tried first (around the
  footer, then the focused control) and cannot work: a dialog is controls top to
  bottom, so on a phone there is no gap — the payment run's error sat over
  "Record run as paid…" in its body. A new dialog needs nothing for this beyond
  being centred in a full-viewport backdrop (its parent), the rule below.
  **The region is hosted inside the topmost modal dialog** while one is open
  (appended last, still
  `position: fixed`), and goes back to its place in the layout when the dialog
  closes: `aria-modal` hides everything outside the dialog from assistive tech
  (WebKit and Chromium prune it), so a region left outside was not announced
  and its dismiss buttons could not be reached. `actions/focusTrap` puts the
  region's dismiss buttons (`data-dialog-satellite`) after the dialog's own
  controls in its Tab cycle — last control → toasts → first control,
  Shift+Tab the reverse — and never gives one the dialog's initial focus;
  dismissing the last toast hands focus back to the dialog control it came
  from (2.1.1, 2.4.3). Escape on a focused toast dismisses that toast, not the
  dialog. The trap is told which dialogs are modal by the `aria-modal`
  attribute, so a new dialog without it neither hosts the region nor is
  avoided by it. **A modal's backdrop must be `position: fixed; inset: 0`**:
  every backdrop has `backdrop-filter`, which makes it the containing block of
  the hosted (fixed) region, and only a full-viewport backdrop keeps the stack
  in the viewport corner. A toast inserted while the region was detached or
  outside the open modal is re-inserted once the region has moved, so it is
  still announced. Browser guard:
  `tests-e2e/a11y/toast-timing.spec.ts` (Playwright clock, no sleeps). The
  rules are the pure `utils/toastQueue.ts` (unit-tested with fake timers).
  **A toast is never the only sign of a failed LOAD**: a page whose data did not
  arrive says so in place with `ui/LoadError` and a Retry (the vendors list and
  the notification bell are the worked examples); a toast reports an action's
  outcome. **One error, one signal:** an error a dialog or panel already states
  in place — a `role="alert"` element beside the control that failed — is not
  also toasted. `role="alert"` is announced the moment it appears, so the copy
  added nothing for a screen-reader user, and since an error toast no longer
  times out it sat over the dialog until dismissed (the payment run's NACHA
  refusal, stated in its panel AND toasted, covered "Record run as paid…"). A
  form that has an error slot puts its own client-side refusals there too
  (`cfo/ForecastVariancePanel`). A failed LOAD's generic in-place block may
  still be paired with a toast carrying the server's own detail — the two say
  different things. Guard: `src/lib/a11y/errorDoubleReport.test.ts` (Svelte AST:
  no `toast(…, 'error')` passes a variable rendered inside `role="alert"`, or
  sits on the same code path as an assignment to one — or, for an `{#if flag}`
  alert with fixed words, raises fixed words too). It reads one file at a time,
  so an error state passed down to a child component is outside it.
- **Inline form feedback** (WCAG 4.1.3) — a message that appears in response
  to an action (`{#if saved}<div class="message">…`) must be announced, not
  just shown. A polite status sits inside a live region that already exists
  (`<div role="status">{#if msg}<div class="message">{msg}</div>{/if}</div>`,
  the reliable form); an error may carry `role="alert"` inline, since its
  insertion is the announcement. When the feedback REPLACES the control the
  user activated (a form swapped for its confirmation), move focus onto it
  instead — `tabindex="-1"` + `.focus()` after `tick()`, as `/signup` and the
  forgot/reset-password pages do. Guard: `src/lib/a11y/liveRegionAudit.test.ts`
  (Svelte-AST scan of every `{#if}`-rendered `error` / `message` / `msg` /
  `success` element; the two non-feedback matches are an explained allowlist).
- **Field validation** (WCAG 3.3.1, 3.3.3) — keep Submit **enabled** and
  validate on submit: a Submit disabled until the form is valid gives no reason,
  and any message it guards can never appear (issue #635 #14). On submit, mark
  every bad field (`aria-invalid`, its message beside it and first in its
  `aria-describedby`) and move focus to the first one — focus is what reads the
  message, so don't also put it in a live region. A server refusal that is
  about one field (a wrong current password, a refused new one) goes beside
  that field the same way and clears when it is edited; only what belongs to
  no field stays in the banner. A message that appears while focus is elsewhere
  (a confirmation mismatch shown on blur) is the one case for a polite live
  region, rendered empty up front. Reference: `auth/PasswordInput.svelte`,
  `auth/NewPasswordFields.svelte`, `/change-password`. Help text such as a
  rules checklist is linked by `aria-describedby` too, after the error.
- **Tabs** (`ui/Tabs.svelte`; WAI-ARIA tabs) — roving `tabindex`
  (active=0, others=-1) + Arrow/Home/End key navigation, plus the
  existing `role=tablist/tab` + `aria-selected` + `aria-controls`. The
  caller still gives the panel `role="tabpanel"` + the matching ids. A
  hand-rolled `<button class="tab" class:active>` bar announces as N
  identical buttons — `/payments` and `/expenses` shipped one each until
  2026-10-05. `src/lib/a11y/tabBar.test.ts` fails any `.tab` button outside
  `ui/Tabs.svelte` that lacks `role="tab"` + `aria-selected`.
- **Filter chips** (`ui/FilterChips.svelte`) — `<button aria-pressed>`
  reflects the active chip.
- **Selected / current state** (WCAG 4.1.2, 1.3.1) — anything painted "on" by
  `class:active` also says so: `aria-current="page"` on the nav link to the
  page you are on (the sidebar — `"true"` on a folded group — and the portal
  nav), `aria-pressed` on a toggle, segmented or filter button (`/cfo`'s
  granularity / horizon pickers, which also sit in a named `role="group"`),
  `role="tab"` + `aria-selected` on a tab. Guard:
  `src/lib/a11y/selectedState.test.ts` fails any `class:active` link or button
  without one; the two exceptions (a toggle whose label is its state, and
  `SortableHeader`, whose `<th>` carries `aria-sort`) are an explained
  allowlist.
- **DataTable** (`ui/DataTable.svelte`) — auto-rendered `<th>` get
  `scope="col"`. A page that passes its own `{#snippet header()}` owns
  adding `scope` to its `<th>`s. The `.grid-container` scroller is a
  **named, focusable region** (`role="region"` + `aria-label` +
  `tabindex="0"`; WCAG 2.1.1): once a table is wider than its card it scrolls
  inside the card (the 1.4.10 remedy), and a table whose cells hold nothing
  focusable would otherwise be pannable only by mouse. Focused, the arrow keys
  pan it. The name defaults to `common.tableRegion` ("Data table"); pass
  `ariaLabel` when a page shows more than one table or "which table" is not
  obvious. The attribute is unconditional rather than applied on overflow,
  because axe's `scrollable-region-focusable` only fires while the table
  actually overflows — which depends on viewport and font width, and is how
  the legal pages' `.table-scroll` passed locally and failed in CI. Guards:
  `src/lib/a11y/tableScrollRegion.test.ts` (static — the attributes, and no
  hand-rolled `.grid-container`), `tests-e2e/a11y/reflow.spec.ts` (runs that
  axe rule on every route at 320px, where every such region is live) and
  `tests-e2e/a11y/table-scroll-region.spec.ts` (the arrow key really pans).
  A page that wraps its own table in a scroller (`/cfo`'s cash-position and
  budget tables) owes the same three attributes; the static guard fails any
  `tabindex="0"` region anywhere in `src/` that lacks a role or a name, or
  whose static `aria-labelledby` names an id not in the same file. The legal
  pages' 19 `.table-scroll` wrappers are covered too: each is
  `aria-labelledby` the heading its table sits under (the legal text stays
  English, decisions §174, so these names are not catalogue entries).
- **Status by colour** (WCAG 1.4.1) — a row tinted red to say "this one
  breaches" also says it in words: `/cfo`'s below-minimum periods carry a
  `<Badge tone="danger">Below minimum</Badge>` in the Closing cell, the
  `/payments` queue's overdue rows an "Overdue" badge. The banner above a table
  can give the count; only the row can say which.
- **Sidebar on a phone** (WCAG 1.4.10, 2.1.2, 2.4.3; #635) — at 700px and
  below the sidebar starts as its 60px icon rail, and expanding it opens the
  panel as an **overlay**, never a push: `<main>` keeps the rail's 60px offset
  and its full width, a `--overlay` backdrop covers it, and `<main>` plus the
  skip link carry `inert` while it is open (with the body scroll lock `ui/Modal`
  takes). Focus moves to the first nav row on open and back to the toggle when
  Escape or the backdrop closes it; following a link closes it too. A popup
  inside the panel (bell, entity menu, profile) takes Escape first — the panel
  reads `[aria-expanded="true"]` inside itself, so the order the window
  listeners run in does not matter. The open panel stacks at z-index 55
  (backdrop 54): above a page's own fixed bars (`BulkBar` is 50), below
  modals (100). It stays the primary-nav landmark rather than becoming a
  `role="dialog"` — the inert page already confines the screen reader to it,
  and the toggle's name ("Collapse sidebar") states that it is open. The
  rail's buttons transition `background-color` only, never `all`: a colour
  transition fades the muted labels through sub-4.5:1 values on every theme
  change. Desktop keeps the push layout. State lives
  in `stores/sidebar.svelte.ts` (`narrow`, `overlay`). Guard:
  `tests-e2e/a11y/sidebar-overlay.spec.ts` (375 and 320px, both themes under
  axe, desktop unchanged).
- **Icon-only controls** — every icon-only `<button>` needs an
  `aria-label` (NotificationBell reflects the unread count; the sidebar
  collapse toggle + profile button carry `aria-label` + `aria-expanded`).
- **De-emphasised rows** (`.row-muted`, app.css; WCAG 1.4.3) — a paused
  subscription, a revoked API key, a deactivated user. Put
  `class:row-muted` on the `<tr>`; the shared rule colours its non-actions
  cells `--text-muted` (5.38:1). **Never spell this as `opacity`.** Opacity
  is GROUP opacity: it composites the row's whole subtree onto the surface
  behind it, so it fades hardest the colours that were already quiet. On
  `--surface` the old fade measured `--text` 5.65:1 @0.6 / 4.34:1 @0.5,
  `--text-muted` 2.77:1 @0.6 / 2.33:1 @0.5, and a tinted `<Badge>`
  2.78–2.93:1 @0.6 — i.e. it was survivable on the one colour that wanted
  dimming and ruinous on every colour that didn't. A colour token also
  needs no carve-out: a descendant that sets its own colour (a `Badge`'s
  `-on-tint` pair, a `RowLink`'s accent) keeps its own calibrated contrast,
  which is why `/admin/api-keys` could drop the `:not(.status-col)` its
  fade had needed. `:not(.actions)` stays — a disabled control is exempt
  under 1.4.3, but an enabled Delete on a paused row is not, and it must
  not read as unavailable either. `opacity` remains correct for an
  **inactive** control (`:disabled` — exempt), a transient `:hover` fade on
  a filled button (measured 4.72–5.10:1), and genuinely text-free
  decoration. Guards: `src/lib/a11y/opacityAudit.test.ts` (static, catches
  the idiom anywhere in the tree) + `tests-e2e/a11y/deemphasised-rows.spec.ts`
  (axe, stubs the list so the faded row is actually on screen — the route
  scan passed for years because a fresh tenant has no paused row).

### Themes — dark, light, and System (docs/decisions.md §289)

The web app has **two palettes and three choices**. The palette is all CSS;
script only decides which one the page asks for.

| Choice | `<html data-theme>` | What renders |
|---|---|---|
| System (default) | `system` | dark, unless the OS says `prefers-color-scheme: light` |
| Light | `light` | the light palette |
| Dark | `dark` | the dark palette |

- **`src/app.css`** declares the dark palette on `:root` (it is the default,
  and what an OS with no preference gets), the light palette on
  `:root[data-theme='light']`, and the **same light block again** inside
  `@media (prefers-color-scheme: light) { :root:not([data-theme='dark']) {…} }`
  for System. CSS cannot share one declaration block between a plain selector
  and a media-gated one, so the copy is unavoidable — `tokenPairing.test.ts`
  fails if the two drift, if the light block misses a colour token the dark one
  declares, or if either loses `color-scheme`. An OS that changes theme while
  the page is open switches it with no script at all.
- **`src/app.html`** carries a tiny inline script that reads the stored choice
  and sets `data-theme` **before first paint**, so a Light user never sees a
  dark frame (the SPA's own CSS and JS arrive later). It cannot import the
  module below, so it repeats its facts — the key, the three values, the
  cookie-first read order, the two `theme-color`s — and `src/lib/theme.test.ts`
  *executes* the script against every cookie × stored value × OS preference to
  hold it to them.
- **Where the choice lives** (decisions §290). Every workspace is its own
  origin (`<slug>.feohledger.com`, plus the apex for signup), and
  `localStorage` is per origin — so on a platform host the choice is a
  **`feoh_theme` cookie on the platform's registrable domain**
  (`Domain=feohledger.com`, `SameSite=Lax`, a year, `Secure` on https), which
  every workspace and the signup page read alike. System is written to it as
  `system`, so it overrides a stale per-origin Light. The domain is only ever
  one the build declares as ours: a `PUBLIC_PLATFORM_DOMAINS` entry or the
  host of `PUBLIC_SITE_URL` (the apex every deploy sets), which the current
  host must equal or sit beneath (`hostRouting.ts::sharedCookieDomainForHost`,
  via `tenant.ts::sharedCookieDomain()`). It is never hardcoded, and never
  taken from the legacy host rule, which would read a customer's
  `ap.customer.example` as ours. On localhost, an IP literal, a customer's
  vanity host, or a build that declares nothing there is none, and the choice falls back
  to `localStorage` per origin (System = no entry) — as it does when the
  browser drops the cookie, which the write detects by reading it back. A
  cookie, when present, wins over `localStorage`.
- **`src/lib/theme.ts`** (pure) + **`stores/theme.svelte.ts`** (rune wrapper)
  own the choice: read / write the cookie and the `localStorage` fallback,
  resolve the palette that is showing, set `<meta name="theme-color">`.
  **Every cookie and storage access is wrapped** — both throw in some private
  modes and with blocked site data, and the page falls back to System rather
  than failing. Only non-CSS consumers need `theme.resolved` (the hCaptcha
  widget on `/signup`, an iframe that cannot see our palette: it is rendered
  explicitly and redrawn when `theme.resolved` changes — unless already solved,
  so a theme change never costs the visitor their token).
- **The control** is `ui/ThemePicker` (a `SegmentedControl` — a fieldset of
  native radios) on `/profile?section=appearance` and under the form on every
  `AuthShell` page. It is listed on the Cookie Notice (`feoh_theme`), which
  with the Privacy Policy says it is the only cookie the app sets —
  `storageInventory.test.ts` fails if a second `document.cookie =` appears.
- **Everything follows the theme:** the signed-in app, the auth shell
  (`AuthShell`, signup, MFA, reset), the supplier portal, the help centre, the
  legal pages and the marketing landing page (decisions §312, which ended
  §289's dark pin). There is no per-subtree palette pin, and
  `tokenPairing.test.ts` fails if a `data-theme-scope` reappears. The landing
  page's header carries a compact `ThemePicker` (an anonymous visitor has no
  `/profile`).
- **The public pages' brand colours are tokens too.** `--gilt` (the tally's
  gold) and `--brand-violet` are the gradient stops beside the accent blue
  (headline, stat figures, pipeline rail, featured-plan ring); `--drop-shade`
  is the cast shadow under a lit object. Each has a light value, and the stops
  are held to 4.5:1 on `--bg` / `--surface` in both palettes, because a
  `background-clip: text` gradient is something the rule scan cannot measure.
  The marketing components may type no colour literal at all except `#fff` on
  a `-strong` fill and a mask's `#000` (the same test).
- **A raster picks its file from `theme.resolved`.** The rendered tally
  ornament has a lighting per palette (`#lib/marketing/ornament.ts`, generated
  by `pnpm gen:marketing`); a `<picture>` with a `prefers-color-scheme` source
  would follow the OS and ignore a stored choice. Both renders share one
  intrinsic size so a theme switch never moves the layout.
- **A new colour token goes in both palettes**, measured in both. The light
  base tokens are deliberately darker than the bare-surface bar needs: ~40
  rules set a base token as text on a 10–20% tint of its own hue, and each base
  token clears its own 20% tint.
- **The one runtime hole is the white-label accent.** A tenant's brand
  `--accent` is written inline on `<html>`, so it applies in both themes, and a
  blue picked for navy is ~3:1 as text on white. Tracked in
  `docs/followups.md`.

### Colour tokens and contrast (WCAG 1.4.3)

The palette in `src/app.css` is small and every colour token has a **stated
job**. They come in families, and picking the wrong member is the one mistake
this codebase kept making. Values below are dark / light (§ Themes):

| Base token — text / icons / borders on an app surface | `-strong` companion — the FILL behind white text | `-tint` / `-on-tint` pair — the status-badge recipe |
|---|---|---|
| `--accent` `#638cff` / `#2c4fc4` | `--accent-strong` `#3f5fd6` / `#2546b0` | `--accent-tint` + `--accent-on-tint` `#7d9bff` / `#2a47b0` |
| `--success` `#1fa86a` / `#11663a` | `--success-strong` `#177a4d` / `#0f5e35` | `--success-tint` + `--success-on-tint` `#26b977` / `#0f5c34` |
| `--danger` `#f87171` / `#a82622` | `--danger-strong` `#c43535` / `#9c221e` | `--danger-tint` + `--danger-on-tint` `#f87171` / `#a3221f` |
| `--warning` `#d4940a` / `#7f5400` | — (nothing puts white text on amber) | `--warning-tint` + `--warning-on-tint` `#dca014` / `#7a4c00` |
| `--text-muted` `#8a8fa0` / `#5a6072` | — | `--muted-tint` + `--muted-on-tint` `#9aa0b2` / `#454a59` |
| — | — | `--violet-tint` + `--violet-on-tint` `#a585f5` / `#5a36b5` (the "with the ERP" pill) |

Chrome tokens with no text on them: `--border-strong` (a hover edge),
`--border-soft` (a dialog's edge), `--overlay` (the dimmed page behind a
modal), `--scrollbar-thumb` / `--scrollbar-thumb-hover`. A tone-tinted
background is written `color-mix(in srgb, var(--<tone>) N%, transparent)`,
never an `rgba()` of today's hex — the audit resolves that `color-mix()`
spelling (and the opaque two-colour form) so it is measured like the literal it
replaced.

- **Never put white text on a base token.** In the dark theme all three are
  mid-tones chosen to be legible *as text on the dark surfaces*; white on them
  is 3.06–3.12:1, well under the 4.5:1 bar. In the light theme they happen to
  carry white, but a stylesheet serves both themes, so the rule stands. That is what the `-strong` half is for, and it is
  the only thing it is for — `--danger-strong` as *text* on `--surface` would
  be unreadable in the other direction.
- **A tinted badge takes the `-on-tint` text, never the base token.** A
  translucent tint lightens the dark surface *toward* text set in the same
  tone, so a base token — chosen to be legible on the BARE surface — lands
  just under the bar once composited. `--accent` on `--accent-tint` is 4.48:1:
  two hundredths short, which is why 29 badges shipped that way unnoticed.
  Write `background: var(--accent-tint); color: var(--accent-on-tint)` and
  nothing else; the pair is calibrated together and carries ≥0.7 of margin on
  both `--bg` and `--surface`. `--danger-on-tint` equals `--danger` on purpose
  — red needs no lift — so that the rule has no exception to remember.
- **`--surface-2` `#232b44` is the hostile surface** (dark theme). Only `--text`
  clears 4.5:1 on it (11.0:1). `--text-muted` is 4.34:1 there — the failure the
  axe guard originally caught. Muted text belongs on `--bg` or `--surface`; the
  light palette happens to admit it, but design to the dark refusal.
- **Never write a `var(--token, fallback)` fallback.** Every token above is
  declared, so the fallback is dead code that becomes the *wrong colour* the
  day a token is renamed. `--surface-2` shipped for months as
  `var(--surface-2, #232b44)` with the token undefined, two call sites
  disagreeing about the value — that is the bug this rule prevents. Same for
  `--font-mono`, the canonical monospace stack.
- **Text colour comes from a token, not a literal.** A bare `color: #<hex>`
  with no background in the same rule renders on whatever the cascade supplies,
  so it has to be legible on `--bg` and `--surface` — and `#e04040` (the old
  status red) was 4.11:1 on `--surface`, failing in 106 places. `#fff` / `#000`
  are exempt: they're the deliberate on-a-coloured-fill choices. Decorative
  fills (a chart bar, a confidence dot, an SVG `fill`) carry no text and are
  not covered by this.

Two guards enforce it, and neither subsumes the other:

- **`src/lib/a11y/tokenPairing.test.ts`** (vitest, no browser) scans **every**
  stylesheet in `src/`, **once per theme** — `app.css` plus every `<style>` block — for a rule that
  sets both `color` and a `background`, resolves both through the palette, and
  fails below 4.5:1 (3:1 when the rule itself declares a large-text size). It
  also fails a bare literal `color:` that can't clear the bar on `--bg` or
  `--surface`, a fallback that contradicts its token, a `var()` on a token
  nothing assigns, and asserts the table above directly. It also measures a
  rule that fades **itself** with `opacity`, because opacity composites text
  and its background down onto the backdrop — so a token that clears the bar at
  full strength can render under it (`--text-muted` at `.85` is 4.24:1 on
  `--surface`). **Don't dim already-muted text with `opacity`**: the token has
  done that job, and the fade only spends contrast. It measures a **translucent
  background** the same way and in the same pass, compositing the tint over each
  backdrop before judging the pair — the check that found all 29 badges. And it
  asserts each `-tint` / `-on-tint` pair directly, so a tone that drifts names
  one token instead of the dozen sites that spelled it. Pure scanners live in
  `a11y/cssAudit.ts`; the WCAG math in `a11y/contrast.ts`.
- **`tests-e2e/a11y/axe.spec.ts`** (dark) and **`light-theme.spec.ts`** (light, a
  cross-section of the same surfaces) cover what the scanner deliberately can't:
  a rule setting only `color` inherits its background through the cascade at
  runtime, and an **ancestor's** `opacity` fades a descendant the scan reads as
  fine (a revoked-row fade put `/admin/api-keys`' status pill at 2.44:1). Add a
  route here when you add a page carrying dialogs or dense controls.

What was left was **consistency, not contrast**. 202 rules used to spell a
tinted badge as a hand-rolled `rgba()` plus a literal hex — 44 spellings of what
the five pairs above name. `ui/Badge.svelte` owns the recipe, and **the
conversion tranches are done**: every entry still in the baseline is a
*deliberate keep* carrying its reason in its own stylesheet, not outstanding
work. The tranches moved one file at a time on purpose — the tokens standardise
on alpha `.15`, so converting a `.1` or `.12` rule *visibly strengthens* that
badge's tint, and landing them all at once would make any visual complaint
unattributable. Two real defects surfaced that way (`/goods-receipts` badging
every status green, so a cancelled receipt read as delivered; a draft run tinted
amber in a modal and flat neutral on the page one click apart), which is the
argument for checking what a distinction was carrying before collapsing it.

**`src/lib/a11y/badgeAudit.test.ts` is the ratchet, and the count.** It scans
every stylesheet for a badge-shaped selector (`badge` / `chip` / `pill` / `tag`)
setting both a tinted background and a colour, holds each file to a recorded
baseline, and holds the converted files at zero. A new hand-roll fails on the
file it landed in. Now that no tranche is outstanding, **a new non-zero baseline
entry is a keep that has to argue for itself** — add it with its reason beside
the others, or use the primitive. Don't restate the remaining count in prose —
it goes stale, and the baseline map is the live figure. Reach for `<Badge>` in
new code and whenever you are already editing one of these rules.

**A failure means changing the colour, never relaxing the rule** — there is no
suppression mechanism, because the `-strong` companions mean a correct answer
always exists. Rationale + what was rejected: `docs/decisions.md` §28.

**The one runtime hole:** white-label theming lets a tenant overwrite
`--accent` / `--accent-strong` with any valid hex (`stores/brandTheme.ts`
`brandThemeVars`), which no static scan can see. `accentStrongContrast` /
`accentStrongMeetsAA` (same file, same WCAG primitive) drive a `FieldWarning`
advisory on both surfaces that edit it — `/organization` Branding and the
`/admin/partner` child-branding modal. Advisory, not a block: the backend
accepts any valid hex and the brand is the tenant's call.

