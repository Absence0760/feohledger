import { describe, expect, it } from 'vitest';
import { EVENT_ORDER, type NotificationEventType } from '#lib/types/notification.ts';
import { NOTIFICATION_ROUTES, notificationHref } from './notificationHref';

/**
 * Roster guard for the notification → route map.
 *
 * The server owns the `entity_type` vocabulary: it is whatever each
 * `notify_event(...)` call under `backend/app/services/` passes (or the
 * dispatcher's own default when a call passes none). An entity type the
 * frontend does not map is a notification that marks itself read and opens
 * nothing — the defect this map was written to close — so the guard reads the
 * real Python source, the same way `types/notification.roster.test.ts` pins
 * the event roster, and fails on a type with no decision recorded for it.
 */

const RAW = import.meta.glob('../../../../backend/app/services/**/*.py', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** The argument list of every call to `fn(` in `src`, by paren matching. */
function callArgs(src: string, fn: string): string[] {
	const out: string[] = [];
	const re = new RegExp(`(?<!def )\\b${fn}\\(`, 'g');
	for (const m of src.matchAll(re)) {
		let depth = 1;
		let i = (m.index ?? 0) + m[0].length;
		const start = i;
		while (i < src.length && depth > 0) {
			if (src[i] === '(') depth++;
			else if (src[i] === ')') depth--;
			i++;
		}
		out.push(src.slice(start, i - 1));
	}
	return out;
}

const dispatch = Object.entries(RAW).find(([p]) => p.endsWith('/notification_dispatch.py'))?.[1] ?? '';
const defaultEntityType =
	/async def notify_event\([\s\S]*?entity_type:\s*str\s*=\s*["']([^"']+)["']/.exec(dispatch)?.[1] ??
	'<unresolved-default>';

/**
 * Every entity type a notify_event call can write. A call whose
 * `entity_type=` is not a string literal is surfaced as `<dynamic:file>`
 * rather than skipped, so a refactor cannot make the guard pass vacuously.
 */
function backendEntityTypes(): { types: Set<string>; calls: number } {
	const types = new Set<string>();
	let calls = 0;
	for (const [path, src] of Object.entries(RAW)) {
		if (path.endsWith('/notification_dispatch.py')) continue;
		for (const args of callArgs(src, 'notify_event')) {
			calls++;
			const kw = /\bentity_type\s*=\s*([^,\n)]+)/.exec(args);
			if (!kw) {
				types.add(defaultEntityType);
				continue;
			}
			const lit = /^["']([^"']+)["']$/.exec(kw[1].trim());
			types.add(lit ? lit[1] : `<dynamic:${path.split('/').pop()}>`);
		}
	}
	return { types, calls };
}

const backend = backendEntityTypes();

describe('notification route roster', () => {
	it('reads the backend services and finds the notify_event calls', () => {
		expect(dispatch, 'notification_dispatch.py was not reached').toContain('def notify_event');
		expect(defaultEntityType).toBe('invoice');
		// Guards the guard: a glob or parser that finds nothing would make the
		// comparison below pass against an empty set.
		expect(backend.calls).toBeGreaterThan(5);
	});

	it('maps exactly the entity types the backend writes', () => {
		expect(
			[...backend.types].sort(),
			'A notify_event call writes an entity_type with no route decision in ' +
				'NOTIFICATION_ROUTES (lib/utils/notificationHref.ts) — add one, even if ' +
				'it resolves to null.'
		).toEqual(Object.keys(NOTIFICATION_ROUTES).sort());
	});
});

const ID = '3f2b8e9c-1a4d-4c5e-9f00-0000000000aa';

describe('notificationHref', () => {
	it.each(['invoice_assigned', 'invoice_approved', 'invoice_rejected', 'invoice_paid', 'chat_message'])(
		'%s opens the invoice drawer',
		(event) => {
			expect(
				notificationHref({ entity_type: 'invoice', event_type: event as NotificationEventType, entity_id: ID })
			).toBe(`/invoices?id=${ID}`);
		}
	);

	it('contract_renewal_due opens the contract', () => {
		expect(
			notificationHref({ entity_type: 'contract', event_type: 'contract_renewal_due', entity_id: ID })
		).toBe(`/contracts?id=${ID}`);
	});

	it('a record-scoped row with no entity_id navigates nowhere', () => {
		expect(notificationHref({ entity_type: 'invoice', event_type: 'invoice_paid', entity_id: null })).toBeNull();
		expect(
			notificationHref({ entity_type: 'contract', event_type: 'contract_renewal_due', entity_id: null })
		).toBeNull();
	});

	it('the org-wide events open their screens without an id', () => {
		expect(
			notificationHref({ entity_type: 'cash_position', event_type: 'cash_shortfall_projected', entity_id: null })
		).toBe('/cash-flow');
		expect(notificationHref({ entity_type: 'billing', event_type: 'ai_invoice_usage', entity_id: null })).toBe(
			'/billing'
		);
		for (const event of ['erp_connection_expiring', 'erp_call_quota'] as const) {
			expect(notificationHref({ entity_type: 'organization', event_type: event, entity_id: null })).toBe(
				'/organization?section=erp'
			);
		}
	});

	it('every event in the roster resolves somewhere when it carries an id', () => {
		// The entity type each event is written with (backend/app/models/notification.py).
		const ENTITY: Record<NotificationEventType, string> = {
			invoice_assigned: 'invoice',
			invoice_approved: 'invoice',
			invoice_rejected: 'invoice',
			invoice_paid: 'invoice',
			contract_renewal_due: 'contract',
			chat_message: 'invoice',
			cash_shortfall_projected: 'cash_position',
			ai_invoice_usage: 'billing',
			erp_connection_expiring: 'organization',
			erp_call_quota: 'organization'
		};
		expect(Object.keys(ENTITY).sort()).toEqual([...EVENT_ORDER].sort());
		for (const event of EVENT_ORDER) {
			expect(
				notificationHref({ entity_type: ENTITY[event], event_type: event, entity_id: ID }),
				`${event} navigates nowhere`
			).not.toBeNull();
		}
	});

	it('an unknown entity type, or one only on Object.prototype, navigates nowhere', () => {
		expect(notificationHref({ entity_type: 'something_new', event_type: 'invoice_paid', entity_id: ID })).toBeNull();
		expect(notificationHref({ entity_type: 'toString', event_type: 'invoice_paid', entity_id: ID })).toBeNull();
		expect(
			notificationHref({ entity_type: 'organization', event_type: 'invoice_paid', entity_id: null })
		).toBeNull();
	});

	it('encodes the id into the query string', () => {
		expect(notificationHref({ entity_type: 'invoice', event_type: 'invoice_paid', entity_id: 'a&b' })).toBe(
			'/invoices?id=a%26b'
		);
	});
});
