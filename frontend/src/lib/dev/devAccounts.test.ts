import { describe, expect, it } from 'vitest';
import { devAccountsFor } from './devAccounts.ts';

describe('devAccountsFor', () => {
	it('lists the seeded acme and e2e accounts', () => {
		expect(devAccountsFor('acme').map((a) => a.email)).toContain('demo@acme.com');
		expect(devAccountsFor('e2e2').map((a) => a.email)).toContain('demo+admin@e2e2.localhost');
	});

	it('offers the admin of an erptest-<erp> workspace (issue #654)', () => {
		expect(devAccountsFor('erptest-sage-uk')).toEqual([
			{ email: 'admin@erptest-sage-uk.localhost', password: 'demo', role: 'admin' }
		]);
	});

	it('offers nothing for any other workspace or no workspace', () => {
		expect(devAccountsFor('sandbox')).toEqual([]);
		expect(devAccountsFor('erptest')).toEqual([]);
		expect(devAccountsFor(null)).toEqual([]);
	});
});
