// Typed helpers for the AP-side supplier-chat endpoints (under /api/invoices/…).
// All requests route through the shared `api` client (Bearer + X-Tenant-Slug +
// 401-bounce), mirroring #lib/api/audit.ts.
import { api } from '#lib/api.ts';
import type { ChatMessage, ChatThread, ChatTemplate } from '#lib/types/supplierChat.ts';

export function getChatThread(invoiceId: string): Promise<ChatThread> {
	return api.get<ChatThread>(`/api/invoices/${invoiceId}/chat`);
}

export function postChatMessage(
	invoiceId: string,
	body: { body: string; mention_user_ids?: string[]; template_key?: string },
): Promise<ChatMessage> {
	return api.post<ChatMessage>(`/api/invoices/${invoiceId}/chat`, body);
}

export function uploadChatAttachment(
	invoiceId: string,
	file: File,
	body?: string,
	mentionUserIds?: string[],
): Promise<ChatMessage> {
	return api.upload<ChatMessage>(`/api/invoices/${invoiceId}/chat/attachments`, file, {
		body,
		mention_user_ids: mentionUserIds,
	});
}

export function resolveChatThread(invoiceId: string): Promise<ChatThread> {
	return api.post<ChatThread>(`/api/invoices/${invoiceId}/chat/resolve`, {});
}

export function reopenChatThread(invoiceId: string): Promise<ChatThread> {
	return api.post<ChatThread>(`/api/invoices/${invoiceId}/chat/reopen`, {});
}

export function getChatTemplates(): Promise<ChatTemplate[]> {
	return api.get<ChatTemplate[]>('/api/invoices/chat/templates');
}

/**
 * A colleague the @mention picker may offer.
 *
 * Deliberately NOT `AdminUser`: this is what the server sends, and what the
 * server sends is a display name and nothing else. The picker used to be handed
 * `AdminUser` rows and rendered each candidate's EMAIL under their name — a
 * directory of every colleague's address, rebuilt inside a chat composer. The
 * type is the thing that keeps that from coming back.
 */
export interface ChatMentionCandidate {
	id: string;
	full_name: string;
	is_active: boolean;
}

/**
 * Who this invoice's chat can @mention.
 *
 * The picker previously read `adminStore.users`, which only `/admin` and
 * `/workflows/[id]` ever load — so on `/invoices`, where the modal actually
 * lives, it was permanently empty unless the user had visited one of those
 * first. `GET /api/admin/users` was not the fix (admin-only, and full of PII);
 * this endpoint is gated exactly like posting a mention, which is every
 * authenticated employee.
 */
export function getChatMentionableUsers(): Promise<ChatMentionCandidate[]> {
	return api.get<ChatMentionCandidate[]>('/api/invoices/chat/mentionable-users');
}

// Path for an attachment's bytes — feed to `api.fetchBlob` for inline render.
export function chatAttachmentUrl(invoiceId: string, fileKey: string): string {
	return `/api/invoices/${invoiceId}/chat/file/${fileKey}`;
}
