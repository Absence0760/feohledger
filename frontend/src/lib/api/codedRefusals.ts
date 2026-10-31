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
 *    the plan-feature 402 every tier gate answers with, recording or
 *    cancelling a goods receipt, an ERP send refused for an open duplicate
 *    or fraud exception or because FeohLedger is already paying the invoice
 *    (both stated by `api/erpRefusals.ts`), an ERP sync
 *    refused because the connection is gone or the listing is incomplete,
 *    and every self-service signup refusal (the workspace address, the
 *    email and name checks, the captcha, the verification link), the
 *    shared rate limiter's 429 on every surface it guards, and every vendor
 *    merge refusal (self-merge, cross-entity, a payments-blocked duplicate);
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
import { ownWorkspaceSignInUrl } from '#lib/api/sessionRejection.ts';
import {
	ERP_CALL_BUDGET_EXHAUSTED,
	EXCEPTION_BLOCKS_ERP_EXPORT,
	PAYMENT_BLOCKS_ERP_EXPORT,
	VENDOR_BLOCKS_ERP_EXPORT,
	erpRefusalText
} from '#lib/api/erpRefusals.ts';
import { apiErrorCode } from '#lib/utils/apiError.ts';
import { formatList } from '#lib/utils/list.ts';
import { isPlanFeature, planFeatureLabelKey, planTierLabelKey } from '#lib/types/planFeatures.ts';
import { refusalBlockers } from '#lib/types/workspaceClosureRefusal.ts';
import { vendorStatusLabelKey } from '#lib/types/vendor.ts';

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

/** `POST /api/signup/complete` for an address that already has an account
 *  (`backend/app/api/signup.py::SIGNUP_EMAIL_HAS_WORKSPACE`). `/verify`
 *  branches on it to link the existing workspace's sign-in page. */
export const SIGNUP_EMAIL_HAS_WORKSPACE = 'signup_email_has_workspace';

/**
 * The `/api/signup/*` refusals that are about ONE form field carry
 * `params.field`, the `SignupStartRequest` field name, so the signup page can
 * put the (already localized) sentence under that field
 * (`backend/app/api/signup.py`). `null` when the refusal names no field.
 */
export function refusalField(params: Record<string, unknown>): string | null {
	const field = params.field;
	return typeof field === 'string' && field ? field : null;
}

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

// --- the shared rate limiter ---------------------------------------------------

/** Every 429 the shared limiter answers (`backend/app/services/rate_limit.py`):
 *  sign-in, MFA, password reset, signup, the supplier portal, the public API. */
export const RATE_LIMITED = 'rate_limited';

/**
 * The limiter's wait, said the way a person would: whole seconds under a
 * minute, then whole minutes, then whole hours — each rounded UP, so the
 * reader is never told to come back before the server will let them. The
 * params carry only the `Retry-After` figure; the sentence adds nothing the
 * server did not say (which limit tripped is deliberately not sent).
 */
function rateLimited(params: Params, t: Translate): string | null {
	const seconds = params.retry_after_seconds;
	if (typeof seconds !== 'number' || !Number.isInteger(seconds) || seconds < 1) return null;
	if (seconds < 60) return t('refusal.rateLimitedSeconds', { n: seconds });
	const minutes = Math.ceil(seconds / 60);
	if (minutes < 60) return t('refusal.rateLimitedMinutes', { n: minutes });
	return t('refusal.rateLimitedHours', { n: Math.ceil(seconds / 3600) });
}

// --- the table ---------------------------------------------------------------

type Builder = (params: Params, t: Translate) => string | null;

/** A refusal whose sentence names nothing. */
const fixed =
	(key: MessageKey): Builder =>
	(_params, t) =>
		t(key);

/** A workspace-address refusal that names the address (`params.slug`). */
const slugNamed =
	(key: MessageKey): Builder =>
	(params, t) => {
		const slug = str(params, 'slug');
		return slug === null ? null : t(key, { slug });
	};

const BUILDERS: Record<string, Builder> = {
	// `services/approval_chain.py`
	approval_segregation: fixed('refusal.approvalSegregation'),
	approval_segregation_receiver: fixed('refusal.approvalSegregationReceiver'),
	approval_level_reuse: fixed('refusal.approvalLevelReuse'),
	approval_not_named_approver: fixed('refusal.approvalNotNamedApprover'),
	// `services/review.py::refuse_unless_in_review`
	invoice_not_in_review: fixed('refusal.invoiceNotInReview'),
	// `services/payment_controls.py` — the run dialog's `actions` verdict
	// Maker-checker names the step it refuses in `params.step` for the two
	// that are not approve / execute (`payment_controls._segregation_for`).
	payment_run_segregation: (p, t) => {
		const step = str(p, 'step');
		if (step === 'nacha_export') return t('refusal.paymentRunSegregationExport');
		if (step === 'record_outside') return t('refusal.paymentRunSegregationRecord');
		return t('refusal.paymentRunSegregation');
	},
	payment_run_awaiting_cfo: fixed('refusal.paymentRunAwaitingCfo'),
	payment_run_nothing_to_retry: fixed('refusal.paymentRunNothingToRetry'),
	payment_run_nothing_settled: fixed('refusal.paymentRunNothingSettled'),
	payment_run_not_recordable: fixed('refusal.paymentRunNotRecordable'),
	payment_run_nothing_to_record: fixed('refusal.paymentRunNothingToRecord'),
	// …and the History row's `accept_settlement_refusal`
	payment_settlement_not_completed: fixed('refusal.paymentSettlementNotCompleted'),
	payment_settlement_covered: fixed('refusal.paymentSettlementCovered'),
	payment_settlement_not_held: fixed('refusal.paymentSettlementNotHeld'),
	// `api/vendors.py::change_request_approve_refusal` — the queue's verdict
	vendor_change_self_requested: fixed('refusal.vendorChangeSelfRequested'),
	vendor_change_self_provisioned: fixed('refusal.vendorChangeSelfProvisioned'),
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
	// `api/workflow.py` — `/complete`, `/send-to-erp`, `/retry-erp` refused
	// for an open duplicate / fraud exception. One sentence with the invoice
	// modal's ERP panel, which states the same code off the audit row.
	[EXCEPTION_BLOCKS_ERP_EXPORT]: (p, t) =>
		erpRefusalText({ code: EXCEPTION_BLOCKS_ERP_EXPORT, params: p }, t),
	// ...and for an invoice FeohLedger is already paying (a live payment or
	// virtual card), the same way.
	[PAYMENT_BLOCKS_ERP_EXPORT]: (p, t) =>
		erpRefusalText({ code: PAYMENT_BLOCKS_ERP_EXPORT, params: p }, t),
	// ...and for a vendor the payment path will not pay (blocked,
	// sanctions-held, rejected), the same way.
	[VENDOR_BLOCKS_ERP_EXPORT]: (p, t) =>
		erpRefusalText({ code: VENDOR_BLOCKS_ERP_EXPORT, params: p }, t),
	// `api/refusals.py::erp_pull_failure` — a chart / vendor / PO sync whose
	// OAuth connection is gone (the ERP panel's own "not connected" sentence),
	// or whose listing could not be read whole, so nothing synced.
	// `api/refusals.py::refuse_due_before_invoice_date` — create / edit.
	invoice_due_before_date: fixed('refusal.invoiceDueBeforeDate'),
	erp_not_connected: fixed('erpRefusal.notConnected'),
	// ...or whose call the ERP's shared API quota refused before it was made
	// (a 429, decisions §322): the push refusal's own sentence.
	[ERP_CALL_BUDGET_EXHAUSTED]: (p, t) =>
		erpRefusalText({ code: ERP_CALL_BUDGET_EXHAUSTED, params: p }, t),
	erp_listing_incomplete: (p, t) => {
		const provider = str(p, 'provider');
		if (provider === null) return null;
		const gap = str(p, 'gap');
		if (gap === 'fetch_failed') return t('erpSync.listingFetchFailed', { provider });
		if (gap === 'truncated') return t('erpSync.listingTruncated', { provider });
		return null;
	},
	// `api/auth.py` + `api/portal_auth.py` — a signed-in factor change
	mfa_code_invalid: fixed('refusal.mfaCodeInvalid'),
	// `api/auth.py::_refuse_other_workspace` — signing in on another
	// workspace's page. `sign_in_url` is the account's own sign-in page, query
	// string (`?email=`) included; the sentence names only its host, which is
	// readable, and leaves the link itself to a button (the login page renders
	// one under the text). A URL that is not a plain http(s) one names nothing.
	wrong_workspace: (p, t) => {
		const url = ownWorkspaceSignInUrl(p);
		return url
			? t('refusal.wrongWorkspace', { workspace: new URL(url).host })
			: t('refusal.wrongWorkspaceNoLink');
	},
	// `api/signup.py::_refuse_if_email_has_account` — a verified signup for an
	// address that already has an account; the link is that account's own
	// sign-in page (decisions §292).
	[SIGNUP_EMAIL_HAS_WORKSPACE]: (p, t) => {
		const url = str(p, 'sign_in_url');
		return url
			? t('refusal.signupEmailHasWorkspace', { url })
			: t('refusal.signupEmailHasWorkspaceNoLink');
	},
	// `api/signup.py` — the self-service signup form and `/verify`. The
	// workspace-address rules are `utils/slug.py`'s (the slug-check reports
	// the same codes, with the address it checked as `slug`); the captcha's
	// are `utils/hcaptcha.py`'s. User-facing text says "workspace address",
	// never "slug".
	slug_required: fixed('refusal.slugRequired'),
	slug_format: fixed('refusal.slugFormat'),
	slug_reserved: slugNamed('refusal.slugReserved'),
	slug_hyphen_edge: fixed('refusal.slugHyphenEdge'),
	slug_double_hyphen: fixed('refusal.slugDoubleHyphen'),
	slug_taken: slugNamed('refusal.slugTaken'),
	// Resting after its workspace was closed (`utils/slug.ensure_slug_not_held`,
	// decisions §306): nobody owns it, so the sentence is not "taken".
	signup_slug_held: fixed('refusal.signupSlugHeld'),
	signup_email_invalid: fixed('refusal.signupEmailInvalid'),
	signup_name_has_link: fixed('refusal.signupNameHasLink'),
	captcha_required: fixed('refusal.captchaRequired'),
	captcha_unavailable: fixed('refusal.captchaUnavailable'),
	captcha_failed: fixed('refusal.captchaFailed'),
	signup_email_send_failed: fixed('refusal.signupEmailSendFailed'),
	signup_link_invalid: fixed('refusal.signupLinkInvalid'),
	signup_unavailable: fixed('refusal.signupUnavailable'),
	signup_provisioning_failed: fixed('refusal.signupProvisioningFailed'),
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
	// `services/payment_controls.py::run_nacha_export_refusal` /
	// `run_nacha_void_refusal` — the run dialog's bank-file verdicts
	nacha_run_not_exportable: fixed('refusal.nachaRunNotExportable'),
	nacha_nothing_to_regenerate: fixed('refusal.nachaNothingToRegenerate'),
	nacha_nothing_to_export: fixed('refusal.nachaNothingToExport'),
	nacha_run_not_exported: fixed('refusal.nachaRunNotExported'),
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
	},
	// The commerce connection and receipt sync (`api/commerce.py`, decisions §273).
	commerce_not_configured: fixed('refusal.commerceNotConfigured'),
	commerce_oauth_not_available: fixed('refusal.commerceOauthNotAvailable'),
	commerce_sync_not_retryable: fixed('refusal.commerceSyncNotRetryable'),
	commerce_sync_not_found: fixed('refusal.commerceSyncNotFound'),
	commerce_unknown_entity: fixed('refusal.commerceUnknownEntity'),
	commerce_unknown_provider: fixed('refusal.commerceUnknownProvider'),
	// One address, one account, one workspace (decisions §291). An SSO / SAML
	// sign-in whose IdP asserted an address held in another workspace — stated
	// on the callback page, deliberately with no link (`api/refusals.py`) — and
	// a user-manager's create / rename onto a taken address.
	sso_email_in_other_workspace: fixed('refusal.ssoEmailInOtherWorkspace'),
	email_in_this_workspace: fixed('refusal.emailInThisWorkspace'),
	email_in_other_workspace: fixed('refusal.emailInOtherWorkspace'),
	// The admin path's other-workspace budget is spent (`api/admin.py`).
	email_check_throttled: fixed('refusal.emailCheckThrottled'),
	// Any 429 from the shared limiter (`services/rate_limit.py`).
	[RATE_LIMITED]: rateLimited,
	// A refused vendor merge (`services/vendor_merge.py`; the malformed-id 422
	// is raised by its endpoint, `api/enrichment.py`). No params — the
	// sentence never names a vendor, its tax id or its bank.
	vendor_merge_self: fixed('refusal.vendorMergeSelf'),
	vendor_merge_no_duplicates: fixed('refusal.vendorMergeNoDuplicates'),
	vendor_merge_canonical_not_found: fixed('refusal.vendorMergeCanonicalNotFound'),
	vendor_merge_duplicate_not_found: fixed('refusal.vendorMergeDuplicateNotFound'),
	vendor_merge_invalid_id: fixed('refusal.vendorMergeInvalidId'),
	vendor_merge_cross_entity: fixed('refusal.vendorMergeCrossEntity'),
	vendor_merge_blocked_duplicate: fixed('refusal.vendorMergeBlockedDuplicate'),
	// Closing a workspace (`api/organization_close.py`, decisions §306). The
	// refusal's blockers are listed by the page itself; the toast is the
	// fixed sentence, and a blocker list this build cannot read falls back.
	workspace_close_confirmation_mismatch: fixed('refusal.workspaceCloseConfirmationMismatch'),
	workspace_close_refused: (p, t) =>
		refusalBlockers(p) === null ? null : t('refusal.workspaceCloseRefused'),
	workspace_close_in_progress: fixed('refusal.workspaceCloseInProgress'),
	workspace_close_actor_not_admin: fixed('refusal.workspaceCloseActorNotAdmin'),
	workspace_close_audit_unavailable: fixed('refusal.workspaceCloseAuditUnavailable'),
	workspace_close_incomplete: fixed('refusal.workspaceCloseIncomplete'),
	// A CSV invoice row whose number is the same payable as another once
	// formatting is ignored (`services/csv_import`, decisions §77) — a row error.
	csv_invoice_number_matches_existing: (p, t) => {
		const number = str(p, 'invoice_number');
		const existing = str(p, 'existing_invoice_number');
		return number === null || existing === null
			? null
			: t('refusal.csvInvoiceNumberMatchesExisting', { number, existing });
	},
	// The match is another entity's invoice: stated as existence only (§40).
	csv_invoice_number_matches_other_entity: (p, t) => {
		const number = str(p, 'invoice_number');
		return number === null ? null : t('refusal.csvInvoiceNumberMatchesOtherEntity', { number });
	},
	csv_invoice_number_matches_row: (p, t) => {
		const number = str(p, 'invoice_number');
		const other = str(p, 'other_invoice_number');
		const row = p.other_row;
		return number === null || other === null || typeof row !== 'number'
			? null
			: t('refusal.csvInvoiceNumberMatchesRow', { number, other, row });
	},
	// Why `POST /api/cards/generate` minted nothing for one invoice
	// (`services/card_issuance.CARD_SKIP_MESSAGES`) — one per gate.
	card_skip_not_found: fixed('refusal.cardSkipNotFound'),
	card_skip_not_payable: (p, t) => {
		const status = str(p, 'status');
		return status === null
			? null
			: t('refusal.cardSkipNotPayable', {
					status: statusLabel(status, invoiceStatusLabelKey, t)
				});
	},
	card_skip_vendor_changed: fixed('refusal.cardSkipVendorChanged'),
	card_skip_lock_timeout: fixed('refusal.cardSkipLockTimeout'),
	card_skip_already_carded: fixed('refusal.cardSkipAlreadyCarded'),
	card_skip_blocked: (p, t) => {
		const type = str(p, 'exception_type');
		if (type === null) return null;
		const key = exceptionTypeLabelKey(type);
		return t('refusal.cardSkipBlocked', { type: key ? t(key) : exceptionTypeFallback(type) });
	},
	card_skip_vendor_not_payable: (p, t) => {
		const status = str(p, 'vendor_status');
		return status === null
			? null
			: t('refusal.cardSkipVendorNotPayable', {
					status: statusLabel(status, vendorStatusLabelKey, t)
				});
	},
	card_skip_live_payment: fixed('refusal.cardSkipLivePayment'),
	card_skip_credit_conflict: fixed('refusal.cardSkipCreditConflict'),
	card_skip_nothing_owed: fixed('refusal.cardSkipNothingOwed'),
	card_skip_vendor_unscreenable: fixed('refusal.cardSkipVendorUnscreenable'),
	card_skip_compliance_hold: fixed('refusal.cardSkipComplianceHold'),
	card_skip_erp_bill: fixed('refusal.cardSkipErpBill'),
	card_skip_issue_failed: fixed('refusal.cardSkipIssueFailed'),
	// The whole `/cards/generate` call, on a tenant whose programme is off.
	cards_not_enabled: fixed('refusal.cardsNotEnabled')
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
