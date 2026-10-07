import { describe, expect, it } from 'vitest';
import { replayFor, type HeldNavigation } from './unsavedChanges.ts';

const ORIGIN = 'http://acme.localhost:7777';
const nav = (over: Partial<HeldNavigation>): HeldNavigation => ({
	type: 'link',
	to: { url: new URL('/invoices', ORIGIN) },
	willUnload: false,
	...over,
});

describe('replayFor', () => {
	it('replays an in-app link as a client-side goto to the same URL', () => {
		expect(replayFor(nav({}), ORIGIN)).toEqual({ kind: 'goto', href: `${ORIGIN}/invoices` });
	});

	it('replays Back/Forward as the same history step, not a new entry', () => {
		expect(replayFor(nav({ type: 'popstate', delta: -1 }), ORIGIN)).toEqual({
			kind: 'history',
			delta: -1,
		});
	});

	it('replays a navigation that leaves the app as a document load', () => {
		const out = nav({ to: { url: new URL('https://example.com/x') }, willUnload: true });
		expect(replayFor(out, ORIGIN)).toEqual({ kind: 'document', href: 'https://example.com/x' });
		const crossOrigin = nav({ to: { url: new URL('https://other.example/y') } });
		expect(replayFor(crossOrigin, ORIGIN)?.kind).toBe('document');
	});

	it('leaves reload / tab close to the browser prompt — nothing to replay', () => {
		expect(replayFor(nav({ type: 'leave', to: null, willUnload: true }), ORIGIN)).toBeNull();
	});

	it('has nothing to replay without a destination', () => {
		expect(replayFor(nav({ to: null }), ORIGIN)).toBeNull();
	});
});
