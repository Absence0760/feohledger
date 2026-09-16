#!/usr/bin/env node
// The sub-processor registers must not drift from the adapters that exist.
// Plain node, no deps.
//
// `/legal/sub-processors` is a customer-facing commitment about which third
// parties can receive personal data, and `docs/sub-processors.md` is the
// internal register it has to agree with. Neither is derived from anything:
// both are prose, written by reading the code at a point in time. So the next
// adapter added silently makes both wrong, which is exactly how the version
// that shipped with the pages came to list seven AWS services that do not
// exist and to claim extraction defaults to `mock` on a deployed instance.
//
// The check is deliberately not clever. It reads the adapter registrations out
// of the source, the rows out of the internal register, and the prose out of
// the published page, and asks two questions:
//
//   1. Is every registered adapter named in the internal register?
//   2. Is every third-party PROCESSOR the internal register names also named
//      on the published page?
//
// A `mock`, `console` or in-process adapter answers (2) trivially — the
// register writes its processor as `— (in-process)` and it is not a third
// party, so the published page owes it nothing. An operator-supplied endpoint
// (`(operator's relay)`) is the same: the sub-processor there is whoever the
// operator chose, and we cannot name them.
//
// This FAILS rather than warns, unlike `check_compliance_drift.mjs` beside it.
// That one is a heuristic over a diff; this one is an exact comparison of two
// lists, so a finding is a fact rather than a prompt to go and look.
//
// Usage: node scripts/check_subprocessor_registry.mjs

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

export const INTERNAL_REGISTER = 'docs/sub-processors.md';
export const PUBLISHED_REGISTER = 'frontend/src/routes/legal/sub-processors/+page.svelte';
const SERVICES_ROOT = 'backend/app/services';

// Every provider family registers through a decorator whose name ends in
// `_adapter`, and the provider slug is its first string argument. Matching the
// shape rather than a list of family names means a NEW family is covered the
// day it exists, which is the whole point — a hand-maintained family list
// drifts the same way the register does.
//
// The `_adapter` suffix is what separates a provider from the other things
// registered through the same pattern: `@register_country_format` (local UBL /
// CFDI / NF-e generation), `@register_exception_agent` (in-process resolvers)
// and `@register_positive_pay_formatter` (a file layout) reach no third party
// and owe the register nothing.
//
// Anchored at column 0 so a decorator in a USAGE EXAMPLE inside a module
// docstring — `@register_fx_adapter("my_provider")`, which several `__init__.py`
// files carry — is not mistaken for a provider that exists.
const REGISTRATION = /^@register(?:_[a-z_]+)?_adapter\(\s*["']([a-z0-9_]+)["']/gm;

/** Every `.py` under a directory, recursively. */
function pythonFiles(dir) {
	const out = [];
	for (const entry of readdirSync(dir)) {
		if (entry === '__pycache__') continue;
		const path = join(dir, entry);
		if (statSync(path).isDirectory()) out.push(...pythonFiles(path));
		else if (entry.endsWith('.py')) out.push(path);
	}
	return out;
}

/** `{ slug -> family }` for every adapter registered under `services/`. */
export function registeredAdapters(root = SERVICES_ROOT, read = readFileSync) {
	const found = new Map();
	for (const file of pythonFiles(root)) {
		const source = read(file, 'utf8');
		for (const match of source.matchAll(REGISTRATION)) {
			// The family is the directory, which is what a reader needs in order
			// to find the row: `mock` exists in a dozen of them.
			const family = file.slice(root.length + 1).split('/')[0];
			found.set(`${family}:${match[1]}`, { family, slug: match[1], file });
		}
	}
	return found;
}

/**
 * Rows of the internal register, as `{ slug, processor, families }`.
 *
 * Every table there leads with the adapter slug in backticks and the processor
 * in the next cell. A processor beginning `—` is in-process; one wrapped in
 * parentheses is whoever the operator configured.
 *
 * `families` comes from the enclosing section heading, which names the source
 * directory it documents — `## 7. Email — outbound (services/email_adapters/)`.
 * Carrying it is what makes the check family-AWARE: `mock` exists in a dozen
 * families and `ses` in two, so a global slug set would let a new family's
 * adapter be satisfied by an unrelated row that happens to share its name. A
 * section documenting no source directory (infrastructure, hCaptcha) yields an
 * empty list and takes part only in the processor-disclosure check.
 */
export function registerRows(markdown) {
	const rows = [];
	let families = [];
	for (const line of markdown.split('\n')) {
		if (line.startsWith('#')) {
			families = [...line.matchAll(/`services\/([a-z0-9_]+)\//g)].map((m) => m[1]);
			continue;
		}
		const match = line.match(/^\|\s*`([a-z0-9_]+)`\s*\|\s*([^|]*)\|/);
		if (!match) continue;
		const processor = match[2].trim().replace(/\*\*/g, '');
		rows.push({ slug: match[1], processor, families });
	}
	return rows;
}

/** Is this row's processor an actual named third party? */
export function isThirdParty(processor) {
	if (!processor || processor.startsWith('—') || processor === '-') return false;
	// "(operator's relay)", "(operator's provider)" — real sub-processors, but
	// ones only the operator can name.
	if (processor.startsWith('(')) return false;
	return true;
}

/**
 * The names that would satisfy a mention on the published page.
 *
 * A processor cell is written for a human: `AWS (Amazon SES)` clarifies in a
 * parenthetical, and `Zenwork / Tax1099` gives a company and the product it is
 * sold as. The published page is free to use any of them, so ANY match counts —
 * requiring the internal register's exact phrasing would report drift every
 * time the page said the same thing in better words.
 */
export function candidateNames(processor) {
	const names = new Set();
	for (const part of processor.split('/')) {
		const trimmed = part.trim();
		if (!trimmed) continue;
		names.add(trimmed);
		// Both halves of `AWS (Amazon SES)`, since either names the processor.
		const outside = trimmed.split('(')[0].trim();
		if (outside) names.add(outside);
		const inside = trimmed.match(/\(([^)]+)\)/);
		if (inside) names.add(inside[1].trim());
	}
	return [...names].filter(Boolean);
}

/**
 * Published-page text, flattened for searching.
 *
 * It is Svelte markup, so a processor's name arrives HTML-escaped (`Dun &amp;
 * Bradstreet`) and can be split across lines or wrapped in a `<strong>`. All
 * three defeat a naive `includes`, and each would have produced a false
 * "undisclosed processor" finding against a page that names it perfectly well.
 */
export function searchableText(markup) {
	return markup
		.replace(/&amp;/g, '&')
		.replace(/&#39;|&apos;/g, "'")
		.replace(/&quot;/g, '"')
		.replace(/<[^>]+>/g, ' ')
		.replace(/\s+/g, ' ')
		.toLowerCase();
}

export function analyze({ adapters, internal, published }) {
	const findings = [];
	const rows = registerRows(internal);
	const documented = new Set();
	for (const row of rows) {
		for (const family of row.families) documented.add(`${family}:${row.slug}`);
	}

	for (const { family, slug, file } of adapters.values()) {
		if (documented.has(`${family}:${slug}`)) continue;
		findings.push({
			rule: 'unregistered-adapter',
			detail:
				`${file} registers the \`${slug}\` adapter, and ${INTERNAL_REGISTER} has no row ` +
				`for it under a section documenting \`services/${family}/\`. Add one — including ` +
				`for an in-process adapter, whose row is what records that nothing leaves the ` +
				`process. A section's heading must name its directory for its rows to count.`,
		});
	}

	const haystack = searchableText(published);
	const missing = new Map();
	for (const { slug, processor } of rows) {
		if (!isThirdParty(processor)) continue;
		const names = candidateNames(processor);
		if (names.some((name) => haystack.includes(name.toLowerCase()))) continue;
		if (!missing.has(processor)) missing.set(processor, []);
		missing.get(processor).push(slug);
	}
	for (const [name, forSlugs] of missing) {
		findings.push({
			rule: 'undisclosed-processor',
			detail:
				`${INTERNAL_REGISTER} names **${name}** as the processor behind ` +
				`${forSlugs.map((s) => `\`${s}\``).join(', ')}, but ${PUBLISHED_REGISTER} ` +
				`never mentions it. A third party that can receive personal data has to be ` +
				`on the page customers read, not only in the internal copy.`,
		});
	}

	return findings;
}

function main() {
	const adapters = registeredAdapters();
	const findings = analyze({
		adapters,
		internal: readFileSync(INTERNAL_REGISTER, 'utf8'),
		published: readFileSync(PUBLISHED_REGISTER, 'utf8'),
	});

	if (findings.length === 0) {
		console.log(
			`Sub-processor registers agree with ${adapters.size} registered adapter(s).`
		);
		return 0;
	}

	console.error(`Sub-processor register drift — ${findings.length} finding(s):\n`);
	for (const finding of findings) console.error(`  [${finding.rule}] ${finding.detail}\n`);
	console.error(
		`Both registers are commitments: ${PUBLISHED_REGISTER} to customers, ` +
			`${INTERNAL_REGISTER} to the Article 30 record behind it.`
	);
	return 1;
}

if (import.meta.url === `file://${process.argv[1]}`) process.exit(main());
