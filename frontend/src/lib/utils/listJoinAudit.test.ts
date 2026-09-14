import { describe, expect, it } from 'vitest';

/**
 * Source-scan guard: a literal `.join(', ')` over PROSE must go through
 * `utils/list.ts::formatList`.
 *
 * `', '` is English punctuation. Japanese enumerates with the ideographic
 * comma `、`; German and Spanish join the final pair with a word. A translated
 * sentence with an ASCII-comma list spliced into it is half-localized in three
 * of the six shipped locales, and the seam is loudest in a screen-reader
 * announcement, where the separator is the pause.
 *
 * But the literal is CORRECT in the other half of the tree, and swapping it for
 * a locale-dependent separator there would be a bug rather than a translation
 * fix. So this guard does not ban the literal — it pins the exhaustive list of
 * places that keep it, each with the reason it does. A new `.join(', ')` fails
 * here until someone classifies it: prose goes through `formatList`, everything
 * else joins the table below with a one-line justification.
 *
 * Textual and deliberately blunt, mirroring `storeLoadRejection.test.ts` and
 * `effectTimerCleanup.test.ts` (and the backend's own source-scan drift guards
 * in `tests/test_payment_methods.py`). It reads the tree through Vite's
 * `import.meta.glob` for the same reason `a11y/tokenPairing.test.ts` does — the
 * frontend deliberately carries no `@types/node`.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * Every component that keeps a literal `', '` join, mapped to how many it has
 * and why it is not prose.
 *
 * Three families, and the distinction is what the joined string is FOR:
 *
 *  1. **Round-tripped through an input.** The value is rendered into a text
 *     field and re-parsed by splitting on `,`. A locale separator would make
 *     the field un-round-trippable — type in Japanese, save, and the whole list
 *     collapses to one entry.
 *  2. **A bare list of machine identifiers in a table cell.** API scopes,
 *     webhook event types, audit-log field names, email addresses. These are
 *     codes a reader copies, not a sentence they read, and the ASCII comma is
 *     part of that convention. **Role slugs used to be the exemplar here and no
 *     longer are**: `types/admin.ts::ROLE_LABEL_KEYS` gave the four built-ins a
 *     translated name, which turns the list into prose — so `/profile` left this
 *     table, and the reason an entry cites "role slugs" now has to be that its
 *     route is untranslated (family 3), not that a role has no name.
 *  3. **Backend-generated English not yet routed through `m()`.** Localizing
 *     the punctuation of a sentence whose words are still hardcoded English
 *     would be a half-fix; the durable fix is extracting the copy, and these
 *     move to `formatList` in the same change.
 */
const ALLOWED: Record<string, { count: number; why: string }> = {
	'/src/lib/components/analytics/ScheduledReportsPanel.svelte': {
		count: 1,
		why: 'family 2 — recipient email addresses in a `title` tooltip; identifiers, not prose'
	},
	'/src/lib/components/exceptions/AgentDashboard.svelte': {
		count: 1,
		why: 'family 2 — an agent decision`s raw `field: old → new` diff pairs'
	},
	'/src/lib/components/modals/ApprovalMatrixEditor.svelte': {
		count: 1,
		why: 'family 1 — routing-rule set value, re-parsed by `parseRuleValue` splitting on ","'
	},
	'/src/lib/components/reports/FilterEditor.svelte': {
		count: 1,
		why: 'family 1 — `in` filter values, re-parsed by `setInText` splitting on ","'
	},
	'/src/lib/components/workflow-builder/ConditionBuilder.svelte': {
		count: 1,
		why: 'family 1 — condition set value, re-parsed by `parseValue` splitting on ","'
	},
	'/src/lib/components/workflow-builder/CustomStepConfig.svelte': {
		count: 1,
		why: 'family 1 — email recipients, re-parsed by `parseAddresses` splitting on ","'
	},
	'/src/routes/admin/access-review/+page.svelte': {
		count: 1,
		why: 'family 3 — role slugs, but the whole route is still hardcoded English (its own column headers included), so labelling the roles alone would localize the punctuation of an untranslated table. Graduates with the route'
	},
	'/src/routes/admin/api-keys/+page.svelte': {
		count: 1,
		why: 'family 2 — API-key scope identifiers in a table cell'
	},
	'/src/routes/admin/webhooks/+page.svelte': {
		count: 1,
		why: 'family 2 — webhook event-type identifiers in a table cell'
	},
	'/src/routes/audit/+page.svelte': {
		count: 1,
		why: 'family 2 — raw audit-log field names off the wire'
	}
};

/** `.join(', ')` and `.join(", ")`, the two spellings prettier permits. */
const JOIN_LITERAL = /\.join\(\s*(?:', '|", ")\s*\)/g;

function literalJoinCounts(): Record<string, number> {
	const counts: Record<string, number> = {};
	for (const [path, source] of Object.entries(RAW)) {
		const n = source.match(JOIN_LITERAL)?.length ?? 0;
		if (n > 0) counts[path] = n;
	}
	return counts;
}

describe('locale-aware list joining', () => {
	it('has no unclassified literal `, ` join in the component tree', () => {
		const found = literalJoinCounts();
		const unexpected = Object.keys(found)
			.filter((p) => !(p in ALLOWED))
			.sort();
		expect(
			unexpected,
			[
				'These files join fragments with a literal ", ".',
				'If the joined text is PROSE a human reads (translated fragments, or data',
				'spliced into an m() sentence), use `formatList` from $lib/utils/list.',
				'If it is an input value re-split on ",", a machine payload, or a bare list',
				'of identifiers, add it to ALLOWED in this file with the reason.'
			].join('\n')
		).toEqual([]);
	});

	it('has not grown a literal join in an already-classified file', () => {
		const found = literalJoinCounts();
		const grown = Object.entries(ALLOWED)
			.filter(([path, { count }]) => (found[path] ?? 0) > count)
			.map(([path, { count }]) => `${path}: ${found[path]} > ${count}`);
		expect(grown, 'a new literal join landed in a file that already had one').toEqual([]);
	});

	it('has no stale ALLOWED entry', () => {
		// A migrated file must leave the table, or the table stops describing the
		// tree and the first guard silently narrows.
		const found = literalJoinCounts();
		const stale = Object.entries(ALLOWED)
			.filter(([path, { count }]) => (found[path] ?? 0) < count)
			.map(([path, { count }]) => `${path}: ${found[path] ?? 0} < ${count}`);
		expect(stale, 'ALLOWED over-counts — drop the entry or lower its count').toEqual([]);
	});

	it('keeps the migrated prose sites on the helper', () => {
		// The components the round-29 and round-30 slices moved. Named explicitly
		// so a revert is a test failure rather than a silent regression to English
		// punctuation inside a translated sentence.
		//
		// `/invoices` is the round-30 entry, and it is the worked example of family
		// 3 graduating: its two joins sat in ALLOWED because the `Warnings: …`
		// sentence around them was itself hardcoded English, and migrating the
		// separator alone would have localized the punctuation of an English
		// sentence. Keying the frame is what let the separator follow, in the same
		// change — which is the rule family 3 states.
		//
		// `/profile` is the round-31 entry, and it is the worked example of a
		// family **2** claim expiring: the Account card's roles were exempt
		// because slugs are identifiers, and they were only slugs because nothing
		// had given the four built-in roles a translated name. `ROLE_LABEL_KEYS`
		// did, and the exemption went with it.
		const MIGRATED = [
			'/src/lib/components/admin/UsersPanel.svelte',
			'/src/lib/components/modals/InvoiceModal.svelte',
			'/src/routes/+page.svelte',
			'/src/routes/invoices/+page.svelte',
			'/src/routes/profile/+page.svelte',
			'/src/routes/vendors/screening/+page.svelte'
		];
		for (const path of MIGRATED) {
			expect(RAW[path], `${path} not found in the glob`).toBeTypeOf('string');
			expect(RAW[path], `${path} should call formatList`).toContain('formatList');
		}
	});
});
