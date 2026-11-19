/**
 * Per-tenant outbound chat-notification config (`/organization` → Chat
 * Notifications), over `GET/PUT /api/organization/chat-notifications`.
 *
 * The incoming-webhook URL is **write-only**: it is the credential for both
 * real providers (whoever holds a Slack / Teams webhook URL can post arbitrary
 * content into the customer's approval channel), so no endpoint returns it.
 * That is why this type has no `webhook_url` field and never will — only
 * `webhook_configured` plus the bare `webhook_host`. See
 * `backend/docs/notifications.md` § Rotating the webhook URL.
 */
export interface ChatNotificationStatus {
	enabled: boolean;
	/** `null` on an org that has never configured chat. */
	provider: string | null;
	/** Per-event opt-out map; a missing key means "on" once enabled. */
	events: Record<string, boolean>;
	webhook_configured: boolean;
	/** Bare hostname (e.g. `hooks.slack.com`) — never the token-bearing path. */
	webhook_host: string | null;
	/** Registry-derived server-side, so the picker can't offer a dead provider. */
	supported_providers: string[];
	supported_events: string[];
}

export interface ChatNotificationSettingsUpdate {
	enabled: boolean;
	provider: string;
	events: Record<string, boolean>;
}

/**
 * Display labels for the chat-notifiable events. Data-driven English,
 * deliberately NOT `notification.ts`'s `EVENT_LABEL_KEYS` wording, because a chat post goes to a shared channel, so "Invoice assigned to me" is
 * the wrong sentence here.
 *
 * The server owns the vocabulary (`supported_events`); an unmapped key renders
 * as itself rather than vanishing, so a newly-added event stays togglable.
 */
export const CHAT_EVENT_LABELS: Record<string, string> = {
	invoice_assigned: 'Invoice assigned for review',
	invoice_approved: 'Invoice approved',
	invoice_rejected: 'Invoice rejected',
	invoice_paid: 'Invoice paid'
};

/** Display labels for the chat providers. Product names — English by convention. */
export const CHAT_PROVIDER_LABELS: Record<string, string> = {
	mock: 'None (local mock — no messages sent)',
	slack: 'Slack',
	teams: 'Microsoft Teams'
};
