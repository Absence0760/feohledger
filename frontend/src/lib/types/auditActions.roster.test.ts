import { describe, expect, it, vi } from 'vitest';
import { EN_ALL } from '#lib/i18n/allMessages.ts';
import type { MessageKey } from '#lib/i18n/messages.ts';

// The real store imports `$app/env`. Every lookup below passes its own `has`
// (English from every catalogue), so the runtime's default is never consulted.
vi.mock('#lib/i18n/store.svelte.ts', () => ({ hasMessage: () => false }));

const { DYNAMIC_ACTION_TEMPLATES, auditActionKey, auditActionLabel } = await import('./auditActions');

/**
 * Roster guard: every `AuditLog.action` token the backend writes has a label
 * on /audit (`audit.action.<token>`), and every f-string template has a rule.
 *
 * There is no backend registry to compare against — tokens are passed at a few
 * hundred call sites — so this scans `backend/app/**\/*.py` the way
 * `notification.roster.test.ts` reads its model: through Vite's raw glob (the
 * frontend carries no `@types/node`). Three harvests, each catching a shape the
 * others miss:
 *
 *  1. the value of an `action=` / `action_name=` keyword or an `*ACTION*`
 *     constant (a parenthesised conditional is read whole);
 *  2. positional literals inside any call whose name contains `audit`
 *     (`_audit_transition(db, req, org, user, "requisition.submitted")`);
 *  3. any dotted literal whose prefix is an audit prefix found by 1–2
 *     (`_record_or_503(org.id, user.id, "organization.erp_connected", …)`),
 *     minus `NOT_AUDIT_ACTIONS` below.
 *
 * Harvest 3 over-reaches on purpose: a new token is either labelled or named
 * here as not-an-action, never silently unlabelled. An unlabelled token still
 * renders — raw — so the failure is cosmetic, not lost evidence.
 */

const RAW = import.meta.glob('../../../../backend/app/**/*.py', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/** Dotted literals with an audit prefix that are not audit actions. */
const NOT_AUDIT_ACTIONS = new Set([
	// app/api/permissions.py — the granular permission catalogue.
	'invoice.approve',
	'payment.execute',
	'payment.void',
	'payment.record_external',
	'payment_run.approve',
	'vendor.bank_change.approve',
	'vendor.block',
	'vendor.manage',
	'vendor.sanctions.adjudicate',
	'user.manage',
	// Rate-limit bucket name (auth.py).
	'auth.forgot_password',
	// `trigger=` reasons for the card-cancel sweep (vendors.py, vendor_screening.py).
	'vendor.status_changed',
	'vendor.cancel_cards_retry',
	'vendor.sanctions_match',
	// Outbound webhook event name (models/webhook.py).
	'payment.settled',
	// Sage Intacct query field names.
	'vendor.id',
	'vendor.name',
	// audit_summary.py's prefix match for the templated ERP-status token.
	'invoice.erp_status_'
]);

const DOTTED = /^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$/;
const UNDOTTED = /^[a-z][a-z0-9_]*$/;
const STRING = /(f?)"([^"\n]*)"/g;

function balancedEnd(src: string, open: number): number {
	let depth = 0;
	for (let i = open; i < src.length; i++) {
		if (src[i] === '(') depth++;
		else if (src[i] === ')' && --depth === 0) return i + 1;
	}
	return src.length;
}

function harvest(files: Record<string, string>) {
	const tokens = new Set<string>();
	const templates = new Set<string>();
	const take = (segment: string) => {
		for (const [, f, text] of segment.matchAll(STRING)) {
			if (f && text.includes('{')) templates.add(text);
			else if (!f && DOTTED.test(text)) tokens.add(text);
		}
	};

	for (const src of Object.values(files)) {
		for (const m of src.matchAll(/\b(?:action(?:_name)?|[A-Z_]*ACTION[A-Z_]*)\s*=\s*/g)) {
			const at = m.index + m[0].length;
			take(src[at] === '(' ? src.slice(at, balancedEnd(src, at)) : src.slice(at, src.indexOf('\n', at)));
		}
		for (const m of src.matchAll(/\b\w*audit\w*\(/g)) {
			const open = m.index + m[0].length - 1;
			const call = src.slice(open, balancedEnd(src, open));
			// Positional arguments only: other keywords (`details={…}`) carry
			// payload values, not the action.
			take(call.replace(/\b(?!action\b)\w+\s*=\s*(\{[^}]*\}|[^,\n]*)/g, ''));
			// The three chat events are the only undotted tokens; read them
			// from the audit call's own `action=` so a fourth is caught too.
			for (const [, word] of call.matchAll(/\baction\s*=\s*"([^"\n]*)"/g)) {
				if (UNDOTTED.test(word)) tokens.add(word);
			}
		}
	}

	const prefixes = new Set([...tokens].filter((t) => t.includes('.')).map((t) => t.split('.')[0]));
	for (const src of Object.values(files)) {
		const code = src.replace(/#[^\n]*/g, '');
		for (const [, f, text] of code.matchAll(STRING)) {
			if (!f && DOTTED.test(text) && prefixes.has(text.split('.')[0]) && !NOT_AUDIT_ACTIONS.has(text)) {
				tokens.add(text);
			}
		}
	}
	return { tokens: [...tokens].sort(), templates: [...templates].sort() };
}

const { tokens, templates } = harvest(RAW);
const EN = EN_ALL as Record<string, string>;
// The labels are a lazy catalogue slice the store has not loaded here, so the
// lookups below read English from every catalogue at once.
const hasEn = (key: string) => Object.hasOwn(EN, key);

describe('audit action roster', () => {
	it('reads the backend source', () => {
		expect(Object.keys(RAW).length, 'the raw glob did not reach backend/app').toBeGreaterThan(50);
	});

	it('harvests a plausible vocabulary', () => {
		// Guards the guard: a broken scan must not pass vacuously.
		expect(tokens.length).toBeGreaterThan(300);
		for (const known of ['invoice.approved', 'payment.voided', 'requisition.submitted', 'chat_message_posted']) {
			expect(tokens, `${known} was not harvested`).toContain(known);
		}
	});

	it('every backend action token has a label', () => {
		const unlabelled = tokens.filter((t) => auditActionKey(t, hasEn) === null);
		expect(
			unlabelled,
			'Add `audit.action.<token>` to all six locale catalogues, or list a ' +
				'non-action literal in NOT_AUDIT_ACTIONS with its reason.'
		).toEqual([]);
	});

	it('every f-string action template has a labelling rule', () => {
		expect(templates).toEqual([...DYNAMIC_ACTION_TEMPLATES].sort());
	});

	it('no label is blank in English', () => {
		for (const t of tokens) {
			expect(EN[`audit.action.${t}`]?.trim().length, `${t} has a blank label`).toBeGreaterThan(0);
		}
	});
});

describe('auditActionLabel', () => {
	const t = (key: MessageKey, params?: Record<string, string | number>) =>
		Object.entries(params ?? {}).reduce((s, [k, v]) => s.replace(`{${k}}`, String(v)), EN[key] ?? key);

	it('labels an exact token', () => {
		expect(auditActionLabel('invoice.approved', t, hasEn)).toBe('Invoice approved');
	});

	it('labels templated tokens with their variable tail', () => {
		expect(auditActionLabel('payment.in_transit', t, hasEn)).toBe('Payment status: in transit');
		expect(auditActionLabel('vendor.bank_details_change_approved', t, hasEn)).toBe(
			'Vendor change approved (bank details)'
		);
		expect(auditActionLabel('invoice.viewed', t, hasEn)).toBe('Record viewed');
	});

	it('prefers an exact label over a pattern', () => {
		expect(auditActionLabel('payment.voided', t, hasEn)).toBe('Payment voided');
		expect(auditActionLabel('vendor.bank_details_change_requested', t, hasEn)).toBe('Bank details change requested');
	});

	it('renders an unknown token raw', () => {
		expect(auditActionLabel('something.new_and_unlabelled', t, hasEn)).toBe('something.new_and_unlabelled');
	});
});
