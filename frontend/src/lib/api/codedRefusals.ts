/**
 * Localizing the backend's coded refusals — the one registry the transport
 * consults before it falls back to the server's English.
 *
 * A refusal a user is expected to ACT on arrives as `detail = {code, message,
 * params}` (`backend/app/api/refusals.py::coded_refusal`): a stable `code`, the
 * typed `params` its sentence needs (money as exact decimal strings beside
 * their currency), and the English `message` as the fallback. `api.ts::
 * apiErrorFromBody` calls {@link localizeApiDetail} once per error response, so
 * every toast and banner on every page is stated in the reader's language
 * without a per-call-site change (`docs/decisions.md` §232).
 *
 * The registry, in the order it is tried:
 *
 * 1. the GL-chart refusal (`api/glChartRefusal.ts`) — structured, not
 *    `{code, params}`, so it keeps its own parser;
 * 2. the money-path refusals below — approval segregation and the named
 *    approver / chain-reuse gates, the CFO and max-amount gates (invoice and
 *    expense report), the exception queue's segregation refusal, credit-memo
 *    application, the invoice stale-edit 409, a wrong authenticator code, and
 *    the no-rail pilot's refusals (a record-only tenant's dispatch refusal,
 *    recording a payment made outside FeohLedger, the NACHA bank file), and
 *    the plan-feature 402 every tier gate answers with, and recording or
 *    cancelling a goods receipt;
 * 3. the auth step-up / passkey refusals (`api/authRefusals.ts`).
 *
 * **Anything this build cannot state → `null`**, and the caller renders the
 * server's sentence: an unknown code (a newer backend), a known code whose
 * params are missing or malformed (a sentence with a blank where a figure
 * belongs is worse than the server's complete English one).
 *
 * A client that BRANCHES on a refusal keys on `ApiError.code`, never on the
 * text — the text is localized here, so a substring match would fail in five
 * locales. {@link INVOICE_STALE_EDIT} is the one a page branches on today.
 *
 * Imports nothing from the Svelte runtime or `$app/*` (the caller passes `m`
 * in), so it stays unit-testable under vitest's node environment.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { formatMoney } from '#lib/utils/money.ts';
import { invoiceStatusLabelKey } from '#lib/types/invoice.ts';
import { paymentStatusLabelKey } from '#lib/types/payment.ts';
import { exceptionTypeFallback, exceptionTypeLabelKey } from '#lib/types/exception.ts';
import { nachaFieldLabelKey } from '#lib/types/paymentExecution.ts';
import { localizeGlChartRefusal, parseGlChartRefusal } from '#lib/api/glChartRefusal.ts';
import { authRefusalText } from '#lib/api/authRefusals.ts';
import { apiErrorCode } from '#lib/utils/apiError.ts';
import { formatList } from '#lib/utils/list.ts';
import { isPlanFeature, planFeatureLabelKey, planTierLabelKey } from '#lib/types/planFeatures.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;
type Params = Record<string, unknown>;

/** A plan-feature gate's 402 (`backend/app/api/deps.py::PLAN_FEATURE_REQUIRED`). */
export const PLAN_FEATURE_REQUIRED = 'plan_feature_required';

/** `PATCH /api/invoices/{id}`'s optimistic-concurrency 409
 *  (`backend/app/api/invoice_version.py::INVOICE_STALE_EDIT`). */
export const INVOICE_STALE_EDIT = 'invoice_stale_edit';

/** Approval's version check: the invoice changed after the approver loaded it
 *  (`backend/app/api/invoice_version.py::INVOICE_STALE_APPROVAL`). The invoice
 *  modal branches on it to drop the stale copy and reopen the current one. */
export const INVOICE_STALE_APPROVAL = 'invoice_stale_approval';

/** `POST /api/invoices/{id}/complete` refused for blank required fields
 *  (`backend/app/api/workflow.py::INVOICE_REQUIRED_FIELDS_MISSING`). */
export const INVOICE_REQUIRED_FIELDS_MISSING = 'invoice_required_fields_missing';

/** The field names that refusal sends, labelled as the invoice table heads them. */
const INVOICE_FIELD_KEYS: Record<string, MessageKey> = {
	vendor: 'invoices.col.vendor',
	invoice_number: 'invoices.col.invoiceNumber',
	amount: 'invoices.col.amount'
};

// --- param readers: `null` means "malformed — fall back to the message" ----

function str(params: Params, key: string): string | null {
	const v = params[key];
	return typeof v === 'string' && v.trim() ? v : null;
}

/** An exact decimal string (`"12000.00"`), as `json_money_string` sends it. */
function moneyStr(params: Params, key: string): string | null {
	const v = str(params, key);
	return v !== null && /^-?\d+(\.\d+)?$/.test(v) ? v : null;
}

function currency(params: Params, key: string): string | null {
	const v = str(params, key);
	return v !== null && /^[A-Za-z]{3}$/.test(v) ? v.toUpperCase() : null;
}

function money(params: Params, amountKey: string, currencyKey: string): string | null {
	const amount = moneyStr(params, amountKey);
	const code = currency(params, currencyKey);
	if (amount === null || code === null) return null;
	return formatMoney(amount, { currency: code });
}

/**
 * A figure in the INVOICE's own currency, which is `null` for an invoice that
 * names none. Then the figure renders bare (the `formatMoney` rule), never with
 * a borrowed symbol; only a currency that is present but malformed falls back.
 */
function invoiceMoney(params: Params, amountKey: string, currencyKey: string): string | null {
	const raw = params[currencyKey];
	if (raw !== null && raw !== undefined) return money(params, amountKey, currencyKey);
	const amount = moneyStr(params, amountKey);
	return amount === null ? null : formatMoney(amount, { currency: null });
}

/** A status named inside a sentence: its label when this build knows it,
 *  otherwise the raw value (visible and searchable, never blank). */
function statusLabel(
	status: string,
	keyFor: (s: string) => MessageKey | null,
	t: Translate
): string {
	const key = keyFor(status);
	return key ? t(key) : status;
}

const CREDIT_MEMO_STATUS_KEYS: Record<string, MessageKey> = {
	open: 'creditMemos.status.open',
	applied: 'creditMemos.status.applied',
	void: 'creditMemos.status.void'
};

// --- the invoice approval money gates ---------------------------------------

/**
 * The two optional notes a money-gate refusal carries after its sentence: the
 * structuring aggregate (`recent_spend` set) and what was actually measured
 * (`expressible: false`, or a `measured_amount` in the limit's currency). A
 * malformed note → `null` for the whole refusal, never a half-stated one.
 */
function gateNotes(params: Params, t: Translate): string[] | null {
	const notes: string[] = [];
	if (params.recent_spend !== null && params.recent_spend !== undefined) {
		const recent = invoiceMoney(params, 'recent_spend', 'currency');
		const aggregate = invoiceMoney(params, 'aggregate_amount', 'currency');
		const days = params.window_days;
		if (recent === null || aggregate === null) return null;
		if (typeof days !== 'number' || !Number.isInteger(days) || days < 0) return null;
		notes.push(t('refusal.gateStructuring', { recent, aggregate, days }));
	}
	const limitCurrency = currency(params, 'limit_currency');
	if (limitCurrency === null) return null;
	if (params.expressible === false) {
		notes.push(t('refusal.gateInexpressible', { currency: limitCurrency }));
	} else if (params.measured_amount !== null && params.measured_amount !== undefined) {
		const measured = money(params, 'measured_amount', 'limit_currency');
		if (measured === null) return null;
		notes.push(t('refusal.gateMeasured', { measured, currency: limitCurrency }));
	}
	return notes;
}

function approvalGate(
	params: Params,
	t: Translate,
	withLimit: MessageKey,
	withoutLimit: MessageKey | null
): string | null {
	const amount = invoiceMoney(params, 'amount', 'currency');
	if (amount === null) return null;
	let head: string;
	if (params.limit === null || params.limit === undefined) {
		if (!withoutLimit) return null;
		head = t(withoutLimit, { amount });
	} else {
		const limit = money(params, 'limit', 'limit_currency');
		if (limit === null) return null;
		head = t(withLimit, { amount, limit });
	}
	const notes = gateNotes(params, t);
	return notes === null ? null : [head, ...notes].join(' ');
}

// --- the expense-report CFO gate ---------------------------------------------

function expenseCfoGate(params: Params, t: Translate): string | null {
	const reporting = currency(params, 'currency');
	if (reporting === null) return null;
	const hasLimit = params.limit !== null && params.limit !== undefined;
	const limit = hasLimit ? money(params, 'limit', 'currency') : null;
	if (hasLimit && limit === null) return null;
	if (params.amount === null || params.amount === undefined) {
		const reportCurrency = currency(params, 'report_currency');
		if (reportCurrency === null) return null;
		return limit === null
			? t('refusal.expenseCfoInexpressibleUnknownLimit', { currency: reporting, reportCurrency })
			: t('refusal.expenseCfoInexpressible', { currency: reporting, reportCurrency, limit });
	}
	const amount = money(params, 'amount', 'currency');
	if (amount === null) return null;
	return limit === null
		? t('refusal.expenseCfoRequiredUnknownLimit', { amount })
		: t('refusal.expenseCfoRequired', { amount, limit });
}

// --- the no-rail pilot (issue #517) -------------------------------------------

/**
 * A recording refusal, stated for one invoice or for a whole run. The run-level
 * door (`POST /runs/{id}/record-outside`) refuses the run on its first bad
 * payment and adds `params.invoice_number`, so the sentence is prefixed with
 * the invoice it names. That key PRESENT but null (an invoice with no number —
 * the server then names its id in the English) falls back to the server's
 * sentence rather than dropping which invoice it was.
 */
const forInvoice =
	(build: Builder): Builder =>
	(params, t) => {
		const sentence = build(params, t);
		if (sentence === null || !('invoice_number' in params)) return sentence;
		const invoice = str(params, 'invoice_number');
		return invoice === null ? null : t('refusal.externalPaymentForInvoice', { invoice, reason: sentence });
	};

/** A NACHA refusal that names the invoice whose payment the file can't carry. */
const nachaForInvoice =
	(key: MessageKey): Builder =>
	(params, t) => {
		const invoice = str(params, 'invoice_number');
		return invoice === null ? null : t(key, { invoice });
	};

function nachaNotConfigured(params: Params, t: Translate): string | null {
	const missing = params.missing;
	if (!Array.isArray(missing) || missing.length === 0) return null;
	if (!missing.every((f) => typeof f === 'string' && f)) return null;
	const labels = (missing as string[]).map((f) => {
		const key = nachaFieldLabelKey(f);
		return key ? t(key) : f;
	});
	return t('refusal.nachaNotConfigured', { fields: formatList(labels) });
}

// --- the table ---------------------------------------------------------------

type Builder = (params: Params, t: Translate) => string | null;

/** A refusal whose sentence names nothing. */
const fixed =
	(key: MessageKey): Builder =>
	(_params, t) =>
		t(key);

const BUILDERS: Record<string, Builder> = {
	// `services/approval_chain.py`
	approval_segregation: fixed('refusal.approvalSegregation'),
	approval_level_reuse: fixed('refusal.approvalLevelReuse'),
	approval_not_named_approver: fixed('refusal.approvalNotNamedApprover'),
	// `services/review.py::_enforce_approval_thresholds`
	approval_max_amount_exceeded: (p, t) => approvalGate(p, t, 'refusal.approvalMaxExceeded', null),
	approval_max_amount_misconfigured: fixed('refusal.approvalMaxMisconfigured'),
	approval_cfo_required: (p, t) =>
		approvalGate(p, t, 'refusal.approvalCfoRequired', 'refusal.approvalCfoRequiredUnknownLimit'),
	// `api/expenses.py` — the report approval CFO gate
	expense_cfo_required: expenseCfoGate,
	// `services/exception_lifecycle.py` — the SAME codes `/bulk/resolve`
	// reports per skipped row, so the two doors agree on the names.
	segregation_raiser: fixed('refusal.exceptionSegregationRaiser'),
	segregation_implicated: fixed('refusal.exceptionSegregationImplicated'),
	// `api/credit_memos.py`
	credit_memo_vendor_mismatch: fixed('refusal.creditMemoVendorMismatch'),
	credit_memo_vendor_unresolved: fixed('refusal.creditMemoVendorUnresolved'),
	credit_memo_entity_mismatch: fixed('refusal.creditMemoEntityMismatch'),
	credit_memo_currency_mismatch: fixed('refusal.creditMemoCurrencyMismatch'),
	credit_memo_invoice_settled: (p, t) => {
		const status = str(p, 'status');
		return status === null
			? null
			: t('refusal.creditMemoInvoiceSettled', {
					status: statusLabel(status, invoiceStatusLabelKey, t)
				});
	},
	credit_memo_not_editable: (p, t) => {
		const status = str(p, 'status');
		return status === null
			? null
			: t('refusal.creditMemoNotEditable', {
					status: statusLabel(status, (s) => CREDIT_MEMO_STATUS_KEYS[s] ?? null, t)
				});
	},
	credit_memo_not_applicable: (p, t) => {
		const status = str(p, 'status');
		return status === null
			? null
			: t('refusal.creditMemoNotApplicable', {
					status: statusLabel(status, (s) => CREDIT_MEMO_STATUS_KEYS[s] ?? null, t)
				});
	},
	credit_memo_exceeds_balance: (p, t) => {
		const remaining = invoiceMoney(p, 'remaining', 'currency');
		return remaining === null ? null : t('refusal.creditMemoExceedsBalance', { remaining });
	},
	// `api/invoices.py` / `api/workflow.py`
	[INVOICE_STALE_EDIT]: fixed('refusal.invoiceStaleEdit'),
	[INVOICE_STALE_APPROVAL]: fixed('refusal.invoiceStaleApproval'),
	[INVOICE_REQUIRED_FIELDS_MISSING]: (p, t) => {
		const fields = p.fields;
		if (!Array.isArray(fields) || fields.length === 0) return null;
		if (!fields.every((f) => typeof f === 'string' && f)) return null;
		const labels = (fields as string[]).map((f) =>
			INVOICE_FIELD_KEYS[f] ? t(INVOICE_FIELD_KEYS[f]) : f
		);
		return t('refusal.invoiceRequiredFieldsMissing', { fields: formatList(labels) });
	},
	// `api/auth.py` + `api/portal_auth.py` — a signed-in factor change
	mfa_code_invalid: fixed('refusal.mfaCodeInvalid'),
	// `api/payments.py::_refuse_record_only` — every dispatching endpoint on a
	// tenant whose payments are recorded, never sent. `params.reason` says why
	// the mode resolved so; the sentence is the same for every reason.
	payments_record_only: fixed('refusal.paymentsRecordOnly'),
	// `services/external_payment.py` (+ `api/payments.py` for the date) — both
	// `POST /record-outside` and the run-level door.
	external_payment_not_payable: forInvoice((p, t) => {
		const status = str(p, 'status');
		return status === null
			? null
			: t('refusal.externalPaymentNotPayable', {
					status: statusLabel(status, invoiceStatusLabelKey, t)
				});
	}),
	external_payment_segregation: forInvoice(fixed('refusal.externalPaymentSegregation')),
	external_payment_blocking_exception: forInvoice((p, t) => {
		const type = str(p, 'exception_type');
		if (type === null) return null;
		const key = exceptionTypeLabelKey(type);
		return t('refusal.externalPaymentBlockingException', {
			type: key ? t(key) : exceptionTypeFallback(type)
		});
	}),
	external_payment_card_live: forInvoice(fixed('refusal.externalPaymentCardLive')),
	external_payment_credit_conflict: forInvoice(fixed('refusal.externalPaymentCreditConflict')),
	external_payment_nothing_to_pay: forInvoice(fixed('refusal.externalPaymentNothingToPay')),
	external_payment_payment_live: forInvoice((p, t) => {
		const status = str(p, 'payment_status');
		return status === null
			? null
			: t('refusal.externalPaymentPaymentLive', {
					status: statusLabel(status, paymentStatusLabelKey, t)
				});
	}),
	external_payment_in_run: forInvoice((p, t) => {
		const run = str(p, 'payment_run_id');
		return run === null ? null : t('refusal.externalPaymentInRun', { run: run.slice(0, 8) });
	}),
	external_payment_amount_mismatch: forInvoice((p, t) => {
		const amount = invoiceMoney(p, 'amount', 'currency');
		return amount === null ? null : t('refusal.externalPaymentAmountMismatch', { amount });
	}),
	// The run-level door only (it always adds `invoice_number`): what the
	// invoice owes moved since the run was staged.
	external_payment_amount_changed: forInvoice((p, t) => {
		const staged = invoiceMoney(p, 'staged_amount', 'currency');
		const amount = invoiceMoney(p, 'amount', 'currency');
		return staged === null || amount === null
			? null
			: t('refusal.externalPaymentAmountChanged', { staged, amount });
	}),
	external_payment_paid_on_future: fixed('refusal.externalPaymentPaidOnFuture'),
	// `api/payments.py` — `GET /runs/{id}/nacha` (`services/nacha.py`)
	nacha_requires_record_only: fixed('refusal.nachaRequiresRecordOnly'),
	nacha_not_configured: nachaNotConfigured,
	nacha_payment_not_ach: nachaForInvoice('refusal.nachaPaymentNotAch'),
	nacha_vendor_bank_missing: nachaForInvoice('refusal.nachaVendorBankMissing'),
	nacha_currency_not_usd: nachaForInvoice('refusal.nachaCurrencyNotUsd'),
	nacha_amount_too_large: nachaForInvoice('refusal.nachaAmountTooLarge'),
	// `params.reason` is the dispatcher's machine code (`invoice_blocked:…`,
	// `net_amount_changed`, …) — not prose, so it is not shown; the sentence
	// names the invoice and the one remedy, which is the same for every reason.
	nacha_payment_not_payable: nachaForInvoice('refusal.nachaPaymentNotPayable'),
	nacha_already_exported: fixed('refusal.nachaAlreadyExported'),
	// `api/deps.py::plan_feature_refusal` — every plan-feature gate (decisions
	// §258). `params.feature` is a `FEATURE_*` key; one this build does not
	// know (a newer backend) falls back to the server's sentence.
	[PLAN_FEATURE_REQUIRED]: (p, t) => {
		const feature = p.feature;
		if (!isPlanFeature(feature)) return null;
		return t('refusal.planFeatureRequired', {
			feature: t(planFeatureLabelKey(feature)),
			plan: t(planTierLabelKey(feature))
		});
	},
	// `services/goods_receipts.py` — recording / cancelling a delivery
	goods_receipt_po_cancelled: (p, t) => {
		const poNumber = str(p, 'poNumber');
		return poNumber === null ? null : t('refusal.goodsReceiptPoCancelled', { poNumber });
	},
	goods_receipt_number_taken: (p, t) => {
		const grNumber = str(p, 'grNumber');
		return grNumber === null ? null : t('refusal.goodsReceiptNumberTaken', { grNumber });
	},
	goods_receipt_line_not_on_po: fixed('refusal.goodsReceiptLineNotOnPo'),
	goods_receipt_line_duplicated: fixed('refusal.goodsReceiptLineDuplicated'),
	goods_receipt_line_required: fixed('refusal.goodsReceiptLineRequired'),
	goods_receipt_nothing_received: fixed('refusal.goodsReceiptNothingReceived'),
	goods_receipt_idempotency_reused: fixed('refusal.goodsReceiptIdempotencyReused'),
	goods_receipt_not_manual: fixed('refusal.goodsReceiptNotManual'),
	goods_receipt_already_cancelled: (p, t) => {
		const grNumber = str(p, 'grNumber');
		return grNumber === null ? null : t('refusal.goodsReceiptAlreadyCancelled', { grNumber });
	}
};

/** The codes this registry states (the money-path + MFA table; the GL-chart
 *  and auth refusals have their own modules and pins). */
export const CODED_REFUSAL_CODES = Object.keys(BUILDERS);

/**
 * The localized sentence for a `{code, params}` refusal this table knows, or
 * `null` — then the caller renders the server's `message`. Never throws.
 */
export function codedRefusalText(code: string, params: Params, t: Translate): string | null {
	const build = BUILDERS[code];
	if (!build) return null;
	try {
		return build(params, t);
	} catch {
		// A formatter that throws on a value this build did not expect must
		// degrade to the server's sentence, never blank the error.
		return null;
	}
}

/**
 * The localized sentence for any structured error body this build recognises —
 * a response `detail` or a CSV row error — or `null`, in which case the caller
 * renders the server's text. The one entry point `api.ts` and the CSV import
 * modal call, so a new localized refusal joins the registry here rather than
 * at every call site.
 */
export function localizeApiDetail(detail: unknown, t: Translate): string | null {
	const chart = parseGlChartRefusal(detail);
	if (chart) return localizeGlChartRefusal(chart, t);
	const { code, params } = apiErrorCode(detail);
	if (!code) return null;
	const message =
		detail && typeof detail === 'object' && typeof (detail as Params).message === 'string'
			? ((detail as Params).message as string)
			: '';
	return codedRefusalText(code, params, t) ?? authRefusalText({ code, params, message }, t);
}
