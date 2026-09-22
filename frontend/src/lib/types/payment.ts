import type { BadgeTone } from '$lib/components/ui/badgeTone';
import type { MessageKey } from '$lib/i18n/messages';
import type { MoneyAmount } from '$lib/utils/money';

// Mirrors the statuses the backend actually persists on `payments.status`.
// `pending_compliance` is the parking state the sanctions/KYC gate
// (`services/compliance.check_payment_compliance`) leaves a payment in — it
// must be listed here or the History badge renders blank (no label) and the
// status has no filter chip, which is how a held payment stayed invisible.
// Its two exits are `POST /api/payments/{id}/compliance/{release,dismiss}`.
export type PaymentStatus =
	| 'pending'
	| 'pending_compliance'
	| 'submitted'
	| 'processing'
	| 'completed'
	| 'failed'
	| 'cancelled'
	| 'voided';

export const PAYMENT_STATUSES: PaymentStatus[] = [
	'pending',
	'pending_compliance',
	'submitted',
	'processing',
	'completed',
	'failed',
	'cancelled',
	'voided'
];

/**
 * The i18n key carrying each status label — never the English string itself.
 *
 * Both surfaces that render a payment status (the `/payments` History badge
 * and its filter chips) are inside the i18n extraction slice, so a hardcoded
 * English map here put a translated chip row directly beside an untranslated
 * `Compliance Hold` badge. Keyed the same way
 * `notification.ts::EVENT_LABEL_KEYS` is; `Record<PaymentStatus, MessageKey>`
 * makes a new status a compile error rather than a blank badge, and
 * `paymentStatus.test.ts` proves every key exists in the catalogue.
 */
export const PAYMENT_STATUS_LABEL_KEYS: Record<PaymentStatus, MessageKey> = {
	pending: 'payments.status.pending',
	pending_compliance: 'payments.status.pendingCompliance',
	submitted: 'payments.status.submitted',
	processing: 'payments.status.processing',
	completed: 'payments.status.completed',
	failed: 'payments.status.failed',
	cancelled: 'payments.status.cancelled',
	voided: 'payments.status.voided'
};

/**
 * The message key for a status, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank badge). `Payment.status` is typed against the union, but a status the
 * backend adds first still reaches the badge as a bare string.
 */
export function paymentStatusLabelKey(status: string): MessageKey | null {
	return PAYMENT_STATUS_LABEL_KEYS[status as PaymentStatus] ?? null;
}

/**
 * Badge tone per payment status.
 *
 * Lives here rather than on `/payments` because the same union is badged in
 * more than one place — the History table, the queue, and `RunDetailModal`'s
 * per-payment column — and the two used to disagree: the modal tinted
 * `pending` amber but had no rule at all for `submitted`, `cancelled`,
 * `voided` or `pending_compliance`, so half the union rendered untinted there
 * while `/payments` painted it. That is the drift a shared map exists to stop
 * (`frontend/CLAUDE.md` § Badge).
 *
 * `Record<PaymentStatus, …>` on purpose: the old per-status CSS rules were a
 * list you had to remember to extend, and `voided` — a real member of
 * `PAYMENT_STATUSES` — never got one, so a voided payment rendered an
 * untinted pill. A total record makes the compiler ask the question.
 *
 * `cancelled` and `voided` share `muted` (a grey tint) while a run's `draft`
 * is `neutral` (flat): "abandoned" and "reversed" are both inert money states,
 * whereas draft is money that has not been attempted yet.
 *
 * No `?? 'neutral'` at the call sites, unlike {@link RUN_STATUS_TONES} below:
 * this record is total over the union, and a status off the union lands on
 * `Badge`'s own `tone` default — which is `neutral` — rather than on a
 * fallback restating it.
 */
export const PAYMENT_STATUS_TONES: Record<PaymentStatus, BadgeTone> = {
	pending: 'warning',
	// Same tone as `pending` — both are waiting. What separates "a human must
	// clear this" from "waiting its turn" is the ring the History cell draws
	// around it (`/payments` `.compliance-ring`), a caller-owned wrapper
	// rather than a sixth tone — decisions.md §52.
	pending_compliance: 'warning',
	submitted: 'accent',
	processing: 'accent',
	completed: 'success',
	failed: 'danger',
	cancelled: 'muted',
	voided: 'muted'
};

/**
 * The statuses a payment RUN can report — `services/payment_runs`' three CLAIM
 * states (`draft` / `executing` / `cancelled`, passed through untouched) plus
 * the four `PaymentRunRollup.run_status` derives from the run's own payments
 * (`submitted` / `completed` / `partial` / `failed`). Mirrors
 * `backend/app/services/payment_runs.py`; decisions.md §41.
 *
 * `PaymentRun.status` stays a bare `string` on the wire — the value is derived
 * on read, so a future rollup rung reaches the UI before this union does. The
 * union exists to make the two maps below TOTAL over what the backend can
 * report today: `partial` and `executing` had no tone rule at all and rendered
 * untinted, and neither had a label.
 */
export type PaymentRunStatus =
	| 'draft'
	| 'executing'
	| 'submitted'
	| 'completed'
	| 'partial'
	| 'failed'
	| 'cancelled';

export const PAYMENT_RUN_STATUSES: PaymentRunStatus[] = [
	'draft',
	'executing',
	'submitted',
	'completed',
	'partial',
	'failed',
	'cancelled'
];

/**
 * Badge tone per payment-RUN status — `services/payment_runs`' three claim
 * states (`draft` / `executing` / `cancelled`) plus the four its rollup
 * derives (decisions.md §41).
 *
 * Total over {@link PaymentRunStatus}, so every status the backend can report
 * today is tinted. `PaymentRun.status` is still a bare string on the wire (the
 * backend derives it on read), so read it through {@link runStatusTone}, which
 * falls an unknown value back to the flat `neutral` chip rather than to
 * nothing. `partial` and `executing` had no rule at all on either surface and
 * rendered untinted — `partial` especially, which is the one run status
 * meaning "some of this money failed".
 *
 * `draft` is `neutral`, not amber: `RunDetailModal` tinted it `warning` while
 * `/payments` rendered it flat, for the same run, one click apart. Flat wins —
 * a draft run is money that has not been attempted yet, which is the absence
 * of a signal rather than a weak one.
 */
export const RUN_STATUS_TONES: Record<PaymentRunStatus, BadgeTone> = {
	draft: 'neutral',
	executing: 'accent',
	submitted: 'accent',
	completed: 'success',
	partial: 'warning',
	failed: 'danger',
	cancelled: 'muted'
};

export function runStatusTone(status: string): BadgeTone {
	return RUN_STATUS_TONES[status as PaymentRunStatus] ?? 'neutral';
}

/**
 * The i18n key carrying each run status' label — never the English string
 * itself.
 *
 * `PaymentRun.status` had no label map at all, so both surfaces that badge it
 * — the `/payments` Runs table and `RunDetailModal`'s header — printed the RAW
 * enum, one cell away from a per-payment badge that round 24 had keyed. A
 * German user read `executing` in Latin script inside an otherwise translated
 * dialog.
 *
 * Keyed off the same union as {@link RUN_STATUS_TONES}, so a status with a
 * tone but no label (a coloured pill showing a raw value) is a compile error;
 * `paymentStatus.test.ts` proves every key exists in the catalogue.
 */
export const RUN_STATUS_LABEL_KEYS: Record<PaymentRunStatus, MessageKey> = {
	draft: 'paymentRuns.status.draft',
	executing: 'paymentRuns.status.executing',
	submitted: 'paymentRuns.status.submitted',
	completed: 'paymentRuns.status.completed',
	partial: 'paymentRuns.status.partial',
	failed: 'paymentRuns.status.failed',
	cancelled: 'paymentRuns.status.cancelled'
};

/**
 * The message key for a run status, or `null` for one this frontend doesn't
 * know (the caller renders the raw value — visible and searchable — rather
 * than a blank badge). `PaymentRun.status` is a bare string on the wire: the
 * backend DERIVES it on read, so a rollup rung this build has never seen
 * arrives as ordinary text.
 */
export function runStatusLabelKey(status: string): MessageKey | null {
	return RUN_STATUS_LABEL_KEYS[status as PaymentRunStatus] ?? null;
}

// UK domestic bank rails (bacs / faster_payments / chaps). A same-currency
// GBP payment to a GB vendor stays inside the UK banking system (sort code +
// account number — no IBAN, no SWIFT, no FX); the backend corridor selector
// (`payment_corridor.pick_corridor`) auto-selects `faster_payments` and honours
// an explicit `bacs` / `chaps` override. See issue #328.
export type PaymentMethod =
	| 'ach'
	| 'wire'
	| 'check'
	| 'virtual_card'
	| 'bacs'
	| 'faster_payments'
	| 'chaps';

export const PAYMENT_METHODS: PaymentMethod[] = [
	'ach',
	'wire',
	'check',
	'virtual_card',
	'bacs',
	'faster_payments',
	'chaps'
];

/**
 * The i18n key carrying each rail's label — never the English string itself.
 * The rail is a column on the (extracted) `/payments` History table and in
 * `RunDetailModal`, one cell from the now-translated status badge.
 *
 * Several rails are proper NAMES rather than words — `ACH`, `BACS`, `Faster
 * Payments`, `CHAPS` are scheme names and their catalogue entries are the
 * same string in all six locales. That is deliberate: routing them through a
 * key still costs nothing, and it means the day a locale does want to
 * transliterate one, there is a place to put it.
 */
export const PAYMENT_METHOD_LABEL_KEYS: Record<PaymentMethod, MessageKey> = {
	ach: 'payments.method.ach',
	wire: 'payments.method.wire',
	check: 'payments.method.check',
	virtual_card: 'payments.method.virtualCard',
	bacs: 'payments.method.bacs',
	faster_payments: 'payments.method.fasterPayments',
	chaps: 'payments.method.chaps'
};

/**
 * The message key for a rail, or `null` for one this frontend doesn't know
 * (the caller renders the raw value — visible and searchable — rather than a
 * blank cell). `Payment.method` is nullable on the wire and the backend can
 * add a rail before this union does.
 */
export function paymentMethodLabelKey(method: string): MessageKey | null {
	return PAYMENT_METHOD_LABEL_KEYS[method as PaymentMethod] ?? null;
}

export interface Payment {
	id: string;
	correlation_id: string | null;
	invoice_id: string;
	payment_run_id: string | null;
	/** `schemas/payment.py::PaymentResponse.amount` is `MoneyAmount` — a JSON number on the wire. */
	amount: MoneyAmount;
	method: PaymentMethod | null;
	status: PaymentStatus;
	reference: string | null;
	created_at: string;
	updated_at: string | null;
	/**
	 * What the PROCESSOR says it settled, beside `amount` — what AP AUTHORIZED
	 * (`schemas/payment.py::PaymentResponse`, migration 0083). Both have been on
	 * the read surface since the settlement-verification work; nothing rendered
	 * them, which is how an invoice held at `payment_scheduled` for an
	 * under-settlement was invisible in the app.
	 *
	 * `null` means no rail ever reported a figure — NOT zero. The backend fails
	 * OPEN on that case (`payment_settlement.settlement_coverage`), so absence
	 * is never evidence of a shortfall. Whether a reported figure actually
	 * covers the invoice is the SERVER's call: never subtract or compare these
	 * two client-side (frontend/CLAUDE.md § Money formatting).
	 */
	settled_amount?: MoneyAmount;
	settled_currency?: string | null;
	/**
	 * What `amount` — the AUTHORIZED figure — is denominated in
	 * (`schemas/payment.py::PaymentResponse.currency`), off the invoice row the
	 * response already joins. `payments` has no currency column; a payment
	 * settles in its invoice's currency.
	 *
	 * Without it, six `/payments` call sites rendered the authorized amount
	 * under the ORG's default code. The sharpest was the Accept-settlement
	 * dialog, whose whole job is to put "Authorized" beside "Settled": the
	 * settled half had `settled_currency` and the authorized half had nothing,
	 * so a EUR payment showed a fabricated `$1,200.00` above a real `€1,150.00`
	 * on the screen built to catch a `currency_mismatch`.
	 *
	 * `null` means the server could not establish it — render the bare figure,
	 * never a substituted default (`docs/decisions.md` §79/§82).
	 */
	currency?: string | null;
	vendor_name: string | null;
	invoice_number: string | null;
	card_last_four: string | null;
	card_provider: string | null;
	card_id: string | null;
	/**
	 * Set ONLY by `POST /{id}/void` and its card-close retry
	 * (`schemas/payment.py::PaymentResponse`). `void_card_outcome` is the
	 * fine-grained provider tag; `void_card_disposition` is the VERDICT the
	 * server derived from it (`services/card_issuance.card_cancel_disposition`).
	 *
	 * **Branch on the disposition, never on the tag.** A client enumerating
	 * outcome strings mis-reads every tag added later — and it fails in the
	 * dangerous direction, rendering a live, bearer-spendable card as closed.
	 *
	 * `null` on every non-void read: the leg never ran, and "we never asked" is
	 * not "it is shut" (`docs/decisions.md` §34).
	 */
	void_card_outcome?: string | null;
	void_card_disposition?: VoidCardDisposition | null;
	void_adapter_outcome?: string | null;
}

/**
 * The verdict on a voided card payment's card, from
 * `services/card_issuance.CardCancelDisposition`.
 *
 * - `closed` — dead at the provider. Nothing to do.
 * - `no_card` — this payment never issued a card.
 * - `not_closed_final` — already CHARGED, so it can never be closed; a retry
 *   cannot help and must not be offered.
 * - `not_closed_retryable` — still LIVE and spendable. Retry the close via
 *   `POST /api/payments/{id}/void/retry-card-cancel`.
 */
export type VoidCardDisposition =
	| 'closed'
	| 'no_card'
	| 'not_closed_final'
	| 'not_closed_retryable';

/**
 * Human label for a card-cancel outcome tag, or `null` when the tag is one this
 * build doesn't know — the caller then renders the raw value, which is PII-free
 * by construction (`card_cancel_error:*` carries an exception TYPE, never the
 * provider's body). Same shape as `paymentMethodLabelKey` above, and for the
 * same reason: the backend can add a tag before this map does.
 */
export function voidCardOutcomeLabelKey(outcome: string): MessageKey | null {
	if (outcome.startsWith('card_cancel_error:')) return 'payments.void.card.outcome.error';
	switch (outcome) {
		case 'card_cancelled':
		case 'cancelled':
			return 'payments.void.card.outcome.cancelled';
		case 'card_already_cancelled':
			return 'payments.void.card.outcome.alreadyCancelled';
		case 'card_already_charged':
			return 'payments.void.card.outcome.alreadyCharged';
		case 'card_cancel_rejected':
			return 'payments.void.card.outcome.rejected';
		case 'cards_not_configured':
			return 'payments.void.card.outcome.cardsOff';
		case 'card_provider_not_configured':
			return 'payments.void.card.outcome.providerUnknown';
		default:
			return null;
	}
}

export interface PaymentRun {
	id: string;
	status: string;
	total_amount: MoneyAmount;
	/**
	 * What `total_amount` is denominated in
	 * (`schemas/payment.py::PaymentRunResponse.currency`). `payment_runs` has no
	 * currency column either — the total is one bare `Numeric`, kept meaningful
	 * by `create_payment_run_for_invoices` refusing a run whose invoices span
	 * more than one currency.
	 *
	 * `null` when the server could not PROVE one: a run with no payments, or a
	 * legacy run predating that guard whose legs disagree — in which case the
	 * total is denominated in nothing real and a code would be worse than none.
	 * Render the bare figure (`docs/decisions.md` §79/§82).
	 */
	currency?: string | null;
	initiated_by: string | null;
	executed_at: string | null;
	created_at: string;
	payment_count: number;
}
