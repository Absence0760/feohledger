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
import { test } from 'node:test';

import {
	analyze,
	candidateNames,
	isThirdParty,
	registerRows,
	searchableText,
} from './check_subprocessor_registry.mjs';

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

test('a section whose heading names no source directory documents no adapter', () => {
	// Infrastructure and hCaptcha have rows but no adapter registry. Their rows
	// take part in the disclosure check and must not silently satisfy a slug.
	const rows = registerRows('## 20. Infrastructure\n\n| `s3` | **AWS** | Storage |\n');
	assert.equal(rows.length, 1);
	assert.deepEqual(rows[0].families, []);
});
