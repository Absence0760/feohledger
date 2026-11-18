// Types for the partner / reseller multi-tenant admin surface.
// Backend: `backend/app/api/partner.py`. See `docs/white-label.md` §
// Partner / reseller admin.

/** A single child tenant administered by the caller's partner org. */
export interface ChildTenant {
	id: string;
	name: string;
	slug: string;
	plan: string;
	/** The child's resolved white-label product name ("" = platform default). */
	product_name: string;
}

/** The partner overview — the caller's own identity + its child tenants.
 *  `is_partner` is derived (true iff it administers >= 1 child). */
export interface PartnerOverview {
	organization_id: string;
	name: string;
	is_partner: boolean;
	children: ChildTenant[];
}

/** A single-use link code an org's admin mints so a partner can attach it.
 *  Two-sided consent: handing this to a partner IS the act of consenting. */
export interface LinkCode {
	link_code: string;
	expires_in_minutes: number;
}

/** Inputs to provision a brand-NEW child tenant under the partner (mirrors the
 *  `create_tenant.py` CLI inputs). The admin password is platform-generated. */
export interface ProvisionChildRequest {
	name: string;
	slug: string;
	admin_email: string;
}

/** The newly provisioned child. No credential: its admin is emailed a
 *  single-use set-password link (decisions §283). `set_password_email_sent` is
 *  false when that email could not be sent after the tenant already existed —
 *  the admin then uses Forgot password on the workspace sign-in page. */
export interface ProvisionedChild extends ChildTenant {
	admin_email: string;
	set_password_email_sent: boolean;
	set_password_link_expires_at: string | null;
}

/** A child tenant's white-label branding (same shape as the org's own brand). */
export interface ChildBranding {
	product_name: string;
	logo_url: string;
	accent_color: string;
	accent_strong_color: string;
	support_url: string;
	legal_url: string;
}
