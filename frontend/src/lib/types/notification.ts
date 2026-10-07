import type { MessageKey } from '#lib/i18n/messages.ts';

/**
 * The notifiable event vocabulary.
 *
 * **The server owns this list.** The canonical roster is
 * `backend/app/models/notification.py::NOTIFICATION_EVENT_TYPES`; this union —
 * and `EVENT_ORDER` below — must carry the same strings *in the same order*.
 * `notification.roster.test.ts` reads that Python file as text and fails if the
 * two drift, because the failure mode is silent: `notification_dispatch
 * .resolve_prefs` defaults a *missing* preference key to **on**, so an event
 * the frontend never lists is an event the user cannot switch off. Three of
 * these (`contract_renewal_due`, `chat_message`, `cash_shortfall_projected`)
 * shipped server-side while this union still stopped at the four `invoice_*`
 * ones — `chat_message` emails the AP team on every supplier-portal message,
 * with no way to mute it from the UI.
 */
export type NotificationEventType =
	| 'invoice_assigned'
	| 'invoice_approved'
	| 'invoice_rejected'
	| 'invoice_paid'
	| 'contract_renewal_due'
	| 'chat_message'
	| 'cash_shortfall_projected'
	| 'ai_invoice_usage';

export interface Notification {
	id: string;
	event_type: NotificationEventType;
	entity_type: string;
	entity_id: string | null;
	title: string;
	body: string | null;
	read_at: string | null;
	created_at: string;
}

export interface NotificationListResponse {
	items: Notification[];
	total: number;
	unread: number;
	page: number;
	page_size: number;
}

export interface ChannelPrefs {
	email: boolean;
	in_app: boolean;
}

/** The complete preference map — one {email, in_app} pair per event type. */
export type NotificationPrefs = Record<NotificationEventType, ChannelPrefs>;

/**
 * The server's default for an unset preference: both channels on. Mirrors
 * `notification_dispatch._DEFAULT_CHANNELS` — opt-out, not opt-in.
 */
export const DEFAULT_CHANNEL_PREFS: ChannelPrefs = { email: true, in_app: true };

/**
 * Fill in any event the server omitted, with the server's own default.
 *
 * `GET /api/notifications/preferences` returns whatever
 * `schemas/notification.py::NotificationPrefs` declares — so a backend that has
 * not yet grown a field for a newer event type answers with a *partial* map.
 * Indexing that map for a row the grid renders would hand the template
 * `undefined` and throw on `.in_app`, taking the whole Notifications card down.
 * Normalising instead degrades to "shown as on, the same state the server will
 * actually apply", which is the truth rather than a crash.
 */
export function normalizePrefs(
	raw: Partial<NotificationPrefs> | null | undefined
): NotificationPrefs {
	const out = {} as NotificationPrefs;
	for (const event of EVENT_ORDER) {
		const channels = raw?.[event];
		out[event] = {
			email: channels?.email ?? DEFAULT_CHANNEL_PREFS.email,
			in_app: channels?.in_app ?? DEFAULT_CHANNEL_PREFS.in_app
		};
	}
	return out;
}

/**
 * Display labels for each event type — English, data-driven, the same
 * convention as `chatNotifications.ts`'s `CHAT_EVENT_LABELS`.
 *
 * Used by the notification centre and the bell, neither of which is in the
 * i18n extraction slice yet. The `/profile` preference grid renders
 * `EVENT_LABEL_KEYS` instead; the roster guard pins the English catalogue
 * values to this map so the two spellings can never diverge.
 */
export const EVENT_LABELS: Record<NotificationEventType, string> = {
	invoice_assigned: 'Invoice assigned to me',
	invoice_approved: 'Invoice approved',
	invoice_rejected: 'Invoice rejected',
	invoice_paid: 'Invoice paid',
	contract_renewal_due: 'Contract renewal due',
	chat_message: 'New supplier chat message',
	cash_shortfall_projected: 'Projected cash shortfall',
	ai_invoice_usage: 'AI-read invoice usage'
};

/**
 * The i18n key carrying each event's label, for surfaces that are inside the
 * extraction slice (the `/profile` preference grid). Split from `EVENT_LABELS`
 * rather than replacing it because the bell and centre are not extracted yet —
 * and kept honest by the roster guard, which asserts `en[key] === label`.
 */
export const EVENT_LABEL_KEYS: Record<NotificationEventType, MessageKey> = {
	invoice_assigned: 'profile.notifications.event.invoiceAssigned',
	invoice_approved: 'profile.notifications.event.invoiceApproved',
	invoice_rejected: 'profile.notifications.event.invoiceRejected',
	invoice_paid: 'profile.notifications.event.invoicePaid',
	contract_renewal_due: 'profile.notifications.event.contractRenewalDue',
	chat_message: 'profile.notifications.event.chatMessage',
	cash_shortfall_projected: 'profile.notifications.event.cashShortfallProjected',
	ai_invoice_usage: 'profile.notifications.event.aiInvoiceUsage'
};

/**
 * Render order for the preference grid. Deliberately the backend's own
 * declaration order in `NOTIFICATION_EVENT_TYPES` — invoice lifecycle first,
 * then the sweep-driven events in the order they were added — so there is one
 * ordering to reason about instead of a frontend opinion that silently drifts.
 */
export const EVENT_ORDER: NotificationEventType[] = [
	'invoice_assigned',
	'invoice_approved',
	'invoice_rejected',
	'invoice_paid',
	'contract_renewal_due',
	'chat_message',
	'cash_shortfall_projected',
	'ai_invoice_usage'
];
