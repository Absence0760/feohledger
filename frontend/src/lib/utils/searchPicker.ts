/**
 * Pure logic behind `ui/SearchPicker.svelte` — the searchable, server-paged
 * combobox that `ui/VendorPicker` and `ui/InvoicePicker` are both built on.
 *
 * It lives outside the component for the usual reason: the parts that decide
 * what the user can reach (which option Enter commits, whether the list on
 * screen is the whole match set, what the count line says and when) are
 * exactly the parts worth testing without a DOM, and they are what the picker
 * exists to get right. The component owns fetching, focus and markup; this
 * module owns the rules.
 *
 * Background: the vendor field on five surfaces was a native `<select>` over a
 * client-side list — two capped at the first 100 rows, three walking every page
 * on mount (`docs/decisions.md` §145) — and the two `/credit-memos` invoice
 * selects walked every INVOICE page on mount and filtered by vendor in the
 * browser (§202). A native `<select>` has no search, so the only ways to make
 * a long list reachable were to fetch all of it or to cut it off silently.
 */

/** One page of options, as the picker's `load` function returns it. */
export interface SearchPickerPage<T> {
	items: T[];
	/** The size of the WHOLE matching set, not of this page. */
	total: number;
}

/**
 * What the picker calls to fetch a page. It is the picker's SOURCE: the parent
 * binds whatever narrows the set (a memo id, a chosen vendor) into it, and a
 * new function identity means a new set — the picker discards what it holds
 * for the old one.
 */
export type SearchPickerLoad<T> = (query: {
	search: string;
	page: number;
	page_size: number;
}) => Promise<SearchPickerPage<T>>;

/**
 * Every string the picker renders, already localized by the wrapper — the
 * component itself is i18n-agnostic (the `FieldWarning` / `SecretReveal`
 * convention), so each wrapper keeps its own key namespace.
 */
export interface SearchPickerText {
	loading: string;
	/** A failed LIST — nothing on screen. Must not read as an empty set. */
	loadFailed: string;
	/** The unfiltered set is empty. */
	empty: string;
	noMatches: (query: string) => string;
	showingAll: (total: number) => string;
	showingPartial: (shown: number, total: number) => string;
	refineHint: string;
	loadMore: string;
	/** A failed NEXT page — the page that landed is still valid. */
	loadMoreFailed: string;
	clearAria: string;
	listAria: string;
	/** An id is committed that nothing on this screen can name. */
	unresolvedSelection: string;
}

/**
 * How many options one page of the picker asks the server for.
 *
 * Deliberately well under the server's `MAX_PAGE_SIZE` of 100: the popup is a
 * browsing aid, not the reach mechanism — `search=` is. A bigger first page
 * only makes the popup longer to scan while moving the same cliff further out.
 */
export const SEARCH_PICKER_PAGE_SIZE = 25;

/**
 * Debounce for the search box, in ms. Matches the `/catalogs` and `/vendors`
 * list surfaces: a cleared box fires immediately (the user is asking for the
 * unfiltered list back and should not wait for it), typing waits.
 */
export function searchPickerDelay(query: string): number {
	return query.trim() === '' ? 0 : 280;
}

/**
 * How much of the matching set is on screen.
 *
 * `partial` is the whole point: the user must never be shown a subset of the
 * matches without being told it is a subset. `loading` is distinct from `none`
 * — "we have not looked yet" and "there is nothing" are different answers, and
 * reading one as the other is how a truncated list reads as an empty set.
 */
export type SearchPickerCount =
	| { kind: 'loading' }
	| { kind: 'none' }
	| { kind: 'all'; total: number }
	| { kind: 'partial'; shown: number; total: number };

export function searchPickerCount(
	shown: number,
	total: number,
	loading: boolean
): SearchPickerCount {
	if (loading && shown === 0) return { kind: 'loading' };
	if (total <= 0) return { kind: 'none' };
	// `shown > total` is not impossible — the set can shrink between the page
	// fetch and the `total` that came with it. Report that as complete rather
	// than as a negative remainder, which would render "-2 more".
	if (shown >= total) return { kind: 'all', total };
	return { kind: 'partial', shown, total };
}

/** True when another page of matches exists beyond what is loaded. */
export function hasMoreOptions(shown: number, total: number): boolean {
	return searchPickerCount(shown, total, false).kind === 'partial';
}

/** Which fetch failed, if any: the LIST (nothing on screen) or just the NEXT page. */
export type SearchPickerFailure = 'none' | 'list' | 'more';

/** What the count line under the input says — the component maps each kind to copy. */
export type SearchPickerStatus =
	| { kind: 'silent' }
	| { kind: 'unresolved' }
	| { kind: 'loading' }
	| { kind: 'loadFailed' }
	| { kind: 'empty' }
	| { kind: 'noMatches'; query: string }
	| { kind: 'all'; total: number }
	| { kind: 'partial'; shown: number; total: number };

export interface SearchPickerStatusInput {
	open: boolean;
	count: SearchPickerCount;
	failure: SearchPickerFailure;
	/** What the user has typed — `''` when they are not typing. */
	searchTerm: string;
	/**
	 * The term the settled result (or failure) on screen answers, or `null`
	 * when nothing has settled for the picker's CURRENT source. A result for a
	 * previous source, or one still in flight, is not an answer.
	 */
	answeredTerm: string | null;
	/** A value is committed that the picker cannot label. */
	unresolvedSelection: boolean;
	/**
	 * Whether the CLOSED control reports an empty or unreachable set. True for
	 * a picker that preloads (`ui/InvoicePicker`): there the set is the
	 * dialog's whole purpose, and "there is nothing to pick" is what the user
	 * needs to know before they open it. A picker that only fetches on open
	 * stays silent at rest, because its last answer may be for a search the
	 * user has since abandoned.
	 */
	announceAtRest: boolean;
}

export function searchPickerStatus(s: SearchPickerStatusInput): SearchPickerStatus {
	if (!s.open) {
		// Resting state: silent, unless the field is holding a value it cannot
		// name — or, for a preloading picker, the UNFILTERED set it last
		// fetched is empty or could not be fetched. The answer must be for the
		// unfiltered set: an abandoned search's "no matches" says nothing about
		// the set, and the closed box no longer shows the term it was for.
		if (s.unresolvedSelection) return { kind: 'unresolved' };
		if (!s.announceAtRest || s.answeredTerm !== '') return { kind: 'silent' };
		if (s.failure === 'list') return { kind: 'loadFailed' };
		if (s.count.kind === 'none') return { kind: 'empty' };
		return { kind: 'silent' };
	}
	// Loading outranks the previous failure: a retry that is genuinely in flight
	// must not still read as "couldn't load".
	if (s.count.kind === 'loading') return { kind: 'loading' };
	if (s.failure === 'list') return { kind: 'loadFailed' };
	switch (s.count.kind) {
		case 'none':
			return s.searchTerm ? { kind: 'noMatches', query: s.searchTerm } : { kind: 'empty' };
		case 'all':
			return { kind: 'all', total: s.count.total };
		case 'partial':
			return { kind: 'partial', shown: s.count.shown, total: s.count.total };
	}
}

/** The copy for a status, from the wrapper's localized strings. `''` = say nothing. */
export function searchPickerStatusText(status: SearchPickerStatus, text: SearchPickerText): string {
	switch (status.kind) {
		case 'silent':
			return '';
		case 'unresolved':
			return text.unresolvedSelection;
		case 'loading':
			return text.loading;
		case 'loadFailed':
			return text.loadFailed;
		case 'empty':
			return text.empty;
		case 'noMatches':
			return text.noMatches(status.query);
		case 'all':
			return text.showingAll(status.total);
		case 'partial':
			return text.showingPartial(status.shown, status.total);
	}
}

/**
 * The roving `aria-activedescendant` index after a navigation key.
 *
 * `current` is -1 when no option is active — the state the list resets to on
 * every new result set, so that Enter can never commit a row the user has not
 * looked at. Returns `null` when the key is not a navigation key, so the caller
 * leaves that event alone.
 */
export function nextActiveIndex(key: string, current: number, count: number): number | null {
	if (key !== 'ArrowDown' && key !== 'ArrowUp' && key !== 'Home' && key !== 'End') return null;
	if (count <= 0) return -1;
	switch (key) {
		case 'ArrowDown':
			// From "nothing active" the first press lands on the FIRST option.
			// `(-1 + 1) % count` happens to give that too, but say it explicitly
			// so the wrap arithmetic can't quietly change it later.
			return current < 0 ? 0 : (current + 1) % count;
		case 'ArrowUp':
			return current < 0 ? count - 1 : (current - 1 + count) % count;
		case 'Home':
			return 0;
		default:
			return count - 1;
	}
}

/**
 * Which option Enter commits.
 *
 * The active one when the user has arrowed to a row. Otherwise the sole option,
 * when the search has narrowed to exactly one — typing a full name or number
 * and pressing Enter is the fastest path through this control, and refusing it
 * because no row was arrowed to would cost a pointless keystroke. With two or
 * more candidates and nothing active there is no defensible pick, so: nothing.
 */
export function resolveEnterSelection<T>(activeIndex: number, options: readonly T[]): T | null {
	if (activeIndex >= 0 && activeIndex < options.length) return options[activeIndex];
	if (activeIndex < 0 && options.length === 1) return options[0];
	return null;
}

/**
 * The text the input reverts to when it loses focus.
 *
 * A combobox whose text can disagree with its committed value is a lie: the
 * field would read "Acme Manufact…" while the form submits whichever option was
 * picked before that. Reverting on blur is also what keeps a native `required`
 * on the input honest — non-empty text then means exactly "a value is
 * committed", which is the condition the consuming form actually gates on.
 */
export function revertedQuery(selectedLabel: string | null): string {
	return selectedLabel ?? '';
}
