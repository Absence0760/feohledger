// Types for the admin outbound-webhook management surface (`/admin/webhooks`).
// Mirrors the backend `SubscriptionResponse` / `SubscriptionCreatedResponse` /
// `DeliveryResponse` shapes in `backend/app/api/webhooks.py`. The full HMAC
// `signing_secret` is ONLY ever present on the create response and is shown
// exactly once — it is never stored, echoed, or re-fetchable (list/get carry
// only `secret_prefix`).

/** A subscribable platform event type (mirrors `WEBHOOK_EVENT_TYPES`). */
export type WebhookEventType =
	| 'invoice.approved'
	| 'invoice.approval_recalled'
	| 'payment.settled'
	| 'exception.raised';

/** All event types the create/edit forms offer, in display order (the
 *  backend's order — `backend/tests/test_webhook_event_catalogue.py` reads
 *  this list and fails when the two differ). */
export const WEBHOOK_EVENT_TYPES: WebhookEventType[] = [
	'invoice.approved',
	'invoice.approval_recalled',
	'payment.settled',
	'exception.raised'
];

export interface WebhookSubscription {
	id: string;
	name: string;
	target_url: string;
	event_types: string[];
	secret_prefix: string;
	active: boolean;
	created_at: string | null;
	updated_at: string | null;
	/** When a rotation's overlap window closes — i.e. when the retiring secret
	 *  stops signing `X-Webhook-Signature-Previous`. `null` when no rotation is
	 *  in flight. The EXPIRY only: the retiring secret itself is never returned
	 *  on a list/get read, same as the current one. */
	previous_secret_expires_at: string | null;
}

/** The create response — one of only two places the full signing secret is
 *  returned (the other is a rotation, below). */
export interface WebhookSubscriptionCreated {
	subscription: WebhookSubscription;
	// Shown once; copy it now. Never persisted client-side after the modal closes.
	signing_secret: string;
}

/**
 * The rotate response (`POST /api/webhooks/{id}/rotate-secret`) — the
 * replacement secret, returned exactly once, on the SAME subscription id (and
 * therefore keeping its whole delivery history, unlike delete-and-recreate).
 */
export interface WebhookSecretRotated {
	subscription: WebhookSubscription;
	// Shown once; copy it now. Never persisted client-side after the modal closes.
	signing_secret: string;
	/** When the retiring secret stops signing `X-Webhook-Signature-Previous`.
	 *  `null` on a hard cutover (`overlap_minutes: 0`) — it already has. */
	previous_secret_expires_at: string | null;
}

/** Delivery lifecycle states (mirrors `DELIVERY_*` constants). */
export type WebhookDeliveryStatus = 'pending' | 'delivered' | 'failed' | 'dead';

export interface WebhookDelivery {
	id: string;
	subscription_id: string;
	event_id: string;
	event_type: string;
	status: string;
	attempt_count: number;
	response_code: number | null;
	next_attempt_at: string | null;
	last_attempt_at: string | null;
	created_at: string | null;
}
