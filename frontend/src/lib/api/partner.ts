// Typed helpers for the partner / reseller multi-tenant admin surface. Routes
// through the shared `api` client (Bearer + X-Tenant-Slug + 401-bounce) — never
// raw fetch. Backend: `backend/app/api/partner.py` (admin + JWT gated, scoped to
// the caller's own child tenants). See `docs/white-label.md` § Partner admin.
import { api } from '#lib/api.ts';
import type {
	ChildBranding,
	ChildTenant,
	LinkCode,
	PartnerOverview,
	ProvisionChildRequest,
	ProvisionedChild
} from '#lib/types/partner.ts';

/** The caller's partner overview — its identity + the child tenants it administers. */
export function getPartnerOverview(): Promise<PartnerOverview> {
	return api.get<PartnerOverview>('/api/partner');
}

/** One child tenant's white-label branding. */
export function getChildBranding(childId: string): Promise<ChildBranding> {
	return api.get<ChildBranding>(`/api/partner/children/${childId}/branding`);
}

/** Push branding onto a child tenant (full replace; preserves the child's
 *  custom-domain list server-side). Admin-only; audited into the child's trail. */
export function updateChildBranding(
	childId: string,
	branding: ChildBranding
): Promise<ChildBranding> {
	return api.put<ChildBranding>(`/api/partner/children/${childId}/branding`, branding);
}

/** Mint a single-use link code FOR the caller's own org, so it can be attached
 *  as a child by a partner. Handing the code to a partner is the consent step —
 *  the partner can do nothing with it until then. Admin-only; 503 if the feature
 *  is not configured (no signing key). */
export function mintLinkCode(): Promise<LinkCode> {
	return api.post<LinkCode>('/api/partner/link-code', {});
}

/** Attach a consenting child to the caller's partner org by redeeming a code the
 *  child's admin minted. The signature is the consent proof — a partner can't
 *  adopt an org that didn't issue a code. Admin-only; 400 on a bad/expired code,
 *  409 on a replay or a child already linked elsewhere. */
export function attachChild(linkCode: string): Promise<ChildTenant> {
	return api.post<ChildTenant>('/api/partner/children', { link_code: linkCode });
}

/** Provision a brand-NEW child tenant already parented to the caller's partner
 *  org (the new-tenant counterpart of `attachChild`, which adopts an existing
 *  one). Admin-only; the new tenant is ALWAYS parented to the caller — no parent
 *  id is sent. Returns the child + a one-time temp password for the new admin.
 *  422 on an invalid slug/email, 409 on a slug already taken. */
export function provisionChild(body: ProvisionChildRequest): Promise<ProvisionedChild> {
	return api.post<ProvisionedChild>('/api/partner/children/provision', body);
}

/** Detach a child tenant from the caller's partner org (back to standalone).
 *  Admin-only; scoped to the caller's own children (opaque 404 otherwise). */
export function detachChild(childId: string): Promise<void> {
	return api.delete(`/api/partner/children/${childId}`);
}
