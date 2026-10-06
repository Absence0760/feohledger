/**
 * Tenant SSO configuration (admin only).
 *
 * The client secret is write-only end to end: it goes out on
 * {@link updateSsoSettings} and never comes back on any read. Don't add a
 * helper that tries to fetch it — the backend has no endpoint that serves it.
 */
import { api } from '#lib/api.ts';
import type { SsoSettingsStatus, SsoSettingsUpdate } from '#lib/types/ssoSettings.ts';

const BASE = '/api/organization/sso';

export const getSsoSettings = () => api.get<SsoSettingsStatus>(BASE);

export const updateSsoSettings = (body: SsoSettingsUpdate) =>
	api.put<SsoSettingsStatus>(BASE, body);
