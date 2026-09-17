#!/usr/bin/env node
// Tests for the sub-processor register guard.
// `node --test scripts/check_subprocessor_registry.test.mjs`
//
// Unlike the compliance-drift detector beside it, this one is exact rather than
// heuristic — so the cases worth pinning are the ones where being exact would
// make it WRONG. Every false positive here was a real one the first run
// produced against a register that was perfectly correct: a name written
// `Zenwork / Tax1099` in one document and `Tax1099` in the other, a `Dun &
// Bradstreet` that reaches the page HTML-escaped as `Dun &amp; Bradstreet`, and
// a decorator inside a module docstring's usage example.
//
// A guard that reports drift against a correct register gets switched off, and
// a switched-off guard protects nothing.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	analyze,
	candidateNames,
	familyScan,
	isThirdParty,
	registeredAdapters,
	registerRows,
	searchableText,
} from './check_subprocessor_registry.mjs';

const GUARD = fileURLToPath(new URL('./check_subprocessor_registry.mjs', import.meta.url));

/** The internal register, in miniature. */
const INTERNAL = `
## 1. Virtual cards (\`services/card_adapters/\`)

| Adapter | Processor | Service |
|---|---|---|
| \`mock\` | — (in-process) | Test issuance |
| \`lithic\` | **Lithic** | Virtual card issuing |

## 2. Email (\`services/email_adapters/\`)

| Adapter | Processor | Service |
|---|---|---|
| \`console\` | — (stdout) | Logs email |
| \`smtp\` | **(operator's relay)** | SMTP delivery |
| \`ses\` | **AWS (Amazon SES)** | Transactional email |
`;

const PUBLISHED = '<p>We use <strong>Lithic</strong> for cards and AWS for hosting.</p>';

const adapters = (...entries) =>
	new Map(
		entries.map(([family, slug]) => [
			`${family}:${slug}`,
			{ family, slug, file: `backend/app/services/${family}/${slug}_adapter.py` },
		]),
	);

test('a registered adapter with a row in its own family passes', () => {
	const findings = analyze({
		adapters: adapters(['card_adapters', 'lithic'], ['email_adapters', 'ses']),
		internal: INTERNAL,
		published: PUBLISHED,
	});
	assert.deepEqual(findings, []);
});

test('a registered adapter with no row is reported', () => {
	const findings = analyze({
		adapters: adapters(['card_adapters', 'marqeta']),
		internal: INTERNAL,
		published: PUBLISHED,
	});
	assert.equal(findings.length, 1);
	assert.equal(findings[0].rule, 'unregistered-adapter');
	assert.match(findings[0].detail, /marqeta/);
});

test('a row in ANOTHER family does not satisfy an adapter', () => {
	// `mock` exists in a dozen families and `ses` in two, so a global slug set
	// would let a new family's adapter be covered by an unrelated row that
	// happens to share its name — which is most of them.
	const findings = analyze({
		adapters: adapters(['assistant', 'mock']),
		internal: INTERNAL,
		published: PUBLISHED,
	});
	assert.equal(findings.length, 1);
	assert.match(findings[0].detail, /services\/assistant\//);
});

test('a third party the published page never names is reported', () => {
	const internal = INTERNAL.replace('| **Lithic** |', '| **Marqeta** |');
	const findings = analyze({ adapters: adapters(), internal, published: PUBLISHED });
	assert.equal(findings.length, 1);
	assert.equal(findings[0].rule, 'undisclosed-processor');
	assert.match(findings[0].detail, /Marqeta/);
});

test('an in-process or operator-supplied processor owes the published page nothing', () => {
	// `mock`, `console` and the operator's own relay reach no third party we can
	// name, so requiring them on a customer-facing register would be noise.
	assert.equal(isThirdParty('— (in-process)'), false);
	assert.equal(isThirdParty('— (stdout)'), false);
	assert.equal(isThirdParty("(operator's relay)"), false);
	assert.equal(isThirdParty('Lithic'), true);

	const findings = analyze({ adapters: adapters(), internal: INTERNAL, published: PUBLISHED });
	assert.deepEqual(findings, []);
});

test('either half of a two-name processor counts as a mention', () => {
	// The internal register writes `Zenwork / Tax1099`; the published page says
	// Tax1099. Both name the same company, and demanding the internal
	// register's exact phrasing would report drift against a correct page.
	assert.deepEqual(candidateNames('Zenwork / Tax1099').sort(), ['Tax1099', 'Zenwork']);
	assert.ok(candidateNames('AWS (Amazon SES)').includes('AWS'));
	assert.ok(candidateNames('AWS (Amazon SES)').includes('Amazon SES'));
});

test('an HTML-escaped or tag-split name on the published page still counts', () => {
	// `Dun & Bradstreet` arrives as `Dun &amp; Bradstreet`, and a name can be
	// wrapped in a <strong> or broken across lines by the formatter. All three
	// defeat a naive includes() and produced false findings on the first run.
	const markup = '<td>Dun &amp;\n  Bradstreet</td>';
	assert.ok(searchableText(markup).includes('dun & bradstreet'));

	const internal = INTERNAL.replace('| **Lithic** |', '| **Dun & Bradstreet** |');
	const findings = analyze({
		adapters: adapters(),
		internal,
		// AWS too: the fixture's other third party, so the only thing this case
		// can fail on is the escaped name.
		published: `${PUBLISHED}${markup}`,
	});
	assert.deepEqual(findings, []);
});

test('an escaped entity is decoded once, never twice', () => {
	// `&amp;quot;` is the markup for the literal text `&quot;`. Undoing `&amp;`
	// before `&quot;` would decode it again, to a `"` the page never showed.
	assert.equal(searchableText('A &amp;quot; B'), 'a &quot; b');
	assert.equal(searchableText('A &amp;amp; B'), 'a &amp; b');
	assert.equal(searchableText('A &amp;#39; B'), 'a &#39; b');
	assert.equal(searchableText('O&#39;Brien &apos;n&apos; &quot;Co&quot;'), `o'brien 'n' "co"`);
});

test('a section whose heading names no source directory documents no adapter', () => {
	// Infrastructure and hCaptcha have rows but no adapter registry. Their rows
	// take part in the disclosure check and must not silently satisfy a slug.
	const rows = registerRows('## 20. Infrastructure\n\n| `s3` | **AWS** | Storage |\n');
	assert.equal(rows.length, 1);
	assert.deepEqual(rows[0].families, []);
});

// --- Reading the registries -------------------------------------------------
//
// The rules above all take an adapter set as given. These ones check the step
// before: that the adapter set is actually the set of adapters. It was not.
// `email_intake_adapters/` declares its providers as a literal dict rather than
// with a decorator, so the whole family — `ses` and `mailgun`, which receive
// every inbound invoice attachment — was invisible, and a fourth provider added
// beside them kept the check green.

/** A throwaway `services/` tree, so the reading tests run on files, not strings. */
function servicesTree(families) {
	const root = mkdtempSync(join(tmpdir(), 'subproc-'));
	for (const [family, files] of Object.entries(families)) {
		mkdirSync(join(root, family), { recursive: true });
		for (const [name, body] of Object.entries(files)) {
			writeFileSync(join(root, family, name), body);
		}
	}
	return root;
}

test('a provider declared in a registry dict is read, not only a decorated one', () => {
	const root = servicesTree({
		email_intake_adapters: {
			'__init__.py':
				'_REGISTRY: dict[str, ParserFn] = {\n' +
				'    "ses": _ses.parse,\n' +
				'    "mailgun": _mailgun.parse,\n' +
				'}\n',
		},
		card_adapters: { 'lithic.py': '@register_card_adapter("lithic")\nclass X: pass\n' },
	});
	const slugs = [...registeredAdapters(root).keys()].sort();
	assert.deepEqual(slugs, [
		'card_adapters:lithic',
		'email_intake_adapters:mailgun',
		'email_intake_adapters:ses',
	]);
});

test('an unrelated module constant is not mistaken for a provider registry', () => {
	// Only a module-level name that says registry counts. A local dict, or a
	// constant that merely holds strings, would otherwise inject phantom
	// "adapters" the register can never satisfy — noise that gets the guard
	// switched off.
	const root = servicesTree({
		card_adapters: {
			'base.py':
				'_TIMEOUTS: dict[str, int] = {\n    "connect": 5,\n}\n' +
				'DEFAULTS = {\n    "currency": "USD",\n}\n' +
				'    _REGISTRY = {\n        "indented": 1,\n    }\n',
		},
	});
	assert.deepEqual([...registeredAdapters(root).keys()], []);
});

test('a family whose registrations cannot be read at all is reported', () => {
	// The blind spot itself. Reporting zero providers and passing is
	// indistinguishable from having nothing to declare, so the check has to say
	// which of the two it is.
	const root = servicesTree({
		peppol_adapters: { 'gateway.py': 'PROVIDERS = ["as4_gateway"]\n' },
	});
	const adapters = registeredAdapters(root);
	const findings = analyze({
		adapters,
		internal: INTERNAL,
		published: PUBLISHED,
		families: familyScan(INTERNAL, root, adapters),
	});
	assert.equal(findings.length, 1);
	assert.equal(findings[0].rule, 'unreadable-registry');
	assert.match(findings[0].detail, /services\/peppol_adapters\//);
});

test('a family we read and found nothing third-party in is not a blind spot', () => {
	// `positive_pay_adapters/` registers file LAYOUTS — `@register_positive_pay_
	// formatter` — which render a file the operator uploads to their own bank and
	// call nobody. Zero providers there is the right answer, not a failure to
	// read, and the difference is that the decorators are visible.
	const root = servicesTree({
		positive_pay_adapters: {
			'csv_formatter.py': '@register_positive_pay_formatter("csv")\nclass X: pass\n',
		},
	});
	const adapters = registeredAdapters(root);
	assert.equal(adapters.size, 0);
	assert.deepEqual(
		analyze({
			adapters,
			internal: INTERNAL,
			published: PUBLISHED,
			families: familyScan(INTERNAL, root, adapters),
		}),
		[],
	);
});

test('a family is found by its `_adapters` suffix or by the register naming it', () => {
	// Both halves are derived. The suffix covers a new family the day the
	// directory exists; the register's own section headings cover the two
	// families that do not carry it (`services/assistant/`, `services/audit_
	// shipping/`). Neither is a hand-maintained list.
	const root = servicesTree({
		card_adapters: { 'x.py': '@register_card_adapter("lithic")\n' },
		audit_shipping: { 'x.py': '@register_audit_shipping_adapter("cloudwatch")\n' },
		e_invoice: { 'x.py': '@register_country_format("mx")\n' },
	});
	const internal = `${INTERNAL}\n## 3. Audit shipping (\`services/audit_shipping/\`)\n`;
	assert.deepEqual(
		familyScan(internal, root, registeredAdapters(root))
			.map((f) => f.family)
			.sort(),
		// `e_invoice/` is neither: it is local document generation, not a
		// provider family, and nothing documents it as one.
		['audit_shipping', 'card_adapters'],
	);
});

// --- The command itself -----------------------------------------------------

test('the command exits 0 on a register that matches the code', () => {
	const root = servicesTree({
		card_adapters: { 'lithic.py': '@register_card_adapter("lithic")\n@register_card_adapter("mock")\n' },
	});
	const dir = mkdtempSync(join(tmpdir(), 'subproc-docs-'));
	writeFileSync(join(dir, 'internal.md'), INTERNAL);
	writeFileSync(join(dir, 'published.svelte'), PUBLISHED);

	const out = execFileSync(
		process.execPath,
		[GUARD, '--services-root', root, '--internal', join(dir, 'internal.md'), '--published', join(dir, 'published.svelte')],
		{ encoding: 'utf8' },
	);
	assert.match(out, /registers agree with 2 registered adapter\(s\)/);
});

test('the command exits 1 on a deliberately broken register', () => {
	// The whole point. A guard nobody has seen fail is not a guard, and the unit
	// cases above only prove `analyze` returns objects — they never run the
	// command or look at its exit code, which is the part CI acts on.
	const root = servicesTree({
		card_adapters: { 'marqeta.py': '@register_card_adapter("marqeta")\n' },
	});
	const dir = mkdtempSync(join(tmpdir(), 'subproc-docs-'));
	writeFileSync(join(dir, 'internal.md'), INTERNAL);
	writeFileSync(join(dir, 'published.svelte'), PUBLISHED);

	assert.throws(
		() =>
			execFileSync(
				process.execPath,
				[GUARD, '--services-root', root, '--internal', join(dir, 'internal.md'), '--published', join(dir, 'published.svelte')],
				{ encoding: 'utf8', stdio: 'pipe' },
			),
		(error) => {
			assert.equal(error.status, 1);
			assert.match(error.stderr, /\[unregistered-adapter\].*marqeta/s);
			return true;
		},
	);
});
