import { describe, it, expect } from 'vitest';
import {
	INVOICE_STATUSES,
	INVOICE_STATUS_LABEL_KEYS,
	INVOICE_STATUS_TONES,
	IMMUTABLE_STATUSES,
	SYSTEM_MANAGED_STATUSES,
	VALID_TRANSITIONS,
	invoiceStatusLabelKey,
	invoiceStatusTone,
	type InvoiceStatus
} from './invoice';
import { EN_ALL } from '#lib/i18n/allMessages.ts';

function readOne(glob: Record<string, string>, label: string): string {
	const values = Object.values(glob);
	if (values.length !== 1) throw new Error(`expected exactly one file for ${label}, got ${values.length}`);
	return values[0];
}

// The backend state machine itself, read off disk: every status its
// `VALID_TRANSITIONS` names (as a source or a target) is a status an invoice
// can be in, so it is a status some badge will render.
const WORKFLOW_ENGINE = readOne(
	import.meta.glob('../../../../backend/app/services/workflow_engine.py', {
		query: '?raw',
		import: 'default',
		eager: true
	}),
	'backend/app/services/workflow_engine.py'
);
const BADGE_SOURCE = readOne(
	import.meta.glob('/src/lib/components/ui/Badge.svelte', {
		query: '?raw',
		import: 'default',
		eager: true
	}),
	'Badge.svelte'
);

function backendStateMachineStatuses(): Set<string> {
	const start = WORKFLOW_ENGINE.indexOf('VALID_TRANSITIONS: dict[');
	expect(start, 'VALID_TRANSITIONS not found in workflow_engine.py').toBeGreaterThanOrEqual(0);
	// The dict literal ends at the first line that is exactly `}`.
	const end = WORKFLOW_ENGINE.indexOf('\n}\n', start);
	expect(end).toBeGreaterThan(start);
	const block = WORKFLOW_ENGINE.slice(start, end);
	return new Set([...block.matchAll(/InvoiceStatus\.(\w+)/g)].map((match) => match[1]));
}

/**
 * Drift guard for the invoice-status LABELS, the sibling of
 * `paymentStatus.test.ts`.
 *
 * `Record<InvoiceStatus, MessageKey>` already fails `pnpm check` when a status
 * is added with no key — but a key naming nothing in the catalogue typechecks
 * fine and renders the raw key string (`m()` falls back key → raw), which is
 * how `invoices.status.readyForReview` would reach a badge as literal text.
 * This is the runtime half.
 */
describe('INVOICE_STATUS_LABEL_KEYS', () => {
	it('names a real, non-empty catalogue key for every status', () => {
		for (const status of INVOICE_STATUSES) {
			const key = INVOICE_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(EN_ALL), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(EN_ALL[key].trim().length).toBeGreaterThan(0);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of INVOICE_STATUSES) {
			expect(EN_ALL[INVOICE_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('labels every status the other status sets name', () => {
		// The three sets are read one after another on the same row: a status
		// that can be selected, transitioned to, or refused as immutable is a
		// status the UI shows the user by name. One with a set membership but
		// no label renders a blank chip / option.
		const named = new Set<string>([
			...SYSTEM_MANAGED_STATUSES,
			...IMMUTABLE_STATUSES,
			...Object.keys(VALID_TRANSITIONS),
			...Object.values(VALID_TRANSITIONS).flat()
		]);
		for (const status of named) {
			expect(invoiceStatusLabelKey(status), `${status} has no label`).toBeTruthy();
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of INVOICE_STATUSES) {
			expect(invoiceStatusLabelKey(status)).toBe(INVOICE_STATUS_LABEL_KEYS[status]);
		}
		// `StatusBadge` renders the raw value — visible and searchable —
		// rather than a blank pill for a status this build doesn't know. The
		// assistant's tool results carry a status straight off the wire, so
		// this is a real runtime path, not a theoretical one.
		expect(invoiceStatusLabelKey('archived')).toBeNull();
	});

	it('keeps `pending` labelled as the extraction step, not "Pending"', () => {
		// Called out on its own: `pending` is the one status whose label is
		// deliberately NOT its enum value — the invoice is mid-extraction, and
		// "Pending" would read as "waiting for a human" beside
		// `ready_for_review`, which is the status that actually means that.
		const status: InvoiceStatus = 'pending';
		expect(EN_ALL[INVOICE_STATUS_LABEL_KEYS[status]]).toBe('Extracting');
	});
});

/**
 * Drift guard for the invoice-status PILL colours.
 *
 * `StatusBadge` used to own one CSS rule per status and had rules for only six
 * of the twelve, so `paid`, `posted_in_erp`, `approved`, `rejected`,
 * `sending_to_erp` and `payment_scheduled` rendered as bare uppercase text on
 * `/invoices` beside a green `DONE` pill. A missing class is invisible to the
 * typechecker; these assertions are what make it a red test instead.
 */
describe('INVOICE_STATUS_TONES', () => {
	// The `.<tone> { background: …; color: … }` rules `Badge.svelte` actually
	// ships. A tone with no rule here would be the same bug one layer down.
	const styledTones = new Set(
		[...BADGE_SOURCE.matchAll(/^\s*\.([a-z]+)\s*\{[^}]*\bbackground\s*:[^}]*\bcolor\s*:/gm)].map(
			(match) => match[1]
		)
	);

	it('reads a non-trivial state machine and a styled tone set', () => {
		// Without these the loops below could pass vacuously on a parse miss.
		expect(backendStateMachineStatuses().size).toBeGreaterThanOrEqual(12);
		expect(styledTones.size).toBeGreaterThanOrEqual(5);
	});

	it('gives every status in the frontend union a tone Badge styles', () => {
		for (const status of INVOICE_STATUSES) {
			const tone = INVOICE_STATUS_TONES[status];
			expect(tone, `${status} has no badge tone`).toBeTruthy();
			expect(styledTones, `${status} → "${tone}" has no rule in Badge.svelte`).toContain(tone);
		}
	});

	it('covers every status the backend state machine names', () => {
		for (const status of backendStateMachineStatuses()) {
			expect(
				INVOICE_STATUSES as string[],
				`backend status "${status}" is missing from INVOICE_STATUSES`
			).toContain(status);
			const tone = invoiceStatusTone(status);
			expect(tone, `backend status "${status}" would render as an untinted chip`).not.toBe(
				'neutral'
			);
			expect(styledTones).toContain(tone);
		}
	});

	it('gives every status the other status sets name a tinted tone', () => {
		const named = new Set<string>([
			...SYSTEM_MANAGED_STATUSES,
			...IMMUTABLE_STATUSES,
			...Object.keys(VALID_TRANSITIONS),
			...Object.values(VALID_TRANSITIONS).flat()
		]);
		for (const status of named) {
			expect(invoiceStatusTone(status), `${status} has no tone`).not.toBe('neutral');
		}
	});

	it('paints the settled end of the pipeline alike — paid and done are both success', () => {
		// The reported bug: DONE was green, PAID beside it was bare text.
		expect(INVOICE_STATUS_TONES.paid).toBe(INVOICE_STATUS_TONES.done);
		expect(INVOICE_STATUS_TONES.done).toBe('success');
	});

	it('is what StatusBadge renders, through Badge, with no colour rules of its own', () => {
		// The map is only a guard if the component reads it: a per-status
		// stylesheet in StatusBadge is the shape that dropped six statuses.
		const statusBadge = readOne(
			import.meta.glob('/src/lib/components/ui/StatusBadge.svelte', {
				query: '?raw',
				import: 'default',
				eager: true
			}),
			'StatusBadge.svelte'
		);
		expect(statusBadge).toContain('invoiceStatusTone(');
		expect(statusBadge).toMatch(/<Badge\b/);
		expect(statusBadge).not.toMatch(/<style\b/);
	});

	it('falls back to the flat neutral chip for a status this build does not know', () => {
		expect(invoiceStatusTone('archived')).toBe('neutral');
		expect(styledTones).toContain('neutral');
	});
});
