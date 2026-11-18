import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

/**
 * Source guard: no spec deletes an invoice, a vendor, a workflow definition or
 * a payment run by hand, and no spec creates a payment run or a vendor without
 * removing it.
 *
 * `invoices` is referenced by 16 foreign keys and **none of them cascade**, so
 * a bare `DELETE FROM invoices WHERE …` only succeeds while that invoice
 * happens to have no children. Around twenty specs relied on that, and one of
 * them (`invoices/upload-refetch-failure`) started failing teardown the moment
 * extraction began succeeding and writing line items — the invoice grew a child
 * the spec's hand-maintained delete list didn't know about.
 *
 * `vendors` is the same trap one table over: 17 foreign keys, only two of them
 * cascading, and seventeen specs each carrying its own partial child list
 * (`vendors/import-csv` knew about `sanctions_checks`, most knew about
 * nothing).
 *
 * `workflow_definitions` is the trap's other shape: three non-cascading
 * references and ten workflow specs carrying a byte-identical copy of the same
 * walk, differing only in the marker swept by — correct today, and ten places
 * to miss the next child in. All three tables now have exactly one owner in
 * `fixtures/helpers.ts`.
 *
 * `payment_runs` joined them when seven specs turned out to carry private
 * copies of the same two-statement delete, and three of them a variant that
 * deleted the payments first and so never matched the run at all.
 *
 * Every call site goes through those owners; this guard is what stops the next
 * spec re-introducing the pattern, because the failure it causes is a teardown
 * error in an unrelated file weeks later.
 *
 * Detection is deliberately source-level rather than type-level: the SQL is a
 * template literal, so no compiler can see it. Comments are stripped first so
 * prose *about* the pattern (this file, and the helpers' own docstrings) doesn't
 * trip it.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const E2E_ROOT = join(HERE, '..');

/** The one file allowed to issue the statements — it is the implementation. */
const OWNER = join('fixtures', 'helpers.ts');

/** Files the scan skips, and why. Kept to exactly two: widening this set is
 *  how the pattern comes back. */
const EXEMPT = new Set([
	OWNER,
	// This file: its known-bad fixtures below are string literals, not comments,
	// so the detector correctly sees them.
	join('meta', 'teardown-guard.spec.ts')
]);

/** One guarded table: the statement to hunt for, the helper that owns it, how
 *  that helper is called, and why the hand-rolled version is a trap. The
 *  `signature` is spelled out because the three owners do NOT agree on it —
 *  `deleteWorkflowsWhere` takes a name prefix, not a WHERE clause body — and a
 *  failure message that guessed would send the next author down the wrong
 *  path. */
type GuardedTable = {
	readonly table: string;
	readonly helper: string;
	readonly signature: string;
	readonly why: string;
};

const GUARDED: readonly GuardedTable[] = [
	{
		table: 'invoices',
		helper: 'deleteInvoicesWhere',
		signature: 'deleteInvoicesWhere(predicate, slug?)',
		why:
			'`invoices` has 16 non-cascading foreign keys, so a bare ' +
			'`DELETE FROM invoices` only works until the invoice acquires a child ' +
			'the spec did not anticipate.'
	},
	{
		table: 'vendors',
		helper: 'deleteVendorsWhere',
		signature: 'deleteVendorsWhere(predicate, slug?)',
		why:
			'`vendors` has 17 foreign keys and only two of them cascade, so a bare ' +
			'`DELETE FROM vendors` only works until the vendor acquires a child ' +
			'the spec did not anticipate — a purchase order, a contract, a catalog, ' +
			'a sanctions check, a virtual card.'
	},
	{
		table: 'payment_runs',
		helper: 'deletePaymentRunsWhere',
		signature: 'deletePaymentRunsWhere(predicate, slug?)',
		why:
			'a run\'s payments carry children of their own (bank transactions, ' +
			'virtual cards, retries) and the run may carry a Positive Pay file, ' +
			'and the payments and the run have to go in one statement whenever ' +
			'the predicate is defined by the payments.'
	},
	{
		table: 'workflow_definitions',
		helper: 'deleteWorkflowsWhere',
		signature: 'deleteWorkflowsWhere(namePrefix, slug?)',
		why:
			'`workflow_definitions` has three non-cascading foreign keys — ' +
			'`workflow_versions`, `workflow_experiments` and `workflow_instances`, ' +
			'the last itself referenced by `workflow_steps` — so a bare ' +
			'`DELETE FROM workflow_definitions` only works until the definition ' +
			'acquires a child the spec did not anticipate, and the helper also ' +
			'carries the `is_default = false` seatbelt that keeps a marker typo ' +
			'away from the seeded default.'
	}
];

/** `DELETE FROM <table>`, in any casing, with any run of whitespace. */
function handRolledDelete(table: string): RegExp {
	return new RegExp(`delete\\s+from\\s+${table}\\b`, 'i');
}

/**
 * Strip `//` line and block comments so prose describing the anti-pattern is
 * not itself flagged. Crude by design — it can mangle a `//` inside a string
 * literal, which cannot produce a false NEGATIVE for the pattern we search for.
 */
export function stripComments(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/\/\/[^\n]*/g, ' ');
}

/** Does this file issue a hand-rolled delete against `table`? */
export function hasHandRolledDelete(source: string, table: string): boolean {
	return handRolledDelete(table).test(stripComments(source));
}

/**
 * The other half of the guard: a spec that CREATES a row must also remove it.
 * The leak this catches is quiet — `cash-flow/copilot` staged a draft run over
 * seeded invoices on every pass, and its live payments kept those invoices out
 * of the payment queue for whichever payments spec ran next.
 *
 * A create is a `.post(` straight to the collection endpoint (the URL literal
 * ends at the collection, optionally with a query string) or an `INSERT INTO`
 * the table. A run is also staged by the copilot's `/draft-run`. Creates made
 * through the UI, or through a URL held in a variable, are out of reach of a
 * source scan; this catches the API shape every existing creator uses.
 *
 * Removal is any call to an owner that reaches the row: `deleteInvoicesWhere`
 * counts for runs because it removes every run holding only its invoices'
 * payments, and `deleteVendorsWhere` because it delegates to it.
 */
type CreatedRow = {
	readonly table: string;
	readonly create: RegExp;
	readonly removers: readonly string[];
};

const QUOTE = '[`\'"]';
const NOT_QUOTE = '[^`\'"]';

function postTo(pathPattern: string): string {
	return `\\.post\\(\\s*${QUOTE}${NOT_QUOTE}*?${pathPattern}(?:\\?${NOT_QUOTE}*)?${QUOTE}`;
}

const CREATED: readonly CreatedRow[] = [
	{
		table: 'payment_runs',
		create: new RegExp(
			`${postTo('/api/payments/runs/?')}|${postTo('/draft-run')}|insert\\s+into\\s+payment_runs\\b`,
			'i'
		),
		removers: ['deletePaymentRunsWhere', 'deleteInvoicesWhere', 'deleteVendorsWhere']
	},
	{
		table: 'vendors',
		create: new RegExp(`${postTo('/api/(?:v1/)?vendors/?')}|insert\\s+into\\s+vendors\\b`, 'i'),
		removers: ['deleteVendorsWhere']
	}
];

/** Files that create a guarded row only in a request the server must REFUSE,
 *  so there is nothing to remove. Every entry says which request. */
const CREATES_NOTHING: Readonly<Record<string, string>> = {
	[join('auth', 'cross-tenant-writes.spec.ts')]:
		'its POST /api/vendors and POST /api/payments/runs carry another tenant\'s ' +
		'token and assert the 403'
};

/** Does this file create a `row` it never removes? */
export function createsWithoutRemoving(source: string, row: CreatedRow): boolean {
	const code = stripComments(source);
	if (!row.create.test(code)) return false;
	return !row.removers.some((helper) => code.includes(`${helper}(`));
}

function typescriptFiles(dir: string): string[] {
	const found: string[] = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		if (entry.name === 'node_modules' || entry.name === '.auth') continue;
		const full = join(dir, entry.name);
		if (entry.isDirectory()) found.push(...typescriptFiles(full));
		else if (entry.name.endsWith('.ts')) found.push(full);
	}
	return found;
}

test.describe('e2e teardown discipline', () => {
	for (const { table, helper, signature, why } of GUARDED) {
		test(`no spec deletes ${table} without going through ${helper}`, () => {
			const offenders = typescriptFiles(E2E_ROOT)
				.map((file) => relative(E2E_ROOT, file))
				.filter((file) => !EXEMPT.has(file))
				.filter((file) => hasHandRolledDelete(readFileSync(join(E2E_ROOT, file), 'utf8'), table));

			expect(
				offenders,
				`${why} Use \`${signature}\` from fixtures/helpers.ts, ` +
					'which owns the whole graph.'
			).toEqual([]);
		});
	}

	for (const row of CREATED) {
		test(`no spec creates ${row.table} without removing them`, () => {
			const offenders = typescriptFiles(E2E_ROOT)
				.map((file) => relative(E2E_ROOT, file))
				.filter((file) => !EXEMPT.has(file) && !(file in CREATES_NOTHING))
				.filter((file) => createsWithoutRemoving(readFileSync(join(E2E_ROOT, file), 'utf8'), row));

			expect(
				offenders,
				`These files create ${row.table} rows and never call ` +
					`${row.removers.map((h) => `\`${h}\``).join(' / ')} from fixtures/helpers.ts. ` +
					'Remove what the spec creates in its teardown, scoped by id or by the ' +
					"spec's own name prefix."
			).toEqual([]);
		});
	}

	test('every create-nothing exemption still names a real creator', () => {
		// An exemption outliving the request it excused would hide the next one.
		for (const file of Object.keys(CREATES_NOTHING)) {
			const source = readFileSync(join(E2E_ROOT, file), 'utf8');
			expect(
				CREATED.some((row) => createsWithoutRemoving(source, row)),
				`${file} no longer creates anything unremoved; drop its exemption`
			).toBe(true);
		}
	});

	test('the helpers still own the statements they are exempted for', () => {
		// If either helper is ever gutted, its guard above would pass vacuously.
		// Pin that the owner really is the owner.
		const helperSource = readFileSync(join(E2E_ROOT, OWNER), 'utf8');
		for (const { table, helper } of GUARDED) {
			expect(hasHandRolledDelete(helperSource, table)).toBe(true);
			expect(helperSource).toContain(`export function ${helper}(`);
		}
	});

	test('the detector flags a known-bad file and clears a known-good one', () => {
		// A clean scan over clean files proves nothing about the detector, so
		// exercise it against fixtures that stand in for both outcomes.
		const bad = [
			"import { tenantPsql } from '../fixtures/helpers';",
			'test.afterEach(() => {',
			"\ttenantPsql(`DELETE FROM invoices WHERE id='${id}'`);",
			'});'
		].join('\n');
		const badLowercase = "sql(`delete from invoices where id = '${id}';`);";
		const badWrapped = 'tenantPsql(`DELETE\n\tFROM\n\tinvoices WHERE id=\'x\'`);';
		const good = [
			"import { deleteInvoicesWhere } from '../fixtures/helpers';",
			'// Never hand-roll a DELETE FROM invoices here — see the helper.',
			'/* DELETE FROM invoices is what this used to do. */',
			'test.afterEach(() => {',
			"\tdeleteInvoicesWhere(`id='${id}'`);",
			'});'
		].join('\n');

		expect(hasHandRolledDelete(bad, 'invoices')).toBe(true);
		expect(hasHandRolledDelete(badLowercase, 'invoices')).toBe(true);
		expect(hasHandRolledDelete(badWrapped, 'invoices')).toBe(true);
		expect(hasHandRolledDelete(good, 'invoices')).toBe(false);

		// The vendor half of the guard, including the two near-misses that must
		// NOT trip it: a different table whose name merely starts with `vendor`,
		// and the child-table sweeps a spec is still free to write.
		const badVendor = "tenantPsql(`DELETE FROM vendors WHERE name LIKE '${MARKER}%'`);";
		const goodVendor = [
			"import { deleteVendorsWhere } from '../fixtures/helpers';",
			"tenantPsql(`DELETE FROM vendor_users WHERE vendor_id='${id}'`);",
			"tenantPsql(`DELETE FROM vendor_statement_reconciliations WHERE id='${id}'`);",
			"deleteVendorsWhere(`id='${id}'`);"
		].join('\n');

		expect(hasHandRolledDelete(badVendor, 'vendors')).toBe(true);
		expect(hasHandRolledDelete(goodVendor, 'vendors')).toBe(false);

		// The workflow half. The near-miss that must NOT trip it is the child
		// sweep a spec is still free to write: `workflows/delete-safety` and
		// `workflows/bulk-delete` each wedge a definition with a synthetic
		// instance and clear it by id.
		const badWorkflow =
			"tenantPsql(`DELETE FROM workflow_definitions WHERE id IN (${doomed})`);";
		const goodWorkflow = [
			"import { deleteWorkflowsWhere } from '../fixtures/helpers';",
			"tenantPsql(`DELETE FROM workflow_instances WHERE id='${instanceId}'`);",
			'deleteWorkflowsWhere(MARKER);'
		].join('\n');

		expect(hasHandRolledDelete(badWorkflow, 'workflow_definitions')).toBe(true);
		expect(hasHandRolledDelete(goodWorkflow, 'workflow_definitions')).toBe(false);

		// The payment-run half. The near-miss is the `payments` sweep a spec is
		// still free to write (`pagination.spec` deletes its marker payments).
		const badRun = "tenantPsql(`DELETE FROM payment_runs WHERE id='${runId}'`);";
		const goodRun = [
			"tenantPsql(`DELETE FROM payments WHERE reference LIKE '${MARKER}%'`);",
			"deletePaymentRunsWhere(`id='${runId}'`);"
		].join('\n');

		expect(hasHandRolledDelete(badRun, 'payment_runs')).toBe(true);
		expect(hasHandRolledDelete(goodRun, 'payment_runs')).toBe(false);
	});

	test('the create detector flags an unremoved create and clears a removed one', () => {
		const [runs, vendors] = CREATED;

		const leakedRun = [
			'const resp = await page.request.post(`${API_BASE}/api/payments/runs`, {',
			'\tdata: { items: [{ invoice_id: id, method: "ach" }] }',
			'});'
		].join('\n');
		const leakedDraft =
			"await page.request.post(`${API_BASE}/api/cash-flow/plans/${planId}/draft-run`, { data });";
		const removedRun = `${leakedRun}\ndeletePaymentRunsWhere(\`id='\${runId}'\`);`;
		// A sub-resource of an existing run creates nothing.
		const execute = 'await page.request.post(`${API_BASE}/api/payments/runs/${runId}/execute`);';
		// A comment naming the endpoint is not a create.
		const prose = '// Stops short of POST /api/payments/runs — page.request.post(`/api/payments/runs`)';

		expect(createsWithoutRemoving(leakedRun, runs)).toBe(true);
		expect(createsWithoutRemoving(leakedDraft, runs)).toBe(true);
		expect(createsWithoutRemoving(removedRun, runs)).toBe(false);
		expect(createsWithoutRemoving(execute, runs)).toBe(false);
		expect(createsWithoutRemoving(prose, runs)).toBe(false);

		const leakedVendor = "await request.post(`${API_BASE}/api/vendors`, { data: { name } });";
		const leakedInsert = "tenantPsql(`INSERT INTO vendors (id, name) VALUES ('${id}', 'X')`);";
		const removedVendor = `${leakedVendor}\ndeleteVendorsWhere(\`name='\${name}'\`);`;
		// Sub-resources and other collections that merely start with the path.
		const notACreate = [
			'await request.post(`${API_BASE}/api/vendors/${id}/block`);',
			"await request.post(`${API_BASE}/api/vendors/import-csv`, { multipart });",
			"await request.post(`${API_BASE}/api/vendor-statements`, { data });"
		].join('\n');

		expect(createsWithoutRemoving(leakedVendor, vendors)).toBe(true);
		expect(createsWithoutRemoving(leakedInsert, vendors)).toBe(true);
		expect(createsWithoutRemoving(removedVendor, vendors)).toBe(false);
		expect(createsWithoutRemoving(notACreate, vendors)).toBe(false);
	});
});
