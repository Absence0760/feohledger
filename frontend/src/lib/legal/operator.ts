/**
 * Operator facts for the published legal pages (`/legal/*`).
 *
 * The pages under `routes/legal/` carry complete legal text. What they cannot
 * carry is the handful of facts only the operator knows — the legal entity, a
 * postal address, the governing-law venue, the Art 27 representatives. Those
 * live here, and every one of them is allowed to be `null`.
 *
 * `null` means PENDING, and a pending fact renders a visibly-marked placeholder
 * rather than a fabricated one. That is the whole point of the seam: a privacy
 * policy that states a false controller address is worse than one that admits
 * the address is not yet filled, because an inaccurate policy is itself an
 * Art 5(1)(a) fairness/transparency problem and a deceptive-practice exposure —
 * not merely an unfinished page. Fail closed, never fabricate.
 *
 * Filling these is an operator checklist item (`docs/followups.md`), not a code
 * change of any substance: set the value here and the pending copy disappears
 * from every page at once, with no other edit.
 */

export interface LegalRepresentative {
	/** Legal name of the representative firm or person. */
	name: string;
	/** Full postal address, including country. */
	address: string;
	/** Contact email the representative monitors. */
	email: string;
}

/**
 * A Data Protection Officer is one of three states, and they are genuinely
 * different things to publish:
 *
 * - a record — appointed, and named on the page as Art 37(7) requires;
 * - `false`  — considered and deliberately NOT appointed, because the Art 37(1)
 *              thresholds are unmet. The page says so explicitly, which is the
 *              honest and useful disclosure;
 * - `null`   — not yet decided. Renders as pending.
 *
 * Collapsing "decided against" into `null` would leave the page permanently
 * claiming a decision is outstanding when it has actually been made.
 */
export type DpoStatus = LegalRepresentative | false | null;

/**
 * The registered entity follows the same three-state rule as the DPO:
 *
 * - a string — the registered company the Service is operated through;
 * - `false`  — decided: there is no separate registered entity, and the
 *              individual in `controllerDescription` is the party to every
 *              agreement (sole proprietor — `docs/decisions.md` §251);
 * - `null`   — not yet decided. Renders as pending.
 */
export type LegalEntityStatus = string | false | null;

/**
 * A postal address, or `false` when the operator has decided not to publish
 * one: written contact is by email (`CONTACT`), and the pages say so and name
 * `location` instead. `false` must never be rendered as an address — "Postal
 * mail reaches us at Virginia" would be a claim nobody can act on.
 */
export type PostalAddressStatus = string | false | null;

export interface OperatorFacts {
	/** Product name. Never null — the service exists. */
	serviceName: string;
	/**
	 * Who the controller actually is, in prose, for Art 13(1)(a). Known today
	 * even though the registered entity is not: the service is operated by a
	 * natural person trading as a sole proprietor.
	 */
	controllerDescription: string;
	/** See {@link LegalEntityStatus}. */
	legalEntity: LegalEntityStatus;
	/**
	 * Where the operator is based, as a region a reader can place (state and
	 * country). Always known, so never pending; published wherever an address
	 * would otherwise stand when `postalAddress` is `false`.
	 */
	location: string;
	/** Controller's postal address (Art 13(1)(a) contact details). See {@link PostalAddressStatus}. */
	postalAddress: PostalAddressStatus;
	/** Governing law + venue for the Terms, e.g. "the State of Delaware, USA". */
	governingLaw: string | null;
	/**
	 * The lead supervisory authority a complaint goes to (Art 13(2)(d)). `false`
	 * is decided: with no establishment in the EU there is no lead authority
	 * under the Art 56 one-stop shop, and the page says so — a data subject
	 * complains to the authority where they live or work, which it already
	 * explains. Naming an authority we have no establishment under would be
	 * the fabricated fact this module exists to prevent.
	 */
	supervisoryAuthority: string | false | null;
	/** Art 27 EU representative — required of a non-EU controller serving EU residents. */
	euRepresentative: LegalRepresentative | null;
	/** UK GDPR Art 27 representative — the separate UK appointment. */
	ukRepresentative: LegalRepresentative | null;
	/** See {@link DpoStatus}. */
	dataProtectionOfficer: DpoStatus;
	/**
	 * Where customer data is physically hosted, named as a region a reader can
	 * check (e.g. "AWS eu-west-1 (Ireland)"). Set 2026-10-07 to `us-east-1`,
	 * the region `infra/` already deploys to (`aws_region` default) and the one
	 * the workload stack must use. Before the pages are served publicly the
	 * stack has to actually run there — if it ever moves, change this in the
	 * same commit, or the pages state a region the data is not in.
	 */
	hostingRegion: string | null;
}

/**
 * Addresses the legal pages tell people to write to. Listing one here is a
 * commitment that it is monitored — creating each alias is an operator
 * checklist item tracked in `docs/followups.md`. A published `mailto:` that
 * bounces is worse than no address at all, because a data subject who writes to
 * it reasonably believes the request has been made and the Art 12(3) clock has
 * started.
 */
export const CONTACT = {
	/** Data-protection enquiries, DSARs, erasure requests. */
	privacy: 'privacy@feohledger.com',
	/** Vulnerability reports and security incidents. */
	security: 'security@feohledger.com',
	/** Contract, Terms and DPA execution. */
	legal: 'legal@feohledger.com',
	/** General product support. */
	support: 'support@feohledger.com',
	/**
	 * Enterprise/commercial enquiries. Published on the pricing page's
	 * Enterprise call-to-action, which previously pointed at
	 * `sales@feohledger.example` — the reserved `.example` TLD, so that
	 * button could never deliver a message to anyone.
	 */
	sales: 'sales@feohledger.com',
} as const;

/**
 * The date the legal pages were last substantively changed, ISO-8601.
 *
 * Every page renders it, and the e2e spec pins the format. Bump it in the same
 * commit as any substantive change to the text — a stale effective date on a
 * changed policy defeats the Art 12 transparency the date exists to provide.
 */
export const LAST_UPDATED = '2026-10-07';

/**
 * The live operator facts.
 *
 * Pending entries are deliberate, not oversights — see the module docstring.
 * The operator trades as a sole proprietor (`docs/decisions.md` §251), so there
 * is deliberately no registered entity, and contact is by email rather than a
 * published postal address. Both are decided values (`false`), not gaps.
 */
export const OPERATOR: OperatorFacts = {
	serviceName: 'FeohLedger',
	controllerDescription:
		'Jared Howard, an individual operating as a sole proprietor',
	legalEntity: false,
	location: 'Virginia, United States',
	postalAddress: false,
	governingLaw: 'the Commonwealth of Virginia, United States',
	supervisoryAuthority: false,
	euRepresentative: null,
	ukRepresentative: null,
	dataProtectionOfficer: false,
	hostingRegion: 'AWS us-east-1 (Northern Virginia, United States)',
};

/**
 * Human-readable label for each fact that can be pending, in the order the
 * pending notice lists them. Keyed so a new pending fact cannot be added to
 * `OperatorFacts` without a label — `Record` makes the omission a type error.
 */
export const PENDING_FACT_LABELS: Record<PendingFactKey, string> = {
	legalEntity: 'the registered legal entity',
	postalAddress: 'a postal address for the controller',
	governingLaw: 'the governing law and venue',
	supervisoryAuthority: 'the lead supervisory authority for complaints',
	euRepresentative: 'an EU representative (GDPR Art 27)',
	ukRepresentative: 'a UK representative (UK GDPR Art 27)',
	dataProtectionOfficer: 'whether a Data Protection Officer is appointed',
	hostingRegion: 'the hosting region for customer data',
};

/** The subset of {@link OperatorFacts} that is allowed to be pending. */
export type PendingFactKey =
	| 'legalEntity'
	| 'postalAddress'
	| 'governingLaw'
	| 'supervisoryAuthority'
	| 'euRepresentative'
	| 'ukRepresentative'
	| 'dataProtectionOfficer'
	| 'hostingRegion';

const PENDING_FACT_KEYS: readonly PendingFactKey[] = [
	'legalEntity',
	'postalAddress',
	'governingLaw',
	'supervisoryAuthority',
	'euRepresentative',
	'ukRepresentative',
	'dataProtectionOfficer',
	'hostingRegion',
];

/**
 * Is a given fact still pending?
 *
 * `false` is a decided value, not a pending one — only `null` is pending. This
 * is the single place that distinction is made, so `dataProtectionOfficer:
 * false` (considered, not appointed) never renders as an unfinished page.
 */
export function isPending(facts: OperatorFacts, key: PendingFactKey): boolean {
	const value = facts[key];
	// A blank string is pending too. `Fact.svelte` renders its gap marker for
	// any falsy value, so without this a fact set to `''` would show the reader
	// a visible "to be confirmed" marker while `operatorFactsComplete()` reported
	// the set finished and the page's pending notice listed nothing. The two
	// have to agree, and they agree in the direction that admits the gap.
	if (typeof value === 'string') return value.trim() === '';
	return value === null;
}

/** Every pending fact, in declaration order, for the pending notice. */
export function pendingFacts(facts: OperatorFacts): PendingFactKey[] {
	return PENDING_FACT_KEYS.filter((key) => isPending(facts, key));
}

/** True when no fact is pending and the pages state every operator fact outright. */
export function operatorFactsComplete(facts: OperatorFacts): boolean {
	return pendingFacts(facts).length === 0;
}

/** Convenience bindings for the pages, computed once against the live facts. */
export const PENDING = pendingFacts(OPERATOR);
export const OPERATOR_FACTS_COMPLETE = operatorFactsComplete(OPERATOR);
