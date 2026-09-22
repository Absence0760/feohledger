import { describe, expect, it } from 'vitest';
import { en } from '$lib/i18n/locales/en';
import {
	EXCEPTION_SEVERITIES,
	EXCEPTION_SEVERITY_LABEL_KEYS,
	EXCEPTION_STATUS_LABEL_KEYS,
	EXCEPTION_STATUS_TONES,
	EXCEPTION_STATUSES,
	EXCEPTION_TYPES,
	EXCEPTION_TYPE_LABEL_KEYS,
	exceptionSeverityLabelKey,
	exceptionStatusLabelKey,
	exceptionStatusTone,
	exceptionTypeFallback,
	exceptionTypeLabelKey
} from './exception';

/**
 * Drift guard: the frontend's exception-type roster and its ENGLISH labels must
 * equal the backend's, which owns both.
 *
 * Two halves, and the second is the one that matters:
 *
 *  1. The roster — `exception_lifecycle.EXCEPTION_TYPES`. `Exception.exception_type`
 *     is a plain `String(50)`, so a type raised with no entry here renders
 *     through the tolerant fallback (de-underscored raw key) rather than a
 *     label. Silent, and in the wrong direction: two of these types are the
 *     ones that block a payment run.
 *  2. The WORDING — `api/exceptions.py::EXCEPTION_TYPE_LABELS`. The queue still
 *     renders the server's `type_label` off that map while the agent decision
 *     log renders `EXCEPTION_TYPE_LABEL_KEYS`, so if the two disagree in
 *     English the same exception reads differently on two tabs of one page.
 *     That is exactly the defect this module was added to fix (the log printed
 *     `po mismatch` where the queue said `PO Mismatch`), so it ships with the
 *     equality asserted rather than observed once.
 *
 * Reads the real Python through Vite's `import.meta.glob`, the way
 * `notification.roster.test.ts` does — the frontend deliberately carries no
 * `@types/node`, so `node:fs` would run under vitest and fail `pnpm check`.
 */

const RAW = import.meta.glob(
	[
		'../../../../backend/app/api/exceptions.py',
		'../../../../backend/app/models/exception.py',
		'../../../../backend/app/services/exception_lifecycle.py'
	],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

/**
 * Every module that can construct an `Exception(...)`, for the severity roster.
 *
 * Wider than the three files above on purpose: the severity vocabulary has no
 * backend constant to read, so the only complete statement of it is the set of
 * literals the raising sites write. A narrower, hand-listed glob would go stale
 * the first time a new service raises one — which is precisely the drift this
 * guard exists to catch.
 */
const BACKEND_SOURCES = import.meta.glob(
	['../../../../backend/app/api/*.py', '../../../../backend/app/services/**/*.py'],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

function source(suffix: string): string {
	const hit = Object.entries(RAW).find(([path]) => path.endsWith(suffix));
	expect(hit, `${suffix} not found through import.meta.glob`).toBeDefined();
	return hit![1];
}

/** The `EXCEPTION_TYPES` tuple in declaration order. */
function backendRoster(): string[] {
	const py = source('exception_lifecycle.py');
	const block = /EXCEPTION_TYPES:\s*tuple\[str, \.\.\.\]\s*=\s*\(([\s\S]*?)\n\)/.exec(py);
	expect(block, 'EXCEPTION_TYPES tuple not found — did it move or change shape?').not.toBeNull();
	return [...block![1].matchAll(/"([a-z0-9_]+)"/g)].map((m) => m[1]);
}

/** The `EXCEPTION_TYPE_LABELS` map, type → English label, in declaration order. */
function backendLabels(): Record<string, string> {
	const py = source('api/exceptions.py');
	const block = /EXCEPTION_TYPE_LABELS\s*=\s*\{([\s\S]*?)\n\}/.exec(py);
	expect(block, 'EXCEPTION_TYPE_LABELS map not found — did it move or change shape?').not.toBeNull();
	const out: Record<string, string> = {};
	for (const m of block![1].matchAll(/"([a-z0-9_]+)":\s*"([^"]*)"/g)) out[m[1]] = m[2];
	return out;
}

/**
 * The lifecycle status vocabulary, rebuilt from the two backend constants that
 * between them produce every value the column can hold: `ACTIONABLE_STATUSES`
 * (the states a queue verb can still be applied from — which includes the
 * `open` column default) and `RESOLUTION_STATUSES` (verb → the status it
 * writes). A fifth queue verb, or a new actionable state, fails here.
 */
function backendStatuses(): Set<string> {
	const py = source('exception_lifecycle.py');
	const res = /RESOLUTION_STATUSES:\s*dict\[str, str\]\s*=\s*\{([\s\S]*?)\n\}/.exec(py);
	expect(res, 'RESOLUTION_STATUSES map not found — did it move or change shape?').not.toBeNull();
	const produced = [...res![1].matchAll(/:\s*"([a-z0-9_]+)"/g)].map((m) => m[1]);
	const actionable = /ACTIONABLE_STATUSES\s*=\s*\(([^)]*)\)/.exec(py);
	expect(
		actionable,
		'ACTIONABLE_STATUSES tuple not found — did it move or change shape?'
	).not.toBeNull();
	const open = [...actionable![1].matchAll(/"([a-z0-9_]+)"/g)].map((m) => m[1]);
	return new Set([...open, ...produced]);
}

/**
 * The statuses `GET /api/exceptions/summary` counts, in its own order — which
 * IS the queue's chip order, because the chips render one per count.
 */
function summaryStatuses(): string[] {
	const py = source('api/exceptions.py');
	const rows = [...py.matchAll(/"([a-z0-9_]+)":\s*by_status\.get\("([a-z0-9_]+)", 0\)/g)];
	expect(rows.length, 'the /summary by_status block stopped matching').toBeGreaterThan(3);
	for (const row of rows) {
		expect(row[1], 'a /summary key disagrees with the status it counts').toBe(row[2]);
	}
	return rows.map((row) => row[1]);
}

describe('exception-type taxonomy', () => {
	it('carries the backend roster, in the backend order', () => {
		const roster = backendRoster();
		expect(roster.length, 'parsed an empty roster — the regex stopped matching').toBeGreaterThan(
			10
		);
		expect([...EXCEPTION_TYPES]).toEqual(roster);
	});

	it('labels every type the backend labels, and nothing it does not', () => {
		expect(Object.keys(EXCEPTION_TYPE_LABEL_KEYS).sort()).toEqual(
			Object.keys(backendLabels()).sort()
		);
	});

	it('reads byte-identically to the backend label in English', () => {
		// The queue renders the server's `type_label`; the agent log renders the
		// key. A divergence here is one exception wearing two names in one page.
		const labels = backendLabels();
		const enRecord = en as Record<string, string>;
		for (const type of EXCEPTION_TYPES) {
			expect(enRecord[EXCEPTION_TYPE_LABEL_KEYS[type]], `${type} label drifted`).toBe(
				labels[type]
			);
		}
	});

	it('resolves every key in the English catalogue', () => {
		for (const key of Object.values(EXCEPTION_TYPE_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('returns null for a type this build has no wording for', () => {
		// A historical row can carry a type this frontend predates. The caller
		// then renders the server's label, or the de-underscored raw key — never
		// an empty cell.
		expect(exceptionTypeLabelKey('duplicate')).toBe('exceptions.type.duplicate');
		expect(exceptionTypeLabelKey('some_future_type')).toBeNull();
		expect(exceptionTypeFallback('some_future_type')).toBe('some future type');
	});
});

describe('exception lifecycle status vocabulary', () => {
	it('carries the backend roster, in the order /summary counts it', () => {
		// `/summary`'s order is the chip order, and the chips are what these
		// labels are shared with — so the roster is pinned against that block
		// rather than against a sorted set.
		expect([...EXCEPTION_STATUSES]).toEqual(summaryStatuses());
		expect(new Set(EXCEPTION_STATUSES)).toEqual(backendStatuses());
	});

	it('reuses the queue filter-chip keys rather than a second key set', () => {
		// The load-bearing property. A chip and the badges it filters name ONE
		// status; two key sets is how they come to name it two ways the first
		// time a translator revises one of them. There must be no
		// `exceptions.status.*` namespace at all.
		for (const status of EXCEPTION_STATUSES) {
			expect(EXCEPTION_STATUS_LABEL_KEYS[status]).toBe(`exceptions.filter.${status}`);
		}
	});

	it('resolves every key in the English catalogue', () => {
		for (const key of Object.values(EXCEPTION_STATUS_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('tones exactly the statuses it labels', () => {
		// Both records are total over the union, so a status that gains a colour
		// without a label — a tinted pill printing a raw wire value — cannot
		// compile. Asserted too, so a widened `Record<string, …>` is caught.
		expect(Object.keys(EXCEPTION_STATUS_TONES).sort()).toEqual(
			Object.keys(EXCEPTION_STATUS_LABEL_KEYS).sort()
		);
	});

	it('is tolerant: an unknown status keeps its raw value and the flat chip', () => {
		// `status` is a plain `String(30)` with no DB enum, so a row written by a
		// later build can carry a status this one predates. The caller then
		// prints what every surface printed before this map existed.
		expect(exceptionStatusLabelKey('escalated')).toBe('exceptions.filter.escalated');
		expect(exceptionStatusLabelKey('awaiting_vendor')).toBeNull();
		expect(exceptionStatusTone('escalated')).toBe('danger');
		expect(exceptionStatusTone('awaiting_vendor')).toBe('neutral');
	});
});

describe('exception severity vocabulary', () => {
	/**
	 * The backend declares the severity roster in a COMMENT on the column and
	 * nowhere else:
	 *
	 *     severity: Mapped[str] = mapped_column(String(20), default="warning")  # error, warning, info
	 *
	 * The backend now also has a real constant — `EXCEPTION_SEVERITY_RANK` in
	 * `services/exception_lifecycle.py`, which the queue's severity SORT ranks
	 * by — so the guard reads all three: that map (roster AND order, since it is
	 * declared worst-first like the chips), the comment, and every
	 * `severity="…"` a raising site actually writes. A fourth severity fails
	 * here whichever way it arrives. The comment and literal scans stay because
	 * the map is only as complete as its own backend guard makes it.
	 */
	function backendRank(): string[] {
		const py = source('services/exception_lifecycle.py');
		const block = /EXCEPTION_SEVERITY_RANK[^=]*=\s*\{([^}]*)\}/.exec(py);
		expect(block, 'EXCEPTION_SEVERITY_RANK not found — did it move or change shape?').not.toBeNull();
		return [...block![1].matchAll(/"([a-z0-9_]+)":\s*\d+/g)].map((m) => m[1]);
	}

	function modelComment(): string[] {
		const py = source('models/exception.py');
		const line = /severity:\s*Mapped\[str\][^\n]*?#\s*([a-z0-9_, ]+)/.exec(py);
		expect(line, 'the severity column comment stopped matching — did it move or change shape?')
			.not.toBeNull();
		return line![1]
			.split(',')
			.map((s) => s.trim())
			.filter(Boolean);
	}

	/** The column default, which must itself be a severity we can label. */
	function modelDefault(): string {
		const py = source('models/exception.py');
		const hit = /severity:\s*Mapped\[str\]\s*=\s*mapped_column\([^\n]*?default="([a-z_]+)"/.exec(py);
		expect(hit, 'the severity column default stopped matching').not.toBeNull();
		return hit![1];
	}

	/** Every `severity="…"` literal anywhere a backend module raises one. */
	function raisedSeverities(): Set<string> {
		const found = new Set<string>();
		for (const py of Object.values(BACKEND_SOURCES)) {
			for (const hit of py.matchAll(/\bseverity="([a-z_]+)"/g)) found.add(hit[1]);
		}
		expect(found.size, 'no severity literals found — the scan stopped matching').toBeGreaterThan(
			1
		);
		return found;
	}

	it('carries the roster the column comment declares', () => {
		expect([...EXCEPTION_SEVERITIES]).toEqual(modelComment());
	});

	it('carries the backend rank map, in its worst-first order', () => {
		// The chips render in `EXCEPTION_SEVERITIES` order and the queue sorts by
		// the backend's rank, so the two must agree on the order as well as the
		// members — a chip row reading info / warning / error above a table
		// sorted error-first would be two answers to one question.
		expect([...EXCEPTION_SEVERITIES]).toEqual(backendRank());
	});

	it('labels the column default', () => {
		// A row that never had a severity set still renders one.
		expect(EXCEPTION_SEVERITY_LABEL_KEYS).toHaveProperty(modelDefault());
	});

	it('labels every severity a backend module actually raises', () => {
		// The half that catches a real fourth value: a new service can write
		// `severity="critical"` without anyone touching the column's comment,
		// and the cell would silently print `critical` in lowercase Latin.
		const unlabelled = [...raisedSeverities()]
			.filter((s) => !(s in EXCEPTION_SEVERITY_LABEL_KEYS))
			.sort();
		expect(unlabelled, 'these severities are raised but have no label').toEqual([]);
	});

	it('resolves every key in the English catalogue', () => {
		for (const key of Object.values(EXCEPTION_SEVERITY_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('is tolerant: an unknown severity keeps its raw value', () => {
		expect(exceptionSeverityLabelKey('error')).toBe('exceptions.severity.error');
		expect(exceptionSeverityLabelKey('critical')).toBeNull();
	});
});
