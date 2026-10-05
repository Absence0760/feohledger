/**
 * Validating a typed money amount WITHOUT routing it through `Number`.
 *
 * The counterpart to `utils/money.ts`, which formats an amount for display.
 * This one prepares an amount the user typed for the WIRE, and the whole point
 * is what it refuses to do: a money value bound for the backend must reach it
 * as the exact decimal string it was typed as, never as a JSON number.
 *
 * `json.loads` on the server decodes the body before any validator runs, so a
 * JSON number with a fractional part is a float by the time the API sees it —
 * the rounding has already happened and no server-side annotation can undo it
 * (pydantic yields `Decimal('100')` from `100.00000000000000001`). The
 * `/api/discounts/optimize` cash budget went out as `Number(input)` for exactly
 * this reason, and that budget decides which invoices get paid early: a spend
 * decision, not a display value. Root `CLAUDE.md` § Project invariants.
 *
 * `Number` is therefore absent from this module by design — including as a
 * validity check, so no later edit can quietly promote the check into the
 * value. Shape is decided by a regex on the text; the string that goes out is
 * the string that came in.
 *
 * Pure — no `$state`, no `fetch` — so it sits in `utils/` beside `money.ts` and
 * is unit-tested under the plain-Node vitest config.
 */

/**
 * A non-negative decimal amount: digits, optionally a fractional part.
 *
 * Deliberately narrow. No thousands separators (ambiguous across the locales
 * this app ships), no currency symbols, no exponent, no sign. Every caller
 * sends a non-negative decimal the backend also constrains to `>= 0`: the
 * optimizer's cash budget, a vendor-statement line amount, and a
 * bulk-negotiation tier percent (which layers its own `0 < p < 100` bounds on
 * top — see `#lib/types/discounts::normalizeTierPercent`). Digits are
 * bounded so a paste of a thousand characters is refused rather than sent.
 *
 * The fractional bound is generous on purpose. A budget beyond 2dp is not
 * meaningful money, but the backend parses it exactly, and a client that
 * silently refused input the server accepts would be re-introducing a
 * narrower version of the rounding this module exists to stop. Anything
 * inside the bound is passed through untouched.
 */
const DECIMAL_INPUT = /^\d{1,15}(\.\d{1,15})?$/;

/**
 * The exact decimal string to send, or `null` when `raw` is not a money amount.
 *
 * Blank input is `null` too — for the cash budget that legitimately means "no
 * budget", so a caller distinguishes the two by testing the trimmed text
 * itself. `null` must never be quietly forwarded as "no constraint" for input
 * that WAS typed: that is the same silent-unconstrained failure the backend's
 * `extra="forbid"` closes on its side.
 *
 * A leading `+` and a trailing `.` are rejected rather than repaired — this
 * normalises whitespace only, so what is sent is what was read.
 */
export function normalizeMoneyInput(raw: string | null | undefined): string | null {
	const text = (raw ?? '').trim();
	if (!DECIMAL_INPUT.test(text)) return null;
	return text;
}

/** Whether `raw` is a well-formed money amount. Blank counts as not-an-amount. */
export function isMoneyInput(raw: string | null | undefined): boolean {
	return normalizeMoneyInput(raw) !== null;
}
