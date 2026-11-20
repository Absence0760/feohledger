import { describe, expect, it } from 'vitest';
import { tenantHostRoute, type TenantHostState } from './tenantHostRoute.ts';

const SIGNED_OUT: TenantHostState = {
	path: '/',
	search: '',
	loggedIn: false,
	userLoaded: false,
	mustChangePassword: false
};
const SIGNED_IN: TenantHostState = { ...SIGNED_OUT, loggedIn: true, userLoaded: true };

describe('tenantHostRoute', () => {
	// The race this module exists to end: the page's own redirect to `/` and
	// the auth wall's `/login?next=/find-workspace` both fired. One answer now.
	it('sends a signed-out /find-workspace straight to /login, with no next back to it', () => {
		expect(tenantHostRoute({ ...SIGNED_OUT, path: '/find-workspace' })).toEqual({
			kind: 'goto',
			to: '/login'
		});
	});

	it('sends a signed-in /find-workspace home, loaded account or not', () => {
		expect(tenantHostRoute({ ...SIGNED_IN, path: '/find-workspace' })).toEqual({
			kind: 'goto',
			to: '/'
		});
		expect(
			tenantHostRoute({ ...SIGNED_IN, userLoaded: false, path: '/find-workspace' })
		).toEqual({ kind: 'goto', to: '/' });
	});

	it('walls any other signed-out path, carrying it as next', () => {
		expect(tenantHostRoute({ ...SIGNED_OUT, path: '/invoices/42', search: '?tab=lines' })).toEqual({
			kind: 'goto',
			to: '/login?next=%2Finvoices%2F42%3Ftab%3Dlines'
		});
		expect(tenantHostRoute({ ...SIGNED_OUT, path: '/' })).toEqual({ kind: 'goto', to: '/login' });
	});

	it('leaves the sign-in steps alone when signed out', () => {
		expect(tenantHostRoute({ ...SIGNED_OUT, path: '/login' })).toEqual({ kind: 'stay' });
		expect(tenantHostRoute({ ...SIGNED_OUT, path: '/login/mfa' })).toEqual({ kind: 'stay' });
	});

	it('loads the account before deciding anything else', () => {
		expect(
			tenantHostRoute({ ...SIGNED_IN, userLoaded: false, path: '/invoices' })
		).toEqual({ kind: 'loadUser' });
	});

	it('forces the password change, carrying the page as next', () => {
		expect(
			tenantHostRoute({ ...SIGNED_IN, mustChangePassword: true, path: '/vendors' })
		).toEqual({ kind: 'goto', to: '/change-password?next=%2Fvendors' });
		expect(
			tenantHostRoute({ ...SIGNED_IN, mustChangePassword: true, path: '/change-password' })
		).toEqual({ kind: 'stay' });
	});

	it('stays put for a signed-in user on an app page', () => {
		expect(tenantHostRoute({ ...SIGNED_IN, path: '/invoices' })).toEqual({ kind: 'stay' });
	});
});
