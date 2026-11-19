import type { Notification } from '#lib/types/notification.ts';

/**
 * Where a notification opens — the one mapping the sidebar bell and
 * `/notifications` share, so the two surfaces cannot disagree about which rows
 * are links.
 *
 * Keyed by the row's `entity_type`, the vocabulary the backend writes through
 * `services/notification_dispatch.notify_event(entity_type=…)`.
 * `notificationHref.test.ts` scans every `notify_event` call under
 * `backend/app/services/` and fails if one names an entity type missing here,
 * so a new kind of notification is a deliberate decision about its
 * destination rather than a row that quietly marks itself read and goes
 * nowhere (which is how every non-invoice notification behaved before).
 *
 * A resolver returns `null` when the row has nowhere to go (a record-scoped
 * type whose `entity_id` is missing). The surfaces render such a row as plain
 * text, never as a control that looks clickable.
 */
type Resolver = (n: Pick<Notification, 'event_type' | 'entity_id'>) => string | null;

const withId = (path: string): Resolver => (n) =>
	n.entity_id ? `${path}?id=${encodeURIComponent(n.entity_id)}` : null;

export const NOTIFICATION_ROUTES: Record<string, Resolver> = {
	// The four invoice_* events and chat_message — `/invoices?id=` opens the
	// invoice's detail drawer.
	invoice: withId('/invoices'),
	// contract_renewal_due — `/contracts?id=` opens the contract's detail modal.
	contract: withId('/contracts'),
	// cash_shortfall_projected — about the org's whole projected position
	// (entity_id is always NULL), so it opens the forecast itself.
	cash_position: () => '/cash-flow',
	// ai_invoice_usage — the plan's AI allowance / spending cap, shown on the
	// billing page's usage meters.
	billing: () => '/billing',
	// erp_connection_expiring / erp_call_quota — both about the org's ERP
	// connection, which lives in the ERP section of the settings screen. Any
	// other event filed under this generic type has no known home yet.
	organization: (n) =>
		n.event_type === 'erp_connection_expiring' || n.event_type === 'erp_call_quota'
			? '/organization?section=erp'
			: null
};

/** The in-app route a notification opens, or `null` when it navigates nowhere. */
export function notificationHref(
	n: Pick<Notification, 'entity_type' | 'event_type' | 'entity_id'>
): string | null {
	const resolve = Object.hasOwn(NOTIFICATION_ROUTES, n.entity_type)
		? NOTIFICATION_ROUTES[n.entity_type]
		: undefined;
	return resolve ? resolve(n) : null;
}
