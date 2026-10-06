import { describe, expect, it } from 'vitest';
import { inline, plainText } from './inline.ts';
import { pageHelpFor, pageHelpHref } from './routeHelp.ts';
import { headingId } from './anchor.ts';

describe('inline()', () => {
	it('splits text and every markup kind, in order', () => {
		expect(
			inline('Click {ui:common.save}, see [[three-way-match]], [[void|voided]], [[guide:run-payments|runs]], [[page:/payments]] and [[page:/profile|your profile]] — **Execute** is *final*.')
		).toEqual([
			{ kind: 'text', text: 'Click ' },
			{ kind: 'ui', key: 'common.save' },
			{ kind: 'text', text: ', see ' },
			{ kind: 'term', id: 'three-way-match' },
			{ kind: 'text', text: ', ' },
			{ kind: 'term', id: 'void', label: 'voided' },
			{ kind: 'text', text: ', ' },
			{ kind: 'guide', id: 'run-payments', label: 'runs' },
			{ kind: 'text', text: ', ' },
			{ kind: 'page', href: '/payments' },
			{ kind: 'text', text: ' and ' },
			{ kind: 'page', href: '/profile', label: 'your profile' },
			{ kind: 'text', text: ' — ' },
			{ kind: 'strong', text: 'Execute' },
			{ kind: 'text', text: ' is ' },
			{ kind: 'em', text: 'final' },
			{ kind: 'text', text: '.' }
		]);
	});

	it('keeps a page link with a query string whole', () => {
		expect(inline('[[page:/admin?tab=users]]')).toEqual([{ kind: 'page', href: '/admin?tab=users' }]);
	});

	it('leaves plain text, and a lone asterisk, alone', () => {
		expect(inline('5 * 3 = 15')).toEqual([{ kind: 'text', text: '5 * 3 = 15' }]);
	});

	it('plainText reduces references to their words', () => {
		const words = plainText('Click {ui:common.save} on [[void]] or [[page:/payments]].', (p) =>
			p.kind === 'ui' ? 'Save' : p.kind === 'term' ? 'Void' : 'Payments'
		);
		expect(words).toBe('Click Save on Void or Payments.');
	});
});

describe('page help lookup', () => {
	it('finds a page by exact href, by prefix, and a tab by its query', () => {
		expect(pageHelpFor('/invoices')?.href).toBe('/invoices');
		expect(pageHelpFor('/workflows/abc-123')?.href).toBe('/workflows');
		expect(pageHelpFor('/vendors/screening')?.href).toBe('/vendors/screening');
		expect(pageHelpFor('/admin', '?tab=roles')?.href).toBe('/admin?tab=roles');
	});

	it("a tabbed page with no tab in the URL gets its first tab's help, like the sidebar", () => {
		expect(pageHelpFor('/admin')?.href).toBe('/admin?tab=users');
	});

	it('the dashboard is matched exactly, not as everyone’s prefix', () => {
		expect(pageHelpFor('/')?.href).toBe('/');
		expect(pageHelpFor('/no-such-page')).toBeUndefined();
	});

	it('links to the guide, or to the directory row when there is none', () => {
		expect(pageHelpHref('/invoices')).toMatch(/^\/help\/guides\/[a-z-]+$/);
		expect(pageHelpHref('/no-such-page')).toBeUndefined();
	});
});

describe('headingId', () => {
	it('slugs a heading, accents and punctuation included', () => {
		expect(headingId('Why an approval can be refused')).toBe('why-an-approval-can-be-refused');
		expect(headingId('Café — résumé & co.')).toBe('cafe-resume-co');
		expect(headingId('!!!')).toBe('section');
	});
});
