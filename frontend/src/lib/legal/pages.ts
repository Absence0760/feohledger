/**
 * The published legal document set.
 *
 * One entry per page under `routes/legal/`. The index page, the cross-page nav
 * strip in `LegalPage.svelte`, the marketing footer and the e2e spec all read
 * this list, so a new legal page is added in exactly one place and every
 * surface picks it up. A page that exists as a route but not here would be
 * unreachable by navigation — the e2e spec asserts the two agree.
 */
export interface LegalPageMeta {
	/** Route path, absolute, no trailing slash. */
	path: string;
	/** `<h1>` and link text. Pinned by the e2e spec. */
	title: string;
	/** One line describing what the document covers, for the index and footer. */
	blurb: string;
}

export const LEGAL_PAGES = [
	{
		path: '/legal/privacy',
		title: 'Privacy Policy',
		blurb:
			'What personal data FeohLedger holds in its own right, why, for how long, and how to exercise your rights over it.',
	},
	{
		path: '/legal/terms',
		title: 'Terms of Service',
		blurb:
			'The agreement governing use of FeohLedger — subscriptions, acceptable use, liability, and termination.',
	},
	{
		path: '/legal/dpa',
		title: 'Data Processing Addendum',
		blurb:
			'The GDPR Article 28 terms under which FeohLedger processes supplier and invoice data on a customer’s behalf.',
	},
	{
		path: '/legal/sub-processors',
		title: 'Sub-processors',
		blurb:
			'Every third party that can receive customer personal data, which are engaged today, and how changes are notified.',
	},
	{
		path: '/legal/accessibility',
		title: 'Accessibility Statement',
		blurb:
			'The conformance target for the web app, the supplier portal and the mobile app, what is verified today, what is not yet, and how to report a barrier.',
	},
	{
		path: '/legal/cookies',
		title: 'Cookie Notice',
		blurb:
			'Every piece of storage the app sets in your browser, what each one is for, and which are optional.',
	},
] as const satisfies readonly LegalPageMeta[];

/**
 * A path in the set, as a literal union.
 *
 * `satisfies` above rather than a type annotation is what makes this possible:
 * an annotation would widen every `path` to `string` and this would be no
 * stronger than `string`, which is exactly how a caller ends up writing its own
 * fallback title beside the lookup and letting the two drift.
 */
export type LegalPath = (typeof LEGAL_PAGES)[number]['path'];

/** Look up a page's metadata by path — `undefined` if it is not in the set. */
export function legalPage(path: string): LegalPageMeta | undefined {
	return LEGAL_PAGES.find((p) => p.path === path);
}

/**
 * The title of a document that is definitely in the set.
 *
 * Total by construction — `LegalPath` is derived from the list, so an unknown
 * path is a compile error rather than a runtime gap. Use this wherever prose
 * links to a document by name: the link text and the page's own `<h1>` are then
 * the same string, and a retitled document renames every link to it at once.
 */
export function legalTitle(path: LegalPath): string {
	return LEGAL_PAGES.find((p) => p.path === path)!.title;
}
