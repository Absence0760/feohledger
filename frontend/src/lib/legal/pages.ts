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

export const LEGAL_PAGES: readonly LegalPageMeta[] = [
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
		path: '/legal/cookies',
		title: 'Cookie Notice',
		blurb:
			'Every piece of storage the app sets in your browser, what each one is for, and which are optional.',
	},
] as const;

/** Look up a page's metadata by path — `undefined` if it is not in the set. */
export function legalPage(path: string): LegalPageMeta | undefined {
	return LEGAL_PAGES.find((p) => p.path === path);
}
