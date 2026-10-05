import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import {
	EVENT_LABELS,
	EVENT_LABEL_KEYS,
	EVENT_ORDER,
	normalizePrefs,
	type NotificationEventType
} from './notification';

/**
 * Roster drift guard: the frontend's notifiable-event list must equal the
 * backend's, in the same order.
 *
 * The server owns the vocabulary
 * (`backend/app/models/notification.py::NOTIFICATION_EVENT_TYPES`), and the
 * drift is **silent in the dangerous direction**:
 * `services/notification_dispatch.resolve_prefs` defaults a *missing*
 * preference key to **on**, so an event the frontend never renders a toggle
 * for is an event the user is subscribed to with no way to unsubscribe. That
 * is exactly how `contract_renewal_due`, `chat_message` and
 * `cash_shortfall_projected` shipped un-mutable — `chat_message` emails the AP
 * team on every supplier-portal message.
 *
 * Mirrors the backend's own source-scan drift guards
 * (`backend/tests/test_payment_methods.py`,
 * `backend/tests/test_exception_type_labels.py`) and reads the tree through
 * Vite's `import.meta.glob` for the same reason
 * `lib/a11y/tokenPairing.test.ts` and `lib/utils/effectTimerCleanup.test.ts`
 * do — the frontend deliberately carries no `@types/node`, so `node:fs` would
 * run under vitest but fail `pnpm check`. The glob reaches up out of
 * `frontend/` into the monorepo's `backend/`, so this reads the *real* Python
 * source rather than a copy that would need its own guard.
 *
 * **Known companion gap** — this guard covers the MODEL roster only. The wire
 * contract also has to carry all seven: `backend/app/schemas/notification.py`
 * declares `NotificationPrefs` / `NotificationPrefsUpdate` field-by-field, and
 * Pydantic drops unknown keys, so an event missing *there* is accepted with a
 * 200 and silently discarded. `normalizePrefs` keeps that from crashing the
 * grid (see the last test below), but it cannot make the preference persist.
 */

const RAW = import.meta.glob('../../../../backend/app/models/notification.py', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const MODEL_PATH = 'backend/app/models/notification.py';
const source = Object.values(RAW)[0] ?? '';

/**
 * The event-type strings in `NOTIFICATION_EVENT_TYPES`, in declaration order.
 *
 * The tuple lists module constants (`EVENT_INVOICE_ASSIGNED`), not literals,
 * so each name is resolved against its own `NAME = "value"` assignment. An
 * unresolvable name is surfaced as `<unresolved:NAME>` rather than dropped —
 * a silently shorter list would let the roster comparison pass vacuously.
 */
function backendEventTypes(py: string): string[] {
	const tuple = /NOTIFICATION_EVENT_TYPES\s*=\s*\(([\s\S]*?)\)/.exec(py);
	if (!tuple) return [];

	const literals = new Map<string, string>();
	for (const m of py.matchAll(/^(EVENT_[A-Z0-9_]+)\s*=\s*["']([^"']+)["']/gm)) {
		literals.set(m[1], m[2]);
	}

	return tuple[1]
		.split(',')
		.map((entry) => entry.replace(/#.*$/gm, '').trim())
		.filter((entry) => entry.length > 0)
		.map((name) => literals.get(name) ?? `<unresolved:${name}>`);
}

const backendRoster = backendEventTypes(source);

describe('notification event roster', () => {
	it(`reads ${MODEL_PATH}`, () => {
		expect(Object.keys(RAW), 'the raw glob did not reach the backend model').toHaveLength(1);
		expect(source).toContain('NOTIFICATION_EVENT_TYPES');
	});

	it('parses a non-empty, fully-resolved backend roster', () => {
		// Guards the guard: a parser that silently returns [] would make every
		// comparison below pass against nothing.
		expect(backendRoster.length).toBeGreaterThan(3);
		expect(backendRoster.filter((e) => e.startsWith('<unresolved:'))).toEqual([]);
	});

	it('EVENT_ORDER matches the backend roster exactly, in declaration order', () => {
		expect(
			EVENT_ORDER,
			`EVENT_ORDER is out of step with ${MODEL_PATH}::NOTIFICATION_EVENT_TYPES. ` +
				'An event listed there but not here cannot be switched off from the UI ' +
				'(resolve_prefs defaults a missing key to ON).'
		).toEqual(backendRoster);
	});

	it('covers the three sweep-driven events that were previously un-mutable', () => {
		// Named explicitly so a regression that drops them fails with the reason
		// rather than as an anonymous array diff.
		for (const event of ['contract_renewal_due', 'chat_message', 'cash_shortfall_projected']) {
			expect(EVENT_ORDER, `${event} has no preference toggle`).toContain(event);
		}
	});

	it('every event has a label and an i18n key, and nothing extra', () => {
		expect(Object.keys(EVENT_LABELS).sort()).toEqual([...EVENT_ORDER].sort());
		expect(Object.keys(EVENT_LABEL_KEYS).sort()).toEqual([...EVENT_ORDER].sort());
	});

	it('the English catalogue value equals the English EVENT_LABELS spelling', () => {
		// Two English sources exist on purpose: the notification centre and bell
		// are not in the i18n extraction slice yet. Pinning them together is what
		// stops that from becoming two different names for one event.
		for (const event of EVENT_ORDER) {
			expect(en[EVENT_LABEL_KEYS[event]], `${event} label differs between EVENT_LABELS and en`).toBe(
				EVENT_LABELS[event]
			);
		}
	});

	it('no event label is empty', () => {
		for (const event of EVENT_ORDER) {
			expect(EVENT_LABELS[event].trim().length, `${event} has a blank label`).toBeGreaterThan(0);
		}
	});
});

describe('normalizePrefs', () => {
	it('fills every event with the server default when the map is empty', () => {
		const out = normalizePrefs({});
		expect(Object.keys(out).sort()).toEqual([...EVENT_ORDER].sort());
		for (const event of EVENT_ORDER) {
			expect(out[event]).toEqual({ email: true, in_app: true });
		}
	});

	it('survives a backend that answers with only the four invoice_* keys', () => {
		// The exact shape today's `schemas/notification.py::NotificationPrefs`
		// returns. Indexing the raw map for a newer event would hand the grid
		// `undefined` and throw on `.in_app`, taking the whole card down.
		const legacy = {
			invoice_assigned: { email: false, in_app: true },
			invoice_approved: { email: true, in_app: true },
			invoice_rejected: { email: true, in_app: true },
			invoice_paid: { email: true, in_app: false }
		} as Partial<Record<NotificationEventType, { email: boolean; in_app: boolean }>>;

		const out = normalizePrefs(legacy);
		// Supplied values are preserved verbatim…
		expect(out.invoice_assigned).toEqual({ email: false, in_app: true });
		expect(out.invoice_paid).toEqual({ email: true, in_app: false });
		// …and the missing ones default to on, matching resolve_prefs.
		expect(out.chat_message).toEqual({ email: true, in_app: true });
		expect(out.contract_renewal_due).toEqual({ email: true, in_app: true });
		expect(out.cash_shortfall_projected).toEqual({ email: true, in_app: true });
	});

	it('treats null/undefined as all-defaults rather than throwing', () => {
		expect(normalizePrefs(null).chat_message).toEqual({ email: true, in_app: true });
		expect(normalizePrefs(undefined).invoice_paid).toEqual({ email: true, in_app: true });
	});

	it('fills a half-populated channel pair', () => {
		const out = normalizePrefs({
			chat_message: { email: false } as unknown as { email: boolean; in_app: boolean }
		});
		expect(out.chat_message).toEqual({ email: false, in_app: true });
	});
});
