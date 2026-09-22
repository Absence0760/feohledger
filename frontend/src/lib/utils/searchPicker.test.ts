import { describe, expect, it } from 'vitest';
import {
	SEARCH_PICKER_PAGE_SIZE,
	hasMoreOptions,
	nextActiveIndex,
	resolveEnterSelection,
	revertedQuery,
	searchPickerCount,
	searchPickerDelay,
	searchPickerStatus,
	searchPickerStatusText,
	type SearchPickerStatusInput,
	type SearchPickerText
} from './searchPicker';

const opt = (id: string, name: string) => ({ id, name });

describe('searchPickerDelay', () => {
	it('fires immediately when the box is cleared', () => {
		// Clearing is a request for the unfiltered list back; waiting 280ms to
		// give it is the one case where the debounce costs more than it saves.
		expect(searchPickerDelay('')).toBe(0);
		expect(searchPickerDelay('   ')).toBe(0);
	});

	it('debounces real typing', () => {
		expect(searchPickerDelay('ac')).toBe(280);
	});
});

describe('searchPickerCount', () => {
	it('reports loading only while nothing is on screen yet', () => {
		// "We have not looked" and "there is nothing" are different answers, and
		// reading one as the other is how a truncated list reads as an empty
		// set. Once a page has landed, a refresh keeps showing the count.
		expect(searchPickerCount(0, 0, true)).toEqual({ kind: 'loading' });
		expect(searchPickerCount(25, 137, true)).toEqual({ kind: 'partial', shown: 25, total: 137 });
	});

	it('reports an empty match set', () => {
		expect(searchPickerCount(0, 0, false)).toEqual({ kind: 'none' });
	});

	it('reports a complete match set', () => {
		expect(searchPickerCount(12, 12, false)).toEqual({ kind: 'all', total: 12 });
	});

	it('reports a partial match set — the whole reason this module exists', () => {
		expect(searchPickerCount(25, 137, false)).toEqual({
			kind: 'partial',
			shown: 25,
			total: 137
		});
	});

	it('treats shown > total as complete, never as a negative remainder', () => {
		// The set can shrink between the page fetch and the `total` it came with.
		// "-2 more" is worse than a slightly stale "all 23".
		expect(searchPickerCount(25, 23, false)).toEqual({ kind: 'all', total: 23 });
	});
});

describe('hasMoreOptions', () => {
	it('is true exactly when the loaded page is a subset', () => {
		expect(hasMoreOptions(25, 137)).toBe(true);
		expect(hasMoreOptions(12, 12)).toBe(false);
		expect(hasMoreOptions(0, 0)).toBe(false);
		expect(hasMoreOptions(25, 23)).toBe(false);
	});
});

describe('searchPickerStatus', () => {
	const base: SearchPickerStatusInput = {
		open: false,
		count: { kind: 'none' },
		failure: 'none',
		searchTerm: '',
		answeredTerm: '',
		unresolvedSelection: false,
		announceAtRest: false
	};

	it('is silent at rest for a picker that only fetches on open', () => {
		// VendorPicker: its last answer may be for a search the user abandoned,
		// and the closed box no longer shows the term it was for.
		expect(searchPickerStatus(base)).toEqual({ kind: 'silent' });
		expect(searchPickerStatus({ ...base, failure: 'list' })).toEqual({ kind: 'silent' });
	});

	it('names an unresolvable committed value at rest, whatever else holds', () => {
		expect(searchPickerStatus({ ...base, unresolvedSelection: true })).toEqual({
			kind: 'unresolved'
		});
		expect(
			searchPickerStatus({ ...base, unresolvedSelection: true, announceAtRest: true })
		).toEqual({ kind: 'unresolved' });
	});

	describe('a preloading picker at rest', () => {
		const resting = { ...base, announceAtRest: true };

		it('says the unfiltered set is empty before the user opens it', () => {
			// The Apply dialog's whole purpose is picking an invoice: "there is
			// nothing this credit can go on" is the first thing to say, not a
			// discovery to make by opening an empty popup.
			expect(searchPickerStatus(resting)).toEqual({ kind: 'empty' });
		});

		it('says the set could not be loaded, which is not the same as empty', () => {
			expect(searchPickerStatus({ ...resting, failure: 'list' })).toEqual({
				kind: 'loadFailed'
			});
		});

		it('says nothing while the preload is in flight or before one has landed', () => {
			expect(
				searchPickerStatus({ ...resting, count: { kind: 'loading' }, answeredTerm: null })
			).toEqual({ kind: 'silent' });
		});

		it('says nothing when the set has options', () => {
			expect(searchPickerStatus({ ...resting, count: { kind: 'all', total: 3 } })).toEqual({
				kind: 'silent'
			});
		});

		it('never reports an abandoned search as an empty set', () => {
			// The user searched "zzz", got no matches and closed the popup: the
			// set itself is not empty, and the closed box no longer shows "zzz".
			expect(searchPickerStatus({ ...resting, answeredTerm: 'zzz' })).toEqual({
				kind: 'silent'
			});
			expect(
				searchPickerStatus({ ...resting, answeredTerm: 'zzz', failure: 'list' })
			).toEqual({ kind: 'silent' });
		});
	});

	describe('open', () => {
		const open = { ...base, open: true };

		it('reports loading ahead of a previous failure', () => {
			expect(
				searchPickerStatus({ ...open, count: { kind: 'loading' }, failure: 'list' })
			).toEqual({ kind: 'loading' });
		});

		it('reports a failed list as a failure, never as an empty set', () => {
			expect(searchPickerStatus({ ...open, failure: 'list' })).toEqual({ kind: 'loadFailed' });
		});

		it('tells no-matches from an empty set by whether the user searched', () => {
			expect(searchPickerStatus(open)).toEqual({ kind: 'empty' });
			expect(searchPickerStatus({ ...open, searchTerm: 'INV-9' })).toEqual({
				kind: 'noMatches',
				query: 'INV-9'
			});
		});

		it('states how much of the matching set is on screen', () => {
			expect(searchPickerStatus({ ...open, count: { kind: 'all', total: 4 } })).toEqual({
				kind: 'all',
				total: 4
			});
			expect(
				searchPickerStatus({ ...open, count: { kind: 'partial', shown: 25, total: 90 } })
			).toEqual({ kind: 'partial', shown: 25, total: 90 });
		});

		it('keeps reporting the page that landed when only the next page failed', () => {
			expect(
				searchPickerStatus({
					...open,
					failure: 'more',
					count: { kind: 'partial', shown: 25, total: 90 }
				})
			).toEqual({ kind: 'partial', shown: 25, total: 90 });
		});
	});
});

describe('searchPickerStatusText', () => {
	const text: SearchPickerText = {
		loading: 'L',
		loadFailed: 'F',
		empty: 'E',
		noMatches: (q) => `N:${q}`,
		showingAll: (t) => `A:${t}`,
		showingPartial: (s, t) => `P:${s}/${t}`,
		refineHint: 'R',
		loadMore: 'M',
		loadMoreFailed: 'MF',
		clearAria: 'C',
		listAria: 'LA',
		unresolvedSelection: 'U'
	};

	it('maps every status to its wrapper-supplied copy', () => {
		expect(searchPickerStatusText({ kind: 'silent' }, text)).toBe('');
		expect(searchPickerStatusText({ kind: 'unresolved' }, text)).toBe('U');
		expect(searchPickerStatusText({ kind: 'loading' }, text)).toBe('L');
		expect(searchPickerStatusText({ kind: 'loadFailed' }, text)).toBe('F');
		expect(searchPickerStatusText({ kind: 'empty' }, text)).toBe('E');
		expect(searchPickerStatusText({ kind: 'noMatches', query: 'x' }, text)).toBe('N:x');
		expect(searchPickerStatusText({ kind: 'all', total: 2 }, text)).toBe('A:2');
		expect(searchPickerStatusText({ kind: 'partial', shown: 1, total: 2 }, text)).toBe('P:1/2');
	});
});

describe('nextActiveIndex', () => {
	it('ignores keys that are not navigation', () => {
		expect(nextActiveIndex('a', 0, 5)).toBeNull();
		expect(nextActiveIndex('Enter', 0, 5)).toBeNull();
		expect(nextActiveIndex('Escape', 0, 5)).toBeNull();
		expect(nextActiveIndex('Tab', 0, 5)).toBeNull();
	});

	it('lands on the first option from nothing-active on ArrowDown', () => {
		expect(nextActiveIndex('ArrowDown', -1, 5)).toBe(0);
	});

	it('lands on the last option from nothing-active on ArrowUp', () => {
		expect(nextActiveIndex('ArrowUp', -1, 5)).toBe(4);
	});

	it('wraps in both directions', () => {
		expect(nextActiveIndex('ArrowDown', 4, 5)).toBe(0);
		expect(nextActiveIndex('ArrowUp', 0, 5)).toBe(4);
	});

	it('steps through the middle of the list', () => {
		expect(nextActiveIndex('ArrowDown', 1, 5)).toBe(2);
		expect(nextActiveIndex('ArrowUp', 3, 5)).toBe(2);
	});

	it('honours Home and End', () => {
		expect(nextActiveIndex('Home', 3, 5)).toBe(0);
		expect(nextActiveIndex('End', 1, 5)).toBe(4);
	});

	it('deactivates rather than pointing past the end of an empty list', () => {
		for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End']) {
			expect(nextActiveIndex(key, -1, 0)).toBe(-1);
		}
	});
});

describe('resolveEnterSelection', () => {
	const three = [opt('a', 'Alpha'), opt('b', 'Bravo'), opt('c', 'Charlie')];

	it('commits the arrowed-to option', () => {
		expect(resolveEnterSelection(1, three)).toEqual(three[1]);
	});

	it('commits the sole match when nothing was arrowed to', () => {
		expect(resolveEnterSelection(-1, [three[2]])).toEqual(three[2]);
	});

	it('commits nothing when several match and none is active', () => {
		// There is no defensible pick here, and guessing one commits a payee (or
		// an invoice to credit) the user never looked at.
		expect(resolveEnterSelection(-1, three)).toBeNull();
	});

	it('commits nothing on an empty list', () => {
		expect(resolveEnterSelection(-1, [])).toBeNull();
		expect(resolveEnterSelection(0, [])).toBeNull();
	});

	it('commits nothing when the active index outran a shrunken list', () => {
		expect(resolveEnterSelection(7, three)).toBeNull();
	});
});

describe('revertedQuery', () => {
	it('restores the committed label on blur', () => {
		expect(revertedQuery('Acme Corp (V-001)')).toBe('Acme Corp (V-001)');
	});

	it('empties the box when nothing is committed', () => {
		// This is what keeps a native `required` on the input honest: non-empty
		// text means exactly "a value is committed".
		expect(revertedQuery(null)).toBe('');
	});
});

describe('SEARCH_PICKER_PAGE_SIZE', () => {
	it('stays under the server MAX_PAGE_SIZE of 100', () => {
		expect(SEARCH_PICKER_PAGE_SIZE).toBeGreaterThan(0);
		expect(SEARCH_PICKER_PAGE_SIZE).toBeLessThanOrEqual(100);
	});
});
