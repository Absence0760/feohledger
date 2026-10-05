/**
 * Chat-notification settings + webhook-credential rotation (admin only).
 *
 * The webhook URL is write-only end to end: it goes out on
 * {@link rotateChatWebhook} and never comes back on any read. Don't add a
 * helper that tries to fetch it — the backend has no endpoint that serves it.
 * See `backend/docs/notifications.md` § Rotating the webhook URL.
 */
import { api } from '#lib/api.ts';
import type {
	ChatNotificationSettingsUpdate,
	ChatNotificationStatus
} from '#lib/types/chatNotifications.ts';

const BASE = '/api/organization/chat-notifications';

export const getChatNotifications = () => api.get<ChatNotificationStatus>(BASE);

export const updateChatNotifications = (body: ChatNotificationSettingsUpdate) =>
	api.put<ChatNotificationStatus>(BASE, body);

/** Set or replace the incoming-webhook URL. Atomic — there is no overlap window. */
export const rotateChatWebhook = (webhookUrl: string) =>
	api.put<ChatNotificationStatus>(`${BASE}/webhook`, { webhook_url: webhookUrl });

/**
 * Revoke the incoming-webhook URL — the fastest containment for a leak when
 * there's no replacement to paste yet. Idempotent.
 *
 * The endpoint answers with the refreshed status, but the shared client types
 * `api.delete` as `void` (most DELETEs carry no body), so re-read it rather
 * than casting the client's return type.
 */
export async function revokeChatWebhook(): Promise<ChatNotificationStatus> {
	await api.delete(`${BASE}/webhook`);
	return getChatNotifications();
}
