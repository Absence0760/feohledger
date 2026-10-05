// Typed helpers for the admin outbound-webhook management surface. Routes
// through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never
// raw fetch. Backend: `backend/app/api/webhooks.py` (admin + JWT gated). The
// create response carries the plaintext `signing_secret` exactly once;
// everything else is metadata only (`secret_prefix`).
import { api } from '#lib/api.ts';
import type {
	WebhookDelivery,
	WebhookSecretRotated,
	WebhookSubscription,
	WebhookSubscriptionCreated
} from '#lib/types/webhooks.ts';

/** This org's webhook subscriptions, newest first. Metadata only. */
export function listWebhookSubscriptions(): Promise<WebhookSubscription[]> {
	return api.get<WebhookSubscription[]>('/api/webhooks');
}

/** Create a subscription. The response is the ONLY place the full signing
 *  secret is returned — surface it once, then drop it. */
export function createWebhookSubscription(body: {
	name: string;
	target_url: string;
	event_types: string[];
}): Promise<WebhookSubscriptionCreated> {
	return api.post<WebhookSubscriptionCreated>('/api/webhooks', body);
}

/** Patch a subscription (name / URL / events / active). Returns updated
 *  metadata, but callers re-list afterwards. */
export function updateWebhookSubscription(
	id: string,
	body: {
		name?: string;
		target_url?: string;
		event_types?: string[];
		active?: boolean;
	}
): Promise<WebhookSubscription> {
	return api.patch<WebhookSubscription>(`/api/webhooks/${id}`, body);
}

/**
 * Mint a replacement signing secret for a subscription, keeping its id — and
 * therefore its whole delivery history, which `deleteWebhookSubscription`
 * CASCADEs away. This is the remedy for a leaked secret; deleting and
 * re-creating destroys the record of what was delivered.
 *
 * The response is the ONLY place the replacement secret is returned — surface
 * it once, then drop it, exactly like the create flow.
 *
 * `overlapMinutes` keeps the retiring secret signing a second
 * `X-Webhook-Signature-Previous` header for that long, so a receiver accepting
 * either header rotates with no dropped deliveries; `0` is a hard cutover for a
 * known-compromised secret. Out-of-range values are refused by the backend
 * (422), not clamped — see `#lib/utils/webhookRotation`.
 */
export function rotateWebhookSecret(
	id: string,
	overlapMinutes: number
): Promise<WebhookSecretRotated> {
	return api.post<WebhookSecretRotated>(`/api/webhooks/${id}/rotate-secret`, {
		overlap_minutes: overlapMinutes
	});
}

/** Delete a subscription (CASCADEs its deliveries). */
export function deleteWebhookSubscription(id: string): Promise<void> {
	return api.delete(`/api/webhooks/${id}`);
}

/** This org's webhook deliveries (newest first), optionally filtered by status
 *  and/or subscription, paginated. */
export function listWebhookDeliveries(params: {
	status?: string;
	subscriptionId?: string;
	page?: number;
	pageSize?: number;
} = {}): Promise<WebhookDelivery[]> {
	const q = new URLSearchParams();
	if (params.status) q.set('status', params.status);
	if (params.subscriptionId) q.set('subscription_id', params.subscriptionId);
	if (params.page) q.set('page', String(params.page));
	if (params.pageSize) q.set('page_size', String(params.pageSize));
	const qs = q.toString();
	return api.get<WebhookDelivery[]>(`/api/webhooks/deliveries${qs ? `?${qs}` : ''}`);
}

/** Re-enqueue + immediately re-attempt a failed / dead delivery. The backend
 *  409s an already-`delivered` delivery (re-sending would double-fire a side
 *  effect at the receiver) — the caller surfaces that message. */
export function redeliverWebhookDelivery(id: string): Promise<WebhookDelivery> {
	return api.post<WebhookDelivery>(`/api/webhooks/deliveries/${id}/redeliver`, {});
}
