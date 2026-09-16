import { describe, expect, test } from 'vitest';

import {
	CONTACT,
	LAST_UPDATED,
	OPERATOR,
	OPERATOR_FACTS_COMPLETE,
	isPending,
	operatorFactsComplete,
	pendingFacts,
	PENDING_FACT_LABELS,
	type OperatorFacts,
	type PendingFactKey,
} from './operator';

/**
 * The operator seam is the one thing standing between the published legal
 * pages and a fabricated fact, so its failure mode has to be the safe one: a
 * missing fact reads as pending, never as absent-and-therefore-fine.
 *
 * These tests pin the fail-closed direction. They deliberately do NOT assert
 * that `OPERATOR` is still all-pending — filling a fact in is the intended
 * next step and must not break the suite.
 */

const rep = {
	name: 'Example Rep Ltd',
	address: '1 Example Street, Dublin, Ireland',
	email: 'rep@example.com',
};

const complete: OperatorFacts = {
	serviceName: 'FeohLedger',
	controllerDescription: 'Test operator',
	legalEntity: 'Example Holdings LLC',
	postalAddress: '1 Example Way, Wilmington, DE, USA',
	governingLaw: 'the State of Delaware, USA',
	supervisoryAuthority: 'the Irish Data Protection Commission',
	euRepresentative: rep,
	ukRepresentative: rep,
	dataProtectionOfficer: false,
	hostingRegion: 'AWS eu-west-1 (Ireland)',
};

const PENDABLE: PendingFactKey[] = [
	'legalEntity',
	'postalAddress',
	'governingLaw',
	'supervisoryAuthority',
	'euRepresentative',
	'ukRepresentative',
	'dataProtectionOfficer',
	'hostingRegion',
];

describe('operator facts', () => {
	test('a fully-filled fact set reports complete with nothing pending', () => {
		expect(operatorFactsComplete(complete)).toBe(true);
		expect(pendingFacts(complete)).toEqual([]);
	});

	test('any single null fact fails closed', () => {
		for (const key of PENDABLE) {
			const facts = { ...complete, [key]: null } as OperatorFacts;
			expect(operatorFactsComplete(facts), key).toBe(false);
			expect(pendingFacts(facts), key).toEqual([key]);
		}
	});

	test('a blank string is pending, so the seam agrees with what Fact.svelte draws', () => {
		// `Fact.svelte` shows its gap marker for any falsy value. If `isPending`
		// counted only `null`, a fact set to '' would render a visible gap while
		// the page reported nothing outstanding — the two disagreeing is the bug.
		for (const blank of ['', '   ', '\n']) {
			const facts = { ...complete, postalAddress: blank } as OperatorFacts;
			expect(isPending(facts, 'postalAddress'), JSON.stringify(blank)).toBe(true);
			expect(operatorFactsComplete(facts), JSON.stringify(blank)).toBe(false);
		}
	});

	test('a DPO that was considered and declined is decided, not pending', () => {
		// `false` is a real answer the page publishes ("no DPO is appointed,
		// because the Art 37(1) thresholds are unmet"). Treating it as pending
		// would leave the page claiming an open question that is in fact closed.
		expect(isPending({ ...complete, dataProtectionOfficer: false }, 'dataProtectionOfficer')).toBe(
			false
		);
		expect(isPending({ ...complete, dataProtectionOfficer: null }, 'dataProtectionOfficer')).toBe(
			true
		);
		expect(operatorFactsComplete({ ...complete, dataProtectionOfficer: false })).toBe(true);
	});

	test('an appointed DPO is also decided', () => {
		expect(operatorFactsComplete({ ...complete, dataProtectionOfficer: rep })).toBe(true);
	});

	test('every pending fact has a label to render', () => {
		// A new pendable fact with no label would render a blank bullet in the
		// pending notice — the reader would see that something is outstanding
		// but not what.
		for (const key of PENDABLE) {
			expect(PENDING_FACT_LABELS[key], key).toBeTruthy();
		}
		expect(Object.keys(PENDING_FACT_LABELS).sort()).toEqual([...PENDABLE].sort());
	});

	test('the shipped OPERATOR constant never claims completeness with a null fact', () => {
		const anyNull = PENDABLE.some((key) => OPERATOR[key] === null);
		expect(OPERATOR_FACTS_COMPLETE).toBe(!anyNull);
		expect(operatorFactsComplete(OPERATOR)).toBe(OPERATOR_FACTS_COMPLETE);
	});

	test('the facts that are always known are never null', () => {
		// These two carry the Art 13(1)(a) controller identity even while the
		// registered entity is pending — if they could go null the pages would
		// have no controller at all.
		expect(OPERATOR.serviceName).toBeTruthy();
		expect(OPERATOR.controllerDescription).toBeTruthy();
	});

	test('every published contact address is on the product domain', () => {
		// A legal page pointing at a personal or third-party mailbox is a
		// deliverability and credibility problem; keep them all on one domain
		// so the operator checklist that creates them is one list.
		for (const [role, address] of Object.entries(CONTACT)) {
			expect(address, role).toMatch(/^[a-z]+@feohledger\.com$/);
		}
	});

	test('the last-updated stamp is an ISO date the pages can render verbatim', () => {
		expect(LAST_UPDATED).toMatch(/^\d{4}-\d{2}-\d{2}$/);
		expect(Number.isNaN(Date.parse(LAST_UPDATED))).toBe(false);
	});
});
