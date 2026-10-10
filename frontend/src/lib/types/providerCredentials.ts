/**
 * Tenant provider credentials (`/organization` → ERP, Payments, Virtual cards),
 * over `GET /api/organization/credentials` and
 * `PUT /api/organization/credentials/{block}`
 * (`backend/app/api/organization_credentials.py`).
 *
 * The secrets are **write-only**: they are stored encrypted server-side and no
 * endpoint returns them, so the status type carries the NAMES of the stored
 * paths and never a value. A save sends only what the admin typed; a blank
 * field keeps what is stored, and removing one is an explicit `clear`. Mirror
 * of `services/provider_credentials.SECRET_FIELDS` — keep the two in step.
 */

export type CredentialBlock = 'erp' | 'payments' | 'cards';

/** Which secret paths are stored, per block. Names only. */
export interface ProviderCredentialStatus {
	erp: string[];
	payments: string[];
	cards: string[];
}

export interface ProviderCredentialUpdate {
	set?: Record<string, string>;
	clear?: string[];
}

/** One secret field on the form: what the admin typed, and the remove toggle. */
export interface SecretFieldInput {
	value: string;
	clear: boolean;
}

/**
 * Build the PUT body from the form's secret fields, or `null` when the save
 * changes no secret (so no request is made). Blank values are left out —
 * "leave blank to keep" — and a typed value wins over a stale remove toggle.
 */
export function credentialUpdate(
	fields: Record<string, SecretFieldInput>
): ProviderCredentialUpdate | null {
	const set: Record<string, string> = {};
	const clear: string[] = [];
	for (const [path, { value, clear: remove }] of Object.entries(fields)) {
		const typed = value.trim();
		if (typed) set[path] = typed;
		else if (remove) clear.push(path);
	}
	if (Object.keys(set).length === 0 && clear.length === 0) return null;
	return {
		...(Object.keys(set).length ? { set } : {}),
		...(clear.length ? { clear } : {})
	};
}
