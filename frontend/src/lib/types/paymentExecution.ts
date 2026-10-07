// The no-rail pilot (issue #517, `docs/decisions.md` §251): whether FeohLedger
// SENDS this tenant's payments or only RECORDS payments the customer made from
// its own bank or ERP. Pure — no `$app`, no store, no `api` — so vitest reaches
// it and `tests-e2e/` may value-import it.
//
// The backend owns the decision (`services/payment_execution_mode`) and every
// dispatching endpoint enforces it; the UI only mirrors it so a record-only
// tenant is never offered a control that can only 409.

import type { MessageKey } from '#lib/i18n/messages.ts';
import type { PaymentMethod } from '#lib/types/payment.ts';

/** `GET /api/payments/execution-mode`. */
export type PaymentExecutionMode = 'processor' | 'record_only';

export interface ExecutionModeResponse {
	mode: PaymentExecutionMode;
	/** Why it resolved so: `configured` | `unknown_mode` |
	 *  `no_processor_in_deployed_environment`. Fixed, PII-free vocabulary. */
	reason: string;
}

/**
 * Whether the page may offer the controls that SEND money (Execute, compliance
 * Release). **Fail closed**: an unknown mode — still loading, the read failed,
 * or a value this build predates — is treated as record-only, so the worst a
 * slow or failed read costs is a hidden button, never a send control on a
 * tenant whose server would refuse it. The record-outside path is offered in
 * both modes, so the page is never left without a way to `paid`.
 */
export function canDispatchPayments(mode: string | null | undefined): boolean {
	return mode === 'processor';
}

/** The run's NACHA bank file is offered only on a confirmed record-only
 *  tenant — the server refuses it anywhere else (`nacha_requires_record_only`). */
export function canDownloadBankFile(mode: string | null | undefined): boolean {
	return mode === 'record_only';
}

/**
 * The rails a payment made outside FeohLedger can be recorded on — every rail
 * except `virtual_card`, which is FeohLedger's own card programme and never a
 * way to pay around it (`schemas/payment.py::RecordPaymentOutsideRequest`).
 */
export const EXTERNAL_PAYMENT_METHODS: readonly PaymentMethod[] = [
	'ach',
	'wire',
	'check',
	'bacs',
	'faster_payments',
	'chaps'
];

/** `Payment.provider` on a payment recorded as made outside FeohLedger. */
export const EXTERNAL_PROVIDER = 'external';

export function isExternalPayment(p: { provider?: string | null }): boolean {
	return p.provider === EXTERNAL_PROVIDER;
}

/** `YYYY-MM-DD` for a calendar date, from its own fields (no timezone shift). */
function isoDate(d: Date, utc: boolean): string {
	const y = utc ? d.getUTCFullYear() : d.getFullYear();
	const mo = (utc ? d.getUTCMonth() : d.getMonth()) + 1;
	const day = utc ? d.getUTCDate() : d.getDate();
	return `${y}-${String(mo).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
}

/**
 * Today in UTC, `YYYY-MM-DD` — the earliest NACHA effective date the server
 * accepts (`export_run_nacha` refuses a date before the UTC today).
 */
export function utcToday(now: Date = new Date()): string {
	return isoDate(now, true);
}

/**
 * Today on the reader's own calendar, `YYYY-MM-DD` — the latest `paid_on` the
 * record forms offer. The server allows one day past the UTC date
 * (`api/payments._refuse_future_paid_on`) precisely so a reader east of UTC can
 * record "today"; the reader's local date is always within that slack, and a
 * later one is a future date whatever the timezone.
 */
export function localToday(now: Date = new Date()): string {
	return isoDate(now, false);
}

/** Whether an ISO `YYYY-MM-DD` date is a weekday — a NACHA effective date must
 *  be one (the server 422s a weekend). A malformed value is not. */
export function isBusinessDay(iso: string): boolean {
	const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
	if (!m) return false;
	const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
	if (d.getUTCMonth() !== Number(m[2]) - 1) return false;
	const day = d.getUTCDay();
	return day !== 0 && day !== 6;
}

/**
 * The next banking day after `now` (local calendar), skipping Saturday and
 * Sunday — the default effective-entry date of a NACHA file. Bank holidays are
 * not known here; the date stays editable and the bank settles on its next
 * business day regardless.
 */
export function nextBusinessDay(now: Date = new Date()): string {
	const d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
	do {
		d.setDate(d.getDate() + 1);
	} while (d.getDay() === 0 || d.getDay() === 6);
	return isoDate(d, false);
}

/** The NACHA Standard Entry Class codes the backend writes (`services/nacha`). */
export const NACHA_SEC_CODES = ['CCD', 'PPD'] as const;
export type NachaSecCode = (typeof NACHA_SEC_CODES)[number];

/** `settings.payments.nacha` — the customer's side of a NACHA file. */
export interface NachaSettings {
	company_name: string;
	company_id: string;
	odfi_routing: string;
	bank_name?: string;
}

/** The `settings.payments.nacha` field names, in display order. */
export const NACHA_FIELDS = ['company_name', 'company_id', 'odfi_routing', 'bank_name'] as const;
export type NachaField = (typeof NACHA_FIELDS)[number];

/** The short label each NACHA field is named by in a sentence — the org
 *  settings form, and the `nacha_not_configured` refusal's field list. */
export const NACHA_FIELD_LABEL_KEYS: Record<NachaField, MessageKey> = {
	company_name: 'org.payments.nachaField.companyName',
	company_id: 'org.payments.nachaField.companyId',
	odfi_routing: 'org.payments.nachaField.odfiRouting',
	bank_name: 'org.payments.nachaField.bankName'
};

export function nachaFieldLabelKey(field: string): MessageKey | null {
	return NACHA_FIELD_LABEL_KEYS[field as NachaField] ?? null;
}

/** ABA routing-number checksum (3-7-1 weights), as `utils/banking.validate_aba_routing`. */
export function isValidAbaRouting(raw: string): boolean {
	const digits = raw.replace(/\s+/g, '');
	if (!/^\d{9}$/.test(digits)) return false;
	const w = [3, 7, 1, 3, 7, 1, 3, 7, 1];
	let sum = 0;
	for (let i = 0; i < 9; i++) sum += Number(digits[i]) * w[i];
	return sum % 10 === 0;
}

/**
 * The fields of a NACHA block the server would refuse, mirroring
 * `services/nacha.originator_problems` so the admin is told before the save
 * rather than by a 422. Field names only — never the values.
 */
export function nachaProblems(cfg: Partial<NachaSettings>): NachaField[] {
	const problems: NachaField[] = [];
	const name = (cfg.company_name ?? '').trim();
	if (!name || name.length > 16) problems.push('company_name');
	const id = cfg.company_id ?? '';
	const ascii = [...id].every((c) => c.charCodeAt(0) < 128);
	if (id.length !== 10 || !ascii) problems.push('company_id');
	if (!isValidAbaRouting(cfg.odfi_routing ?? '')) problems.push('odfi_routing');
	if (cfg.bank_name !== undefined && cfg.bank_name.length > 23) problems.push('bank_name');
	return problems;
}

/**
 * The NACHA block to SAVE from the form's four fields: `null` when every field
 * is blank (no bank file configured — the record-outside path still works),
 * otherwise the block with a blank optional `bank_name` left out.
 */
export function nachaForSave(form: NachaSettings): NachaSettings | null {
	const company_name = form.company_name.trim();
	const company_id = form.company_id.trim();
	const odfi_routing = form.odfi_routing.replace(/\s+/g, '');
	const bank_name = (form.bank_name ?? '').trim();
	if (!company_name && !company_id && !odfi_routing && !bank_name) return null;
	const out: NachaSettings = { company_name, company_id, odfi_routing };
	if (bank_name) out.bank_name = bank_name;
	return out;
}
