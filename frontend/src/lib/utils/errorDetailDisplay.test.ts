import { describe, expect, it } from 'vitest';

/**
 * Source-scan ratchet: no file builds display text from a caught error's
 * `.detail`.
 *
 * `ApiError.detail` is the RAW response field, kept for a caller that needs its
 * structure (signup's 422 list routing). For a coded refusal it is a
 * `{code, message, params}` object — and the shared rate limiter's 429
 * `rate_limited`, vendor merge and signup all became coded. Nine toasts built
 * as `e?.detail ?? e?.message ?? fallback` (behind a cast that claimed
 * `detail?: string`) then rendered `"[object Object]"`, and a plain-string
 * detail rendered the server's English instead of the localized message.
 * `ApiError.message` is the display sentence; `utils/apiError.ts::errorMessage`
 * is the one way to read it with a fallback.
 *
 * Read through Vite's `import.meta.glob`, like `i18n/untranslatedCopy.test.ts`
 * (the frontend carries no `@types/node`).
 */

const RAW = import.meta.glob(['/src/**/*.svelte', '/src/**/*.ts', '!/src/**/*.test.ts'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * Identifiers whose `.detail` is display text by contract, not a caught error's
 * raw body: an SSE `error` frame from the assistant / cash-flow stream carries a
 * server-sent sentence (`api.ts::streamAssistantChat`).
 */
const ALLOWED_OWNERS = new Set(['frame']);

/** Shapes that turn a `.detail` into text. Group 1 is the owning identifier. */
const PATTERNS: { name: string; re: RegExp }[] = [
	{ name: '`x.detail ??` / `x.detail ||` fallback chain', re: /(\w+)\??\.detail\s*(?:\?\?|\|\|)/g },
	{ name: '`String(x.detail)`', re: /String\(\s*(\w+)\??\.detail\s*\)/g },
	{ name: 'template `${x.detail}`', re: /\$\{\s*(\w+)\??\.detail\s*\}/g },
	{ name: '`toast(… x.detail …)`', re: /toast\([^;]*?\b(\w+)\??\.detail\b/g },
	// The cast that made the bug typecheck: a caught `unknown` claimed to carry
	// a string `detail`. Owner is the cast itself, never allowed.
	{ name: 'cast claiming `detail?: string`', re: /as\s*\{[^}]*\bdetail\?:\s*string()/g }
];

/** Drop comments so prose that names the anti-pattern is not a hit. */
function stripComments(src: string): string {
	return src
		.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
		.replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');
}

function findViolations(files: Record<string, string>): string[] {
	const hits: string[] = [];
	for (const [path, raw] of Object.entries(files)) {
		const src = stripComments(raw);
		for (const { name, re } of PATTERNS) {
			for (const match of src.matchAll(re)) {
				const owner = match[1];
				if (owner && ALLOWED_OWNERS.has(owner)) continue;
				const line = src.slice(0, match.index).split('\n').length;
				hits.push(`${path}:${line} — ${name}: ${match[0].trim()}`);
			}
		}
	}
	return hits;
}

describe('no display text from a caught error’s raw .detail', () => {
	it('scans a real tree', () => {
		expect(Object.keys(RAW).length).toBeGreaterThan(100);
	});

	it('no src file reads `.detail` as message text — use errorMessage(err, fallback)', () => {
		expect(findViolations(RAW)).toEqual([]);
	});

	it('catches each shape it claims to (so a green run means something)', () => {
		const bad = {
			'a.svelte': "toast(e?.detail ?? e?.message ?? m('x'), 'error');",
			'b.ts': 'const e = err as { detail?: string; message?: string } | null;',
			'c.ts': 'msg = String(err.detail);',
			'd.ts': 'text = `failed: ${error.detail}`;',
			'e.ts': "return err.detail || 'fallback';"
		};
		expect(findViolations(bad)).toHaveLength(6);
	});

	it('leaves structured reads, SSE frames and comments alone', () => {
		const ok = {
			'a.svelte': "if (err.status === 422 && Array.isArray(err.detail)) { for (const r of err.detail) {} }",
			'b.svelte': "msg.error = frame.detail || m('assistant.error.generic');",
			'c.ts': '// never write e.detail ?? e.message here\nconst x = 1;',
			'd.ts': '/* toast(e.detail) */ const y = 2;',
			'e.svelte': 'onchange={(ev) => (value = ev.detail.value)}'
		};
		expect(findViolations(ok)).toEqual([]);
	});
});
