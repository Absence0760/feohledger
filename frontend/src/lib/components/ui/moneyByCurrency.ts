/**
 * One currency's figure in a set that must not be summed — the shape
 * `ui/MoneyByCurrency.svelte` renders.
 *
 * `currency: null` is a figure nobody recorded a currency for (a purchase order
 * created before `purchase_orders.currency` existed, with no requisition behind
 * it — `docs/decisions.md` §197). It is its own entry, rendered bare, and never
 * folded into a real currency's figure.
 *
 * **Why a `.ts` module and not the component's `<script module>`:** the same
 * reason as `badgeTone.ts` — `$lib/types/analytics.ts` types its payloads with
 * it, and e2e fixtures pin those payloads with `satisfies`, which `pnpm
 * check:e2e` resolves through the `*.svelte` shim that has no named exports.
 */
import type { MoneyAmount } from '$lib/utils/money';

export interface CurrencyFigure {
	currency: string | null;
	amount: MoneyAmount;
}
