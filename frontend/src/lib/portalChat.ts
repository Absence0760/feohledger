// Typed helpers for the supplier-portal chat endpoints (under
// /api/portal/invoices/…). Routes through the portal client (`portalApi`),
// which carries the VendorUser token. The portal surface is masked: no
// author_user_id, no mentions, no templates, no resolve/reopen.
import { portalApi } from '#lib/portalApi.ts';
import type { PortalChatMessage, PortalChatThread } from '#lib/types/supplierChat.ts';

export function getPortalChatThread(invoiceId: string): Promise<PortalChatThread> {
	return portalApi.get<PortalChatThread>(`/api/portal/invoices/${invoiceId}/chat`);
}

export function postPortalChatMessage(
	invoiceId: string,
	body: { body: string },
): Promise<PortalChatMessage> {
	return portalApi.post<PortalChatMessage>(`/api/portal/invoices/${invoiceId}/chat`, body);
}

export function uploadPortalChatAttachment(
	invoiceId: string,
	file: File,
	body?: string,
): Promise<PortalChatMessage> {
	return portalApi.upload<PortalChatMessage>(
		`/api/portal/invoices/${invoiceId}/chat/attachments`,
		file,
		{ body },
	);
}

// Portal attachments are download-to-save only this slice (`portalApi` has
// `download` but no object-URL/fetchBlob helper).
export function downloadPortalChatAttachment(invoiceId: string, fileKey: string): Promise<Blob> {
	return portalApi.download(`/api/portal/invoices/${invoiceId}/chat/file/${fileKey}`);
}
