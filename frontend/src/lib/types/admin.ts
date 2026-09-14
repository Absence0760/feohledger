import type { MessageKey } from '$lib/i18n/messages';

export interface Role {
	id: string;
	name: string;
	description: string | null;
	is_system: boolean;
	// Effective granular permissions the role confers (catalog strings). For a
	// system role this is its static default set; for a custom role it's the
	// stored list. Optional for back-compat with older responses.
	permissions?: string[];
}

/** One row of the backend granular-permission catalog (GET /api/admin/permissions). */
export interface PermissionCatalogEntry {
	key: string;
	label: string;
}

// Permission keys — mirror `backend/app/api/permissions.py::ALL_PERMISSIONS`.
// Referenced by `auth.can(PERM_*)` at the gated controls so a typo is caught.
export const PERM_INVOICE_APPROVE = 'invoice.approve';
export const PERM_PAYMENT_RUN_APPROVE = 'payment_run.approve';
export const PERM_PAYMENT_EXECUTE = 'payment.execute';
export const PERM_PAYMENT_VOID = 'payment.void';
export const PERM_VENDOR_BANK_CHANGE_APPROVE = 'vendor.bank_change.approve';
export const PERM_VENDOR_BLOCK = 'vendor.block';
export const PERM_VENDOR_MANAGE = 'vendor.manage';
export const PERM_USER_MANAGE = 'user.manage';

export interface AdminUser {
	id: string;
	email: string;
	full_name: string;
	is_active: boolean;
	roles: Role[];
	created_at: string;
}

/**
 * The four built-in roles, in the backend's own declaration order
 * (`api/deps.py::ALL_ROLES`). `Role.name` is a plain string on the wire — a
 * tenant's own custom roles come back through the same field — so this tuple
 * is the set that has a translatable name, not the set of names that exist.
 */
export const SYSTEM_ROLES = ['admin', 'ap_manager', 'ap_clerk', 'cfo'] as const;

export type SystemRole = (typeof SYSTEM_ROLES)[number];

/**
 * The i18n key carrying each system role's label — never the English string
 * itself.
 *
 * This was `ROLE_LABELS`, a hardcoded English map, and it was the last one
 * left: `/admin`'s user rows read it while `/profile`'s Account card printed
 * the raw slugs (`admin, ap_manager`) and `/admin`'s own system-roles table
 * printed them too — three surfaces, two vocabularies, one role.
 *
 * The English values are byte-identical to what `ROLE_LABELS` held, because
 * `admin/users.spec.ts` selects the "AP Manager" checkbox by its label text.
 */
export const ROLE_LABEL_KEYS: Record<SystemRole, MessageKey> = {
	admin: 'admin.roles.name.admin',
	ap_manager: 'admin.roles.name.apManager',
	ap_clerk: 'admin.roles.name.apClerk',
	cfo: 'admin.roles.name.cfo'
};

/**
 * The message key for a role name, or `null` for one this build has no wording
 * for — which is every **custom** role, by design.
 *
 * A custom role's name is text an admin typed into their own tenant. It is
 * data, not copy: translating it would rename someone's `Approver` role
 * according to the locale each viewer happens to be in. So the caller falls
 * back to the stored name, verbatim.
 *
 * That fallback can never collide with a real system role, and the guarantee is
 * the backend's rather than a convention here: `POST /api/admin/roles` 400s any
 * name in `ALL_ROLES` as reserved (`api/admin.py`), so no tenant can own a role
 * called `cfo` and have this map speak for it.
 */
export function roleLabelKey(role: string): MessageKey | null {
	return ROLE_LABEL_KEYS[role as SystemRole] ?? null;
}
