import { describe, expect, it } from 'vitest';

import { CONTACT } from './operator';

/**
 * Static guard: **a published address comes from the registry, never a literal.**
 *
 * `operator.ts`'s `CONTACT` says of itself that listing an address there "is a
 * commitment that it is monitored", and that a published `mailto:` which
 * bounces "is worse than no address at all, because a data subject who writes
 * to it reasonably believes the request has been made and the Art 12(3) clock
 * has started". That reasoning is about the address being *real*, and it is not
 * specific to the legal pages — it applies wherever the product tells a person
 * to write to us.
 *
 * It had already failed twice, the same way, in two places:
 *
 *   - the pricing page's Enterprise call-to-action shipped
 *     `sales@feohledger.example`, and
 *   - `routes/billing/+page.svelte` shipped `billing@example.com` on all three
 *     of its "contact us" buttons — the empty state, the plan-change link and
 *     the card-setup failure.
 *
 * Both are the IANA-reserved `.example` space (RFC 2606 / RFC 6761), which is
 * guaranteed never to resolve. Every one of those buttons was dead, and looked
 * exactly like a working one. The billing three are the more instructive case:
 * they sat on an authenticated in-app page rather than a legal document, which
 * is why fixing the first occurrence did not surface them.
 *
 * A literal-address ban is the rule that catches both, and it is strictly
 * stronger than banning `.example` would be. An unprovisioned real-looking
 * address — `billing@feohledger.com`, which is NOT one of the five aliases that
 * exist — bounces exactly like a reserved one, so a `.example` blocklist would
 * wave through the more plausible mistake. Sourcing from `CONTACT` (or from an
 * `OPERATOR` fact, which is `null` until filled and renders a marked gap rather
 * than an invented value) is what actually ties a published address to one
 * someone has committed to monitor.
 *
 * Scope note: this checks `mailto:` only. An `example.com` in a form
 * `placeholder`, or in a workflow-builder webhook-URL hint, is correct usage —
 * it is illustrative text the user replaces, not somewhere we promise to read.
 */

const RAW = import.meta.glob('/src/**/*.{svelte,ts}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	// This file necessarily contains the very literals it forbids.
	.filter(({ path }) => path !== 'lib/legal/publishedAddresses.test.ts')
	.sort((a, b) => a.path.localeCompare(b.path));

/**
 * A `mailto:` followed by a literal address — rather than a Svelte
 * interpolation (`mailto:{CONTACT.x}`) or a template expression
 * (`` `mailto:${CONTACT.x}` ``).
 *
 * It matches on the shape of an address (`local-part@domain`) rather than on
 * "anything that is not a brace". That distinction is load-bearing and the
 * third test below is why: a negated character class that forgot `$` happily
 * matched `mailto:$` out of the template-literal form, so the guard reported
 * `Pricing.svelte` — a correctly registry-sourced call site — as an offender.
 * Requiring a local-part cannot make that mistake.
 *
 * Capturing to end-of-address lets the failure name the offender instead of
 * only its file, which is the difference between a guard someone acts on and
 * one they re-run to find out what it meant.
 */
const LITERAL_MAILTO = /mailto:([A-Za-z0-9._%+-]+@[^"'`{}\s>)]+)/g;

describe('published contact addresses', () => {
	it('exports only addresses on the operator domain', () => {
		// The registry itself is the one place a literal is correct, so it gets
		// the check the call sites are exempt from. `.example` here would mean
		// every consumer inherited a dead address and this suite still passed.
		for (const [key, address] of Object.entries(CONTACT)) {
			expect(address, `CONTACT.${key}`).toMatch(/^[a-z]+@feohledger\.com$/);
		}
	});

	it('are never hardcoded at a call site', () => {
		const offenders: string[] = [];

		for (const { path, source } of files) {
			for (const [, address] of source.matchAll(LITERAL_MAILTO)) {
				offenders.push(`${path}: mailto:${address}`);
			}
		}

		expect(
			offenders,
			'A published address must come from `CONTACT` in lib/legal/operator.ts ' +
				'(or an OPERATOR fact), never a literal — see this file’s header for why. ' +
				`Found:\n  ${offenders.join('\n  ')}`
		).toEqual([]);
	});

	it('catches the two literals this guard was written for', () => {
		// Proves the rule above actually bites rather than passing vacuously —
		// both strings are the ones that really shipped.
		const shipped = [
			'<a href="mailto:billing@example.com">Contact us</a>',
			'ctaHref: `mailto:sales@feohledger.example`'
		];

		for (const sample of shipped) {
			expect([...sample.matchAll(LITERAL_MAILTO)]).toHaveLength(1);
		}

		// …and that a registry-sourced href in either spelling does not.
		const sourced = [
			'<a href="mailto:{CONTACT.sales}">Contact sales</a>',
			'ctaHref: `mailto:${CONTACT.sales}`'
		];

		for (const sample of sourced) {
			expect([...sample.matchAll(LITERAL_MAILTO)]).toHaveLength(0);
		}
	});
});
