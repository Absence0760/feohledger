import { describe, expect, it } from 'vitest';

// The CloudFront Function that decides, per request, "file the build emitted"
// versus "SPA route → /index.html" (infra/cloudfront/spa-router.js,
// docs/decisions.md §278). It cannot ask S3 whether a file exists, so it names
// the files instead — and a list of files drifts. This runs the function's own
// source against what the build actually ships: every file under `static/`
// (copied verbatim to the build root) plus SvelteKit's `_app/` output.
//
// Files come through `import.meta.glob`, not `node:fs` (see
// socialPreviewCard.test.ts for why).

const SOURCE = Object.values(
	import.meta.glob('../../../../infra/cloudfront/spa-router.js', {
		query: '?raw',
		import: 'default',
		eager: true,
	}),
)[0] as string;

type Request = { uri: string; querystring?: Record<string, unknown> };
// CloudFront calls the top-level `handler` by name; the file has no exports.
const handler = new Function(`${SOURCE}\nreturn handler;`)() as (event: {
	request: Request;
}) => Request;

function route(uri: string): string {
	return handler({ request: { uri, querystring: {} } }).uri;
}

// Keys are '/static/<path>'; the build serves each at '/<path>'.
const STATIC_FILES = Object.keys(import.meta.glob('/static/**/*')).map((k) =>
	k.replace(/^\/static/, ''),
);

describe('spa-router (CloudFront viewer-request)', () => {
	it('has the static tree to check against', () => {
		expect(SOURCE).toContain('function handler');
		expect(STATIC_FILES).toContain('/favicon.svg');
	});

	it('passes every file the build copies from static/ through untouched', () => {
		const rewritten = STATIC_FILES.filter((f) => route(f) !== f);
		expect(
			rewritten,
			'these built files would be answered with index.html — add them to ROOT_FILES or FILE_PREFIXES in infra/cloudfront/spa-router.js',
		).toEqual([]);
	});

	it('lists no root file that static/ no longer ships', () => {
		const listed = SOURCE.match(/var ROOT_FILES = \[([\s\S]*?)\];/)?.[1] ?? '';
		const roots = [...listed.matchAll(/'([^']+)'/g)].map((m) => m[1]);
		expect(roots.length).toBeGreaterThan(0);
		const stale = roots.filter((r) => r !== '/index.html' && !STATIC_FILES.includes(r));
		expect(stale, 'ROOT_FILES entries with no file in frontend/static').toEqual([]);
	});

	it.each(['/_app/version.json', '/_app/immutable/entry/start.Bx1.js', '/_app/env.js', '/index.html'])(
		'passes SvelteKit output %s through',
		(uri) => {
			expect(route(uri)).toBe(uri);
		},
	);

	it.each([
		'/',
		'/login',
		'/invoices',
		'/legal/privacy',
		// A dot in a route segment is a route, not a file. The rule this
		// replaced ("no extension → index.html") 404s every one of these.
		'/invoices/INV-2026.001',
		'/vendors/acme.inc',
		'/portal/invite/jane.doe@example.com',
		'/robots.txt',
	])('rewrites the route %s to the SPA document', (uri) => {
		expect(route(uri)).toBe('/index.html');
	});

	it('leaves a missing asset under _app/ as a miss, so S3 can 404 it', () => {
		expect(route('/_app/immutable/chunks/deleted-in-this-release.js')).toBe(
			'/_app/immutable/chunks/deleted-in-this-release.js',
		);
	});

	it('does not touch the query string', () => {
		const qs = { filter: { value: 'open' } };
		const out = handler({ request: { uri: '/invoices', querystring: qs } });
		expect(out.querystring).toBe(qs);
	});
});
