import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { enSettings as en } from '#lib/i18n/locales/settings/en.ts';
import {
	WORKSPACE_CLOSE_BLOCKER_KEYS,
	blockerLabelKey,
	confirmationMatches,
	refusalBlockers
} from './workspaceClosure.ts';

describe('workspace closure blockers', () => {
	it('every blocker the backend can return has a sentence, and nothing more', () => {
		// The backend test reads THIS file for the reverse direction; here the
		// backend source is read so a code this table invents fails too.
		const source = readFileSync(
			new URL('../../../../backend/app/services/workspace_closure.py', import.meta.url),
			'utf8'
		);
		for (const code of Object.keys(WORKSPACE_CLOSE_BLOCKER_KEYS)) {
			expect(source, `backend has no blocker "${code}"`).toContain(`= "${code}"`);
		}
	});

	it('names a catalogued sentence for each code', () => {
		for (const key of Object.values(WORKSPACE_CLOSE_BLOCKER_KEYS)) {
			expect(en[key]).toBeTruthy();
		}
	});

	it('has no sentence for a code this build predates', () => {
		expect(blockerLabelKey('payments')).toBe('org.close.blocker.payments');
		expect(blockerLabelKey('a_newer_guard')).toBeNull();
		expect(blockerLabelKey('toString')).toBeNull();
	});
});

describe('refusalBlockers', () => {
	it('reads the list a refusal carries', () => {
		expect(refusalBlockers({ blockers: ['payments', 'other_users'] })).toEqual([
			'payments',
			'other_users'
		]);
	});

	it('refuses anything else rather than rendering a blank', () => {
		expect(refusalBlockers({})).toBeNull();
		expect(refusalBlockers({ blockers: [] })).toBeNull();
		expect(refusalBlockers({ blockers: 'payments' })).toBeNull();
		expect(refusalBlockers({ blockers: ['payments', ''] })).toBeNull();
	});
});

describe('confirmationMatches', () => {
	it('is the comparison the server makes', () => {
		expect(confirmationMatches('acme', 'acme')).toBe(true);
		expect(confirmationMatches('  acme ', 'acme')).toBe(true);
		expect(confirmationMatches('Acme', 'acme')).toBe(false);
		expect(confirmationMatches('acm', 'acme')).toBe(false);
		expect(confirmationMatches('', '')).toBe(false);
	});
});
