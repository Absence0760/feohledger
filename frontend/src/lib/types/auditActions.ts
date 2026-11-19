// Human labels for `AuditLog.action` tokens on the /audit console.
//
// The backend has no single action registry: tokens are written at ~300
// `dispatch_audit` / `log_action` call sites, plus a handful built from
// f-strings. So the vocabulary lives here as catalogue keys
// (`audit.action.<token>`), and `auditActions.roster.test.ts` scans the
// backend source so a new token cannot ship without a label. The labels are
// the `audit` catalogue slice (catalogues.ts), which /audit's `+layout.ts`
// loads before the page renders, so `hasMessage` sees them on the first frame.
//
// The label never replaces the token. Auditors cite `invoice.approved`
// verbatim, the CSV export carries it unchanged, and an unknown token (a
// retired action on an old row, or one added after this build) renders raw.

import { hasMessage } from '#lib/i18n/store.svelte.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** The backend f-string templates, each with the rule that labels it. The
 *  roster test fails on a template that is not listed here. */
export const DYNAMIC_ACTION_TEMPLATES = [
	'billing.subscription_{event.status}',
	'commerce_inventory.{row.status}',
	'contract.{target}',
	'expense_preapproval.{new_status}',
	'invoice.erp_status_{target_status.value}',
	'payment.{payment.status}',
	'vendor.{change_type}_change_requested',
	'vendor.{req.change_type}_change_approved',
	'vendor.{req.change_type}_change_rejected',
	'{entity_type}.viewed'
] as const;

// The variable tail of a templated token is a status / change-type enum value
// with no label of its own here; it is shown with underscores as spaces.
const PATTERNS: { re: RegExp; key: MessageKey; param: 'status' | 'change' | null }[] = [
	{ re: /^billing\.subscription_([a-z_]+)$/, key: 'audit.actionPattern.subscriptionStatus', param: 'status' },
	{ re: /^commerce_inventory\.([a-z_]+)$/, key: 'audit.actionPattern.inventorySync', param: 'status' },
	{ re: /^contract\.([a-z_]+)$/, key: 'audit.actionPattern.contractStatus', param: 'status' },
	{ re: /^expense_preapproval\.([a-z_]+)$/, key: 'audit.actionPattern.preapprovalStatus', param: 'status' },
	{ re: /^invoice\.erp_status_([a-z_]+)$/, key: 'audit.actionPattern.erpStatus', param: 'status' },
	{ re: /^payment\.([a-z_]+)$/, key: 'audit.actionPattern.paymentStatus', param: 'status' },
	{ re: /^vendor\.([a-z_]+)_change_requested$/, key: 'audit.actionPattern.vendorChangeRequested', param: 'change' },
	{ re: /^vendor\.([a-z_]+)_change_approved$/, key: 'audit.actionPattern.vendorChangeApproved', param: 'change' },
	{ re: /^vendor\.([a-z_]+)_change_rejected$/, key: 'audit.actionPattern.vendorChangeRejected', param: 'change' },
	{ re: /^[a-z_]+\.viewed$/, key: 'audit.actionPattern.viewed', param: null }
];

/** The catalogue key for an exact token, or null when it has none. `has`
 *  defaults to the runtime's view of the loaded catalogues. */
export function auditActionKey(action: string, has: (key: string) => boolean = hasMessage): MessageKey | null {
	const key = `audit.action.${action}`;
	return has(key) ? (key as MessageKey) : null;
}

/** A translated label for `action`, or the token itself when none applies. */
export function auditActionLabel(action: string, t: Translate, has: (key: string) => boolean = hasMessage): string {
	const exact = auditActionKey(action, has);
	if (exact) return t(exact);
	for (const { re, key, param } of PATTERNS) {
		const match = re.exec(action);
		if (!match) continue;
		return param ? t(key, { [param]: match[1].replaceAll('_', ' ') }) : t(key);
	}
	return action;
}
