import { describe, expect, it } from 'vitest';

import {
	countByFile,
	findMoneyShapedNumberFields,
	isMoneyShapedName
} from './moneyTypeAudit';

import type { Budget, BudgetSpend } from './budget';
import type { CatalogItem } from './catalog';
import type { ContractSpend } from './contract';
import type { Expense } from './expense';
import type { IntakeRequest } from './intake';
import type { Invoice } from './invoice';
import type { Payment } from './payment';
import type { PositivePayFile } from './positivePay';
import type { RecurringOccurrence, RecurringTemplate } from './recurring';
import type { ReconLine } from './vendorStatementRecon';
import type { ApprovalStepConfig } from './workflow';

/**
 * Repo-wide ratchet on the money-typing conversion of `src/lib/types/`.
 *
 * `frontend/CLAUDE.md` § Money formatting forbids typing an API money field
 * `number`: it silently invites `.toFixed()`, `a - b` and `Math.max()` on
 * currency. Typed `MoneyAmount` / `MoneyString` the same expression is a
 * *compile error*, which must then be resolved with `parseMoneyForLayout`
 * (geometry) or `isPositiveAmount` / `isNegativeAmount` (a predicate) —
 * **never an `as` cast**, which would put the hazard straight back.
 *
 * This cannot land as one sweep, and that is the whole reason it is a ratchet:
 * a scan for money-shaped names returns ~105 fields of which most are NOT money
 * (`total` is a pagination row count on every list envelope, `budget` and
 * `remaining` are *token* counts on the assistant meter, `limit` is a report
 * row cap). There is no way to tell them apart by name, so each needs a
 * judgment and each genuine money field needs its call sites resolved. So it:
 *
 *   - **cannot be satisfied by adding.** A new `number`-typed money-shaped
 *     field fails on the file it landed in, by name, with the line number.
 *   - **records the conversion as it happens.** Landing a module means editing
 *     its number down here — or moving it into `CONVERTED` — in the same
 *     commit, which is also the only place the remaining work is counted.
 *   - **holds the finished modules at zero.** A regression in a converted
 *     module is a plain failure, not a loosened budget.
 *
 * The type-level half of the guarantee is at the bottom of this file: a set of
 * `@ts-expect-error` probes that FAIL `pnpm check` if arithmetic on any of the
 * converted modules' money fields ever starts compiling again.
 */

const RAW = import.meta.glob('/src/lib/types/*.ts', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const sources = Object.entries(RAW)
	.map(([path, source]) => [path.replace(/^\/src\//, ''), source] as const)
	.filter(([path]) => !path.endsWith('.test.ts') && !path.endsWith('moneyTypeAudit.ts'))
	.sort(([a], [b]) => a.localeCompare(b));

/**
 * The per-field judgments: money-SHAPED names that are not money.
 *
 * Keyed `file` → `Interface.field` → why. Every entry is a claim a reviewer can
 * check against the backend schema, which is the point — the alternative is a
 * name-based guess nobody can audit. A file may only enter {@link CONVERTED}
 * once every candidate in it is either retyped or judged here.
 */
const JUDGED_NOT_MONEY: Record<string, Record<string, string>> = {
	'lib/types/bankReconciliation.ts': {
		// Pagination row count of the whole statement set, not money.
		'BankStatementListResponse.total': 'pagination row count'
	},
	'lib/types/erpConnection.ts': {
		// Blackbaud SKY's daily API-call allowance (decisions §322 / §324).
		'ErpCallUsage.limit': 'an ERP API call count, not money'
	},
	'lib/types/accessReview.ts': {
		'AccessReviewResponse.total': 'users reviewed — a head count, not money'
	},
	'lib/types/analytics.ts': {
		// The dashboard's invoice ROW COUNT — `total_invoices` is money-shaped by
		// name only (`total_`). The money figure it sits beside is
		// `total_amount`, typed `MoneyAmount`, and so are the five
		// `AgingBuckets` bands.
		'DashboardData.total_invoices': 'invoice row count of the whole tenant, not money'
	},
	'lib/types/assistant.ts': {
		'ConversationListResponse.total': 'conversation row count',
		// `schemas/assistant.py::UsageResponse` types both `int`, beside
		// input_tokens / output_tokens / total_tokens. The assistant meter is
		// denominated in TOKENS; the money that meter later prices lives on the
		// billing surface, not here.
		'UsageResponse.budget': 'the per-org monthly TOKEN allowance, not a cash budget',
		'UsageResponse.remaining': 'tokens left in that allowance',
		'InvoiceListResult.total': 'row count of the assistant tool result',
		'PendingApprovalsResult.total': 'row count of the assistant tool result'
	},
	'lib/types/cardRebate.ts': {
		// Deliberately renamed apart from the money it used to be: the envelope's
		// `total` is now the pagination row COUNT (the canonical list shape), and
		// the summed rebate amount lives beside it as `total_amount`, typed
		// `MoneyAmount`.
		'RebateListResponse.total': 'pagination row count of the whole filtered set'
	},
	'lib/types/catalog.ts': {
		'CatalogListResponse.total': 'pagination row count of the whole filtered set'
	},
	'lib/types/contract.ts': {
		'ContractListResponse.total': 'pagination row count of the whole filtered set'
	},
	'lib/types/discounts.ts': {
		'DiscountOfferPage.total': 'pagination row count of the whole filtered set'
	},
	'lib/types/expense.ts': {
		'ExpenseListResponse.total': 'pagination row count of the whole filtered set',
		// `ExpenseSummaryResponse.total` is `int`, beside the per-currency money
		// in `by_currency` — which is grouped, never summed. The KPI money on
		// this surface is `ExpenseCurrencyTotal.total`, already `MoneyString`.
		'ExpenseSummary.total': 'expense row count of the whole filtered set',
		'ExpenseReportListResponse.total': 'pagination row count of the whole filtered set',
		'CardTransactionListResponse.total': 'pagination row count of the whole filtered set'
	},
	'lib/types/requisition.ts': {
		'RequisitionListResponse.total': 'pagination row count of the whole filtered set',
		'RequisitionSummary.total': 'requisition row count of the whole filtered set'
	},
	'lib/types/vendorStatementRecon.ts': {
		'ReconciliationListResponse.total': 'pagination row count of the whole filtered set',
		'ReconciliationSummary.total': 'reconciliation-run row count of the whole filtered set'
	},
	'lib/types/workflow.ts': {
		// `schemas/workflow.py` types it `float = Field(ge=0.0, le=1.0)` and the
		// builder renders it as `Math.round(x * 100)%` on a slider: the
		// EXTRACTION CONFIDENCE an invoice must clear to auto-approve. The
		// money threshold that gates a big invoice is `require_cfo_above`
		// beside it, which IS `MoneyString`.
		'ExtractionStepConfig.auto_approve_threshold':
			'0..1 extraction-confidence ratio, not an amount',
		// A condition rule's operand, discriminated by its sibling `field`: a
		// money amount only when `field === "amount"`, otherwise a currency
		// code / vendor id / GL account / cost-centre / department string. The
		// union already carries `string | string[]`, so arithmetic on it is
		// ALREADY a type error — the hazard the ratchet exists to catch cannot
		// occur here, and naming the whole operand `MoneyAmount` would misread
		// the five non-money arms.
		'ConditionRule.value': 'a per-field rule operand, money only on the `amount` field'
	},
	'lib/types/budget.ts': {
		'BudgetListResponse.total': 'pagination row count of the whole filtered set',
		// The allocation totals are on `BudgetSummary.by_currency`, grouped per
		// currency and typed `MoneyString` — this sits beside them and counts rows.
		'BudgetSummary.total': 'budget row count of the whole filtered set'
	},
	'lib/types/exceptionAgents.ts': {
		'AgentDecisionList.total': 'decision row count',
		'AgentStats.total_decisions': 'decisions the agents took — a count'
	},
	'lib/types/intake.ts': {
		'IntakeListResponse.total': 'pagination row count of the whole filtered set',
		'IntakeSummary.total': 'intake row count of the whole filtered set'
	},
	'lib/types/notification.ts': {
		'NotificationListResponse.total': 'notification row count'
	},
	'lib/types/positivePay.ts': {
		'PositivePayListResponse.total': 'pagination row count of the whole filtered set',
		'PositivePaySummary.total': 'file row count of the whole filtered set'
	},
	'lib/types/privacy.ts': {
		'DataSubjectRequestList.total': 'DSAR / erasure request row count'
	},
	'lib/types/recurring.ts': {
		'RecurringListResponse.total': 'pagination row count of the whole filtered set',
		'RecurringTemplateSummary.total': 'template row count of the whole filtered set',
		'RecurringHistory.total': 'generated-invoice row count for one template'
	},
	'lib/types/reports.ts': {
		// `ReportSpec.limit` is the report builder's hard ROW cap, independent of
		// pagination — nothing to do with a spend limit.
		'ReportSpec.limit': 'a row cap on the report result, not a spend limit'
	},
	'lib/types/vendor.ts': {
		'VendorStatusCounts.total': 'vendor row count behind the status chips',
		'ScreeningReviewQueueResponse.total': 'pagination row count of the review queue',
		'VendorMergeResponse.total_reassigned': 'rows re-pointed by the merge — a count',
		'VendorChangeRequestPage.total': 'pagination row count',
		'VendorChangeRequestCounts.total': 'change-request row count'
	}
};

/**
 * Modules whose every money field is typed `MoneyAmount` / `MoneyString`.
 *
 * A candidate reappearing here is a regression, not a budget to raise. Seven
 * more landed this round — `payment`, `workflow`, `catalog`, `invoice`,
 * `vendorStatementRecon`, `contract` and `expense` — leaving `discounts` and
 * `requisition` in {@link BASELINE} at one field each, for the reason both
 * carry in their own doc comment: the page SCALES the amount by a
 * non-money factor (a tier percent; a line quantity) to preview a figure the
 * server owns, and `utils/money.ts` exposes no exact multiply — only
 * `sumMoney`. Retyping without one would force a cast, or
 * `parseMoneyForLayout` used outside its stated geometry-only contract.
 */
const CONVERTED = [
	'lib/types/accessReview.ts',
	'lib/types/assistant.ts',
	'lib/types/budget.ts',
	'lib/types/catalog.ts',
	'lib/types/contract.ts',
	'lib/types/discounts.ts',
	'lib/types/exceptionAgents.ts',
	'lib/types/expense.ts',
	'lib/types/intake.ts',
	'lib/types/invoice.ts',
	'lib/types/notification.ts',
	'lib/types/payment.ts',
	'lib/types/positivePay.ts',
	'lib/types/privacy.ts',
	'lib/types/recurring.ts',
	'lib/types/reports.ts',
	'lib/types/requisition.ts',
	'lib/types/vendor.ts',
	'lib/types/vendorStatementRecon.ts',
	'lib/types/workflow.ts'
];

/**
 * `number`-typed money-shaped fields still awaiting a judgment, per module.
 *
 * **Only ever edit a number DOWNWARD** (or move the file into `CONVERTED` at
 * zero). A module missing from this map and from `CONVERTED` must have none —
 * which is how a brand-new type module is held to the rule from its first
 * commit.
 */
const BASELINE: Record<string, number> = {
	// EMPTY — every module under `src/lib/types/` is converted. The map stays
	// so a module that legitimately needs a staged conversion has somewhere to
	// land, but adding an entry now is a REGRESSION, not a plan: an unconverted
	// module fails `no module grows a new number-typed money field` first.
};

const candidates = findMoneyShapedNumberFields(sources);
const unjudged = candidates.filter((c) => JUDGED_NOT_MONEY[c.file]?.[c.key] === undefined);
const counts = countByFile(unjudged);

describe('money-typing ratchet over src/lib/types', () => {
	it('scans a non-trivial set of type modules', () => {
		// A glob that silently matched nothing would make every assertion below
		// pass vacuously.
		expect(sources.length).toBeGreaterThan(30);
	});

	it('detects the shape it is meant to detect', () => {
		// The audit's own regression test. Without it, breaking the parser turns
		// this whole suite green — and now that no real module carries a
		// `number`-typed money field, there is nothing left in the repo to point
		// at, so the probe is synthetic. It must stay synthetic: pointing it at
		// a real module again would mean the ratchet had slipped.
		const planted = findMoneyShapedNumberFields([
			[
				'lib/types/__probe__.ts',
				'export interface Probe {\n\tamount: number;\n\tlabel: string;\n}'
			]
		]);
		expect(planted.map((c) => c.key)).toEqual(['Probe.amount']);
	});

	it.each(CONVERTED)('%s carries no number-typed money field', (path) => {
		const remaining = unjudged
			.filter((c) => c.file === path)
			.map((c) => `${c.key}: ${c.type} (line ${c.line})`);
		expect(
			remaining,
			`${path} is a CONVERTED module: every money field there is typed ` +
				'`MoneyAmount` / `MoneyString`. A new `number`-typed field is either ' +
				'money (retype it, and resolve the arithmetic with parseMoneyForLayout / ' +
				'isPositiveAmount — never an `as` cast) or it is not (add it to ' +
				'JUDGED_NOT_MONEY with the reason).'
		).toEqual([]);
	});

	it('no module grows a new number-typed money field', () => {
		const regressions = Object.entries(counts)
			.filter(([path, n]) => n > (BASELINE[path] ?? 0))
			.map(([path, n]) => `${path}: ${n} (baseline ${BASELINE[path] ?? 0})`);

		expect(
			regressions,
			'A money field typed `number` invites `a - b` / `Math.max()` on ' +
				'currency and the type system will not stop it. Type it `MoneyAmount` ' +
				'(endpoints that serialise a JSON number) or `MoneyString` (exact ' +
				'decimal strings). If the field is NOT money — a row count, a ' +
				'percentage, a token budget — record that judgment in ' +
				'JUDGED_NOT_MONEY rather than raising the baseline.'
		).toEqual([]);
	});

	it('the baseline names no module that is already clean', () => {
		// Keeps the map honest as modules land: a stale entry would quietly
		// re-authorise a `number`-typed money field in a converted module.
		const stale = Object.keys(BASELINE).filter((path) => (counts[path] ?? 0) === 0);
		expect(stale, 'converted — move these to CONVERTED and drop them here').toEqual([]);
	});

	it('every judgment still names a real field', () => {
		// A judgment left behind after the field was renamed or retyped would
		// silently pre-authorise a future field of the same name.
		const live = new Set(candidates.map((c) => `${c.file}::${c.key}`));
		const stale = Object.entries(JUDGED_NOT_MONEY).flatMap(([file, keys]) =>
			Object.keys(keys)
				.filter((key) => !live.has(`${file}::${key}`))
				.map((key) => `${file}::${key}`)
		);
		expect(stale, 'field is gone or no longer number-typed — drop the judgment').toEqual([]);
	});

	it('a converted module fails the moment a number-typed money field lands', () => {
		// Proves the guard rather than assuming it: this is exactly the diff the
		// ratchet exists to reject, run through the same scanner.
		const planted = findMoneyShapedNumberFields([
			[
				'lib/types/budget.ts',
				['export interface Budget {', '\tamount: number;', '}'].join('\n')
			]
		]);
		expect(planted).toHaveLength(1);
		expect(planted[0].key).toBe('Budget.amount');
		// ...and it is NOT covered by any existing judgment, so it reaches the
		// CONVERTED assertion above as a failure.
		expect(JUDGED_NOT_MONEY['lib/types/budget.ts']['Budget.amount']).toBeUndefined();
	});
});

describe('money-shaped name classification', () => {
	it('flags names that could denominate currency', () => {
		for (const name of [
			'amount',
			'total_amount',
			'unit_price',
			'spend_limit',
			'remaining_after',
			'projected_savings'
		]) {
			expect(isMoneyShapedName(name), name).toBe(true);
		}
	});

	it('does not flag counts, ratios or identifiers', () => {
		// The ~40 non-money fields a blanket sweep would have failed on.
		for (const name of [
			'total_count',
			'unconverted_count',
			'total_tokens',
			'total_requests',
			'total_rows',
			'amount_mismatches',
			'amount_variance_pct',
			'exception_count',
			'page_size',
			'invoice_id'
		]) {
			expect(isMoneyShapedName(name), name).toBe(false);
		}
	});

	it('ignores a field that is not number-typed', () => {
		const found = findMoneyShapedNumberFields([
			['lib/types/probe.ts', 'export interface X {\n\tamount: MoneyAmount;\n}']
		]);
		expect(found).toEqual([]);
	});

	it('catches a money union re-spelled inline instead of aliased', () => {
		// `MoneyAmount` is `string | number | null | undefined` — but it is the
		// NAME that makes arithmetic a type error at the call site, and an
		// inline re-spelling reads as deliberate while behaving identically.
		const found = findMoneyShapedNumberFields([
			['lib/types/probe.ts', 'export interface X {\n\tamount: string | number | null;\n}']
		]);
		expect(found.map((c) => c.key)).toEqual(['X.amount']);
	});
});

// ---------------------------------------------------------------------------
// Type-level guarantee.
//
// The scan above is a source check; THIS is the property it exists to protect.
// Every line below must be a compile error — `@ts-expect-error` fails the build
// if it is not — so retyping any of these fields back to `number` turns
// `pnpm check` red. Nothing here runs: the function is never called, and the
// imports are type-only.
// ---------------------------------------------------------------------------

function moneyArithmeticMustNotCompile(
	budget: Budget,
	spend: BudgetSpend,
	intake: IntakeRequest,
	file: PositivePayFile,
	template: RecurringTemplate,
	occurrence: RecurringOccurrence,
	payment: Payment,
	approval: ApprovalStepConfig,
	item: CatalogItem,
	invoice: Invoice,
	reconLine: ReconLine,
	contractSpend: ContractSpend,
	expense: Expense
) {
	// @ts-expect-error money is not a number — use `parseMoneyForLayout` for geometry.
	const scaled = budget.amount * 2;
	// @ts-expect-error never subtract two amounts client-side — read the backend's own figure.
	const headroom = spend.allocated - spend.committed;
	// @ts-expect-error a money comparison is `isNegativeAmount`, not `< 0`.
	const overspent = spend.remaining < 0;
	// @ts-expect-error a chart scale goes through `parseMoneyForLayout`.
	const scale = Math.max(intake.estimated_amount, 0);
	// @ts-expect-error money never round-trips through `.toFixed()`.
	const rendered = file.total_amount.toFixed(2);
	// @ts-expect-error cadence normalisation is the server's, in exact Decimal.
	const monthly = template.amount / 12;
	// @ts-expect-error summing amounts client-side is `sumMoney`, not `+`.
	const twoRuns = occurrence.amount + occurrence.amount;
	// @ts-expect-error a payment amount is money, whatever the wire shape.
	const doubled = payment.amount * 2;
	// @ts-expect-error the CFO gate is an exact decimal string, not a number to compare.
	const overGate = approval.require_cfo_above > 1000;
	// @ts-expect-error extending a catalog price by a quantity is not float math.
	const extended = item.unit_price * 3;
	// @ts-expect-error money never round-trips through `.toFixed()`.
	const invoiceText = invoice.amount.toFixed(2);
	// @ts-expect-error the statement-vs-ledger delta is the server's own figure.
	const delta = reconLine.statement_amount - reconLine.ledger_amount;
	// @ts-expect-error headroom under a spend limit is `ContractSpend.remaining`.
	const headroomLeft = contractSpend.spend_limit - contractSpend.invoiced_total;
	// @ts-expect-error a chart scale goes through `parseMoneyForLayout`.
	const expenseScale = Math.max(expense.amount, 0);
	return {
		scaled,
		headroom,
		overspent,
		scale,
		rendered,
		monthly,
		twoRuns,
		doubled,
		overGate,
		extended,
		invoiceText,
		delta,
		headroomLeft,
		expenseScale
	};
}
