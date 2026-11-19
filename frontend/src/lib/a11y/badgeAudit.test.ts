import { describe, expect, it } from 'vitest';
import { countByFile, findHandRolledBadgeRules } from './badgeAudit';
import { extractStyleBlocks, type StyleSource } from './cssAudit';

/**
 * Repo-wide ratchet on the tinted-badge conversion.
 *
 * `ui/Badge.svelte` owns the `background: var(--<tone>-tint); color:
 * var(--<tone>-on-tint)` recipe. 205 CSS rules hand-rolled it in 44 spellings of
 * the same five tones before it existed. They moved onto the primitive in
 * **attributable tranches** — the shared tokens standardise on alpha `.15`, so
 * converting a `.1` or `.12` rule visibly strengthens that badge, and landing
 * them all at once would have made any visual complaint impossible to trace to
 * a commit.
 *
 * **The tranches are finished.** Every entry left in `BASELINE` is a deliberate
 * keep carrying its own reason — a pill with metrics a shared primitive cannot
 * serve (an inline annotation beside a name, a canvas edge label, a filled
 * tier chip), or a container with a child control. Each still takes the palette
 * pairs, so none of them can drift off-tone. There is no work queued here: a
 * NEW non-zero entry is a keep that has to argue for itself in a comment, not a
 * tranche waiting to land.
 *
 * That is what makes a ratchet the right guard rather than a hard zero. It:
 *
 *   - **cannot be satisfied by adding.** A new hand-rolled badge fails on the
 *     file it landed in, by name, with the selector.
 *   - **records the conversion as it happens.** Landing a tranche means editing
 *     the number down here, in the same commit, which is also the only place
 *     the remaining work is counted.
 *   - **holds the finished files at zero.** `/payments`, `/admin/webhooks`,
 *     `RequisitionModal` and `ExpenseModal` were converted in round 13; a
 *     regression there is a plain failure, not a loosened budget.
 *
 * The audit deliberately counts more than the seven files the follow-up names:
 * `chip` / `pill` / `tag` are the same capsule under other names, and the
 * recipe is respelled in components (`RunDetailModal`, `ScreeningBadge`,
 * `SupplierChatThread`) as readily as in routes.
 */

const RAW = import.meta.glob('/src/**/*.{svelte,css}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const sources: StyleSource[] = Object.entries(RAW)
	.map(([path, source]) => [path.replace(/^\/src\//, ''), source] as const)
	.sort(([a], [b]) => a.localeCompare(b))
	.flatMap(([path, source]) => extractStyleBlocks(path, source));

/**
 * Hand-rolled badge rules per file, as of the last tranche.
 *
 * **Only ever edit a number DOWNWARD** (or delete the entry at zero). A file
 * missing from this map must have none at all — which is how a brand-new page
 * is held to the primitive from its first commit.
 */
const BASELINE: Record<string, number> = {
	// --- Deliberate keeps ---------------------------------------------------
	// Each of these already takes the palette `-tint` / `-on-tint` pairs and
	// carries its reason in its own stylesheet; they are counted because the
	// rule IS still hand-rolled, not because the conversion is outstanding.
	// A rule here that stops using the tokens is a regression like any other.
	//
	// Role NAMES and permission TAGS, not statuses — sentence-cased at the
	// user's own capitalisation, which `Badge`'s uppercase would misrender.
	'lib/components/admin/RolesPanel.svelte': 3,
	// `.you-badge` is also an e2e hook, and a status pill's metrics would push
	// the name it sits beside around.
	'lib/components/admin/UsersPanel.svelte': 2,
	// The status pill moved onto `Badge` in round 25 (its e2e hooks went to a
	// wrapper span). The one keep is the composer's mention tag, which wraps
	// its own remove button: a flex container with a child control, not a
	// text pill.
	'lib/components/chat/SupplierChatThread.svelte': 1,
	// Summary-row COUNTS, read as a group at their own denser metrics.
	'lib/components/modals/PositivePayModal.svelte': 3,
	'lib/components/modals/VendorStatementReconModal.svelte': 3,
	// A member's ROLE inside a cluster, in a dense table beside the vendor
	// name — the `ScreeningBadge` concession in miniature.
	'lib/components/modals/VendorConsolidationModal.svelte': 1,
	// The accepted tier is a FILLED chip (`--success-strong` behind white) and
	// the palette has no solid tone; putting the rest on the primitive would
	// leave that emphasis as a colour rule on a `variant`, which `Badge`
	// forbids. §52's ring trick doesn't transfer — here the distinction IS the
	// fill. Also carries `tabular-nums` so the figures line up down a column.
	'lib/components/ui/DiscountTierBar.svelte': 1,
	// Its own smaller sentence-case metrics are deliberate (a badge that needs
	// different metrics is a different component, not a prop) — but it takes the
	// palette tokens, so it is a keeper rather than a conversion target.
	'lib/components/ui/ScreeningBadge.svelte': 3,
	// TRUE/FALSE edge labels sized to sit on a canvas connector, where a status
	// pill's metrics would swamp the node.
	'lib/components/workflow-builder/WorkflowCanvas.svelte': 2,
	// `.priors-badge` is an inline extraction-PROVENANCE annotation
	// ("RAG·2·cache·3") sitting beside the vendor NAME in a fixed-width
	// `overflow: hidden` cell — the `.you-badge` case again. A status pill's
	// metrics would crowd out the name it annotates, and `Badge`'s uppercase
	// would shout a figure that is deliberately quiet. Takes the palette pair.
	'routes/invoices/+page.svelte': 1,
	// "The plan you already have" / "the default card" — markers inside a radio
	// label and a table cell, both a register smaller than a status pill.
	'routes/billing/+page.svelte': 2,
	// The two deliberate keeps from the round-13 tranche: `.discount-chip` is two
	// stacked lines, `.blocked-chip` wraps a localised sentence where `nowrap`
	// would break 320px reflow. Both took the palette tokens.
	// `.pinned-chip` is the third, and it is `.blocked-chip`'s sibling by
	// construction: same shape, same wrapped localised sentence, accent tone
	// instead of danger because that row is PAYABLE (a live virtual card pins
	// its rail, it does not refuse the invoice) — so it must not read as a
	// refusal next to the rows that are one. `Badge`'s `nowrap` would break the
	// same 320px reflow, and it takes the same palette pair.
	'routes/payments/+page.svelte': 3,
	// `.approver-chip` wraps its own remove button and renders a person's name;
	// `.chip-remove:hover` is a hover STEP that has to read stronger than the
	// chip it sits inside, so a tone token would make it invisible.
	'routes/workflows/[id]/+page.svelte': 2,
};

/** Files a tranche took to zero. A rule reappearing here is a regression. */
const CONVERTED = [
	'routes/admin/webhooks/+page.svelte',
	// The offer-status pill; its tone map went to `types/discounts.ts` beside the
	// `DiscountStatus` union it is keyed on, because `accepted` (amber) vs
	// `captured` (green) is the distinction between money committed and money
	// actually saved — not a shade the page should be free to pick.
	'routes/discounts/+page.svelte',
	// The three yes/no 1099-compliance chips. They sit in their OWN centred
	// columns rather than beside a name, so the primitive's metrics cost the
	// table nothing — the "dense chip" concession `ScreeningBadge` earns does
	// not apply here. Eligible/On file/Verified are `success`, the W-9 and TIN
	// gaps `danger`, and "not eligible" stays `muted`: it is the absence of an
	// obligation, not a compliance failure, and flattening the two onto one
	// tone would make a vendor we owe no form for read like a vendor we are
	// missing a form FROM.
	'routes/tax/+page.svelte',
	// The per-run discrepancy COUNT pill. Kept apart from the `VendorStatementReconModal`
	// summary-row counts above, which stay hand-rolled: those are read as a
	// GROUP at their own denser metrics, this one is a single figure in its own
	// numeric column. The zero case is deliberately left un-badged.
	'routes/vendor-statements/+page.svelte',
	// The vendor lifecycle pill, the last of the tranches. Its tone map went to
	// `types/vendor.ts` because `VendorModal` is the detail view of the same row
	// and would otherwise be free to tint it differently; `.status-badge` is
	// kept as a `variant` because `tests-e2e/vendors/bulk.spec.ts` selects on it.
	// The neighbouring `.source-badge` (Manual / ERP Sync / AI Extracted) is
	// deliberately untouched: it is flat `var(--bg)`, so it was never the tinted
	// recipe, and it reads a register quieter than the status beside it on purpose.
	'routes/vendors/+page.svelte',
	// Converted with `types/payment.ts` and `/payments` as one move, because it
	// badges the SAME `PaymentStatus` union and the same run statuses — the two
	// tone maps now live beside `PAYMENT_STATUS_LABELS` in the shared types
	// module. Its seven local rules had already drifted from the list page's
	// (amber `draft` vs flat, and no rule at all for four payment statuses),
	// which is what converting it with a local map would have preserved.
	'lib/components/modals/RunDetailModal.svelte',
	// The dashboard's second spelling of `.overdue-badge` — /payments already
	// rendered the same flag through the primitive, so it shipped at two sizes
	// on two pages.
	'routes/+page.svelte',
	// Converted in round 13; their tone maps were hoisted into
	// `types/requisition.ts` / `types/expense.ts` when the list pages below
	// converted, which is where each map's own comment said they belonged.
	'lib/components/modals/RequisitionModal.svelte',
	'lib/components/modals/ExpenseModal.svelte',
	// Four vocabularies between them (expense, expense report, pre-approval,
	// card reconciliation, requisition), every tone map now beside its label map
	// in the shared types module rather than duplicated in the modal.
	'routes/expenses/+page.svelte',
	'routes/requisitions/+page.svelte',
	'lib/components/marketing/Landing.svelte',
	'lib/components/modals/InvoiceModal.svelte',
	'routes/credit-memos/+page.svelte',
	'routes/exceptions/+page.svelte',
	'routes/goods-receipts/+page.svelte',
	'routes/organization/+page.svelte',
	'routes/purchase-orders/+page.svelte',
	'routes/vendors/change-requests/+page.svelte',
	'routes/workflows/+page.svelte',
	// The supplier portal's status pills (round 25). They were never counted
	// here: `.status` / `.s-paid` are not badge-shaped names and set a tinted
	// border instead of a colour, so the scan could not see them. Their tones
	// now live beside the phase label maps (`types/portalStatus.ts`) and the
	// shared PO map (`types/purchaseOrder.ts`).
	'routes/portal/invoices/+page.svelte',
	'routes/portal/payments/+page.svelte',
	'routes/portal/purchase-orders/+page.svelte'
];

const counts = countByFile(findHandRolledBadgeRules(sources));

describe('tinted-badge conversion ratchet', () => {
	it('scans a non-trivial set of stylesheets', () => {
		// A glob that silently matched nothing would make every assertion below
		// pass vacuously.
		expect(sources.length).toBeGreaterThan(50);
	});

	it('detects the recipe it is meant to detect', () => {
		// The audit's own regression test: a file everyone agrees still
		// hand-rolls must be found. Without it, breaking the parser turns this
		// whole suite green — every `toBe(0)` below would pass vacuously.
		// Repointed from `/vendors` when the last tranche converted. Now anchored
		// on a PERMANENT keep rather than a conversion target, so it cannot be
		// invalidated by the next file that lands at zero.
		expect(counts['lib/components/ui/ScreeningBadge.svelte']).toBeGreaterThan(0);
	});

	it.each(CONVERTED)('%s stays on the shared primitive', (path) => {
		expect(counts[path] ?? 0).toBe(0);
	});

	it('no file grows a new hand-rolled badge rule', () => {
		const regressions = Object.entries(counts)
			.filter(([path, n]) => n > (BASELINE[path] ?? 0))
			.map(([path, n]) => `${path}: ${n} (baseline ${BASELINE[path] ?? 0})`);

		expect(
			regressions,
			'A badge-shaped rule that sets both a tinted background and a colour ' +
				'belongs on <Badge tone="…">, which owns that recipe — a caller names a ' +
				'tone and cannot spell it wrong. If this is a deliberate keep (its own ' +
				'metrics, a multi-line chip), take the palette tokens and raise the ' +
				'baseline with the reason.'
		).toEqual([]);
	});

	it('the baseline names no file that is already clean', () => {
		// Keeps the map honest as tranches land: a stale entry would quietly
		// re-authorise a hand-roll in a file that no longer has one.
		const stale = Object.keys(BASELINE).filter((path) => (counts[path] ?? 0) === 0);
		expect(stale, 'converted — drop these from BASELINE').toEqual([]);
	});
});
