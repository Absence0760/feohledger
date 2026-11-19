import { describe, expect, it } from 'vitest';

/**
 * Every animation on the public marketing surfaces stops on its own within
 * five seconds (decisions §279).
 *
 * That is how the landing page meets WCAG 2.2.2 Pause, Stop, Hide with no
 * pause control: the criterion only covers motion that starts automatically
 * AND lasts more than five seconds. The page used to loop its hero, aurora,
 * headline sheen and adapter marquee forever behind a "Pause animation"
 * button; the operator's call was to keep the motion and lose the control, so
 * the budget is what now carries the criterion. An `infinite` creeping back
 * in would put the page out of conformance with nothing to stop it.
 *
 * This is the static half of the guard: it reads each marketing component's
 * stylesheet and checks every `animation` declaration — a finite iteration
 * count, and `delay + duration × count ≤ 5s` wherever the timing is a literal
 * or a custom property the same file defines. A delay computed per element
 * (`calc(var(--i) * …)`) cannot be resolved without rendering, so the runtime
 * half — `tests-e2e/marketing/landing.spec.ts` § motion ends on its own —
 * reads `getComputedTiming().endTime` for every animation the page actually
 * runs.
 */

const RAW = import.meta.glob('/src/lib/components/marketing/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

/**
 * The pre-auth employee pages: the shell and its siblings, and every route
 * that renders inside it. Same budget, one documented exemption — a busy
 * spinner (`ui-patterns.md` § Motion, rule 4a; see {@link spinnerExempt}).
 */
const AUTH_RAW = import.meta.glob(
	[
		'/src/lib/components/auth/*.svelte',
		'/src/routes/{login,verify,signup,change-password,find-workspace}/**/*.svelte'
	],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

const BUDGET_S = 5;

type Finding = { file: string; decl: string; problem: string };

function styleOf(source: string): string {
	return [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
}

/** `--name: value;` custom properties declared anywhere in the stylesheet. */
function customProps(css: string): Map<string, string> {
	const props = new Map<string, string>();
	for (const m of css.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) props.set(m[1], m[2].trim());
	return props;
}

/** Seconds for a literal time or a `var(--x)` naming one; null when it needs rendering. */
function seconds(token: string, props: Map<string, string>): number | null {
	const v = token.match(/^var\((--[\w-]+)\)$/);
	if (v) {
		const resolved = props.get(v[1]);
		return resolved === undefined ? null : seconds(resolved, props);
	}
	const t = token.match(/^(-?\d*\.?\d+)(ms|s)$/);
	if (!t) return null;
	return Number(t[1]) / (t[2] === 'ms' ? 1000 : 1);
}

const isTime = (token: string) => /^(-?\d*\.?\d+)(ms|s)$|^var\(|^calc\(/.test(token);

/** Split on a top-level separator, keeping `calc(a + b)` / `cubic-bezier(…)` whole. */
function splitTop(value: string, sep: RegExp): string[] {
	const out: string[] = [];
	let depth = 0;
	let cur = '';
	for (const ch of value) {
		if (ch === '(') depth++;
		if (ch === ')') depth--;
		if (sep.test(ch) && depth === 0) {
			if (cur) out.push(cur);
			cur = '';
		} else cur += ch;
	}
	if (cur) out.push(cur);
	return out;
}
const tokens = (layer: string) => splitTop(layer, /\s/);

/** The selector of the innermost rule the declaration at `index` sits in. */
function selectorAt(css: string, index: number): string {
	const open = css.lastIndexOf('{', index);
	const start = Math.max(css.lastIndexOf('}', open), css.lastIndexOf('{', open - 1)) + 1;
	return css.slice(start, open).replace(/\/\*[\s\S]*?\*\//g, '').trim();
}

/**
 * A busy spinner may loop (rule 4a): it is the `.spinner` rule, and every
 * element carrying that class in the markup is mounted inside an `{#if}`, so
 * it exists only while the request it stands for is in flight and goes when
 * the answer lands. That is WCAG 2.2.2's own carve-out — the Understanding
 * text calls a progress animation during a phase the user is waiting on
 * essential, and it is user-started or the page's sole content, never
 * presented beside content it competes with. A spinner rendered
 * unconditionally would loop forever on a settled page, so it does not
 * qualify. Reduced motion stops it outright (app.css collapses every
 * iteration count to 1); the text beside it carries the meaning.
 */
function spinnerExempt(source: string, selector: string): boolean {
	if (selector !== '.spinner') return false;
	const markup = source.replace(/<style[^>]*>[\s\S]*?<\/style>/g, '').replace(/<script[^>]*>[\s\S]*?<\/script>/g, '');
	const uses = [...markup.matchAll(/class="spinner"/g)];
	if (uses.length === 0) return false;
	return uses.every((u) => {
		const before = markup.slice(0, u.index);
		const opened = (before.match(/\{#if\b/g) ?? []).length;
		const closed = (before.match(/\{\/if\}/g) ?? []).length;
		return opened > closed;
	});
}

function audit(file: string, source: string, allowSpinner = false): Finding[] {
	const css = styleOf(source);
	const props = customProps(css);
	const findings: Finding[] = [];

	for (const m of css.matchAll(/animation(-iteration-count|-duration|-delay)?\s*:\s*([^;]+);/g)) {
		const decl = m[0];
		const longhand = m[1];
		const value = m[2].trim();
		if (/\binfinite\b/.test(value)) {
			if (allowSpinner && spinnerExempt(source, selectorAt(css, m.index!))) continue;
			findings.push({ file, decl, problem: 'loops forever' });
			continue;
		}
		if (longhand || value === 'none') continue;

		for (const layer of splitTop(value, /,/)) {
			const parts = tokens(layer.trim());
			const times = parts.filter(isTime);
			// CSS's default iteration count is 1.
			const count = parts.find((p) => /^\d*\.?\d+$/.test(p)) ?? '1';
			const duration = times[0] === undefined ? null : seconds(times[0], props);
			if (duration === null) {
				findings.push({ file, decl, problem: 'duration is not a literal or a file-local property' });
				continue;
			}
			const delay = times[1] === undefined ? 0 : seconds(times[1], props);
			if (delay === null) continue; // per-element calc: measured by the e2e half
			const end = delay + duration * Number(count);
			if (end > BUDGET_S + 1e-9) {
				findings.push({ file, decl, problem: `ends at ${end.toFixed(2)}s, past the ${BUDGET_S}s budget` });
			}
		}
	}
	return findings;
}

describe('marketing motion budget (WCAG 2.2.2 without a pause control)', () => {
	const files = Object.entries(RAW).sort(([a], [b]) => a.localeCompare(b));

	it('scans the marketing components', () => {
		// A glob that silently matched nothing would make the guard pass by
		// scanning nothing.
		expect(files.map(([path]) => path.split('/').pop())).toEqual(
			expect.arrayContaining(['Atmosphere.svelte', 'HeroPipeline.svelte', 'Landing.svelte'])
		);
	});

	it('every animation is finite and ends within five seconds', () => {
		const findings = files.flatMap(([path, source]) => audit(path, source));
		expect(findings).toEqual([]);
	});

	it('the audit itself catches a loop and an over-long run', () => {
		const bad = `<style>
			.a { animation: spin 2s linear infinite; }
			.b { animation: drift 4s ease-in-out 0.5s 2 forwards; }
			.c { animation: fade 1s ease-out; }
			.d { --build: 4.5s; animation: pop 1s cubic-bezier(0.3, 1, 0.6, 1) var(--build) forwards; }
			.e { animation-iteration-count: infinite; }
		</style>`;
		expect(audit('bad.svelte', bad).map((f) => f.problem)).toEqual([
			'loops forever',
			'ends at 8.50s, past the 5s budget',
			'ends at 5.50s, past the 5s budget',
			'loops forever'
		]);
	});
});

describe('auth-shell motion budget (same rule, busy-spinner exemption)', () => {
	const files = Object.entries(AUTH_RAW).sort(([a], [b]) => a.localeCompare(b));

	it('scans the shell and the pages that render in it', () => {
		const paths = files.map(([path]) => path);
		for (const want of [
			'/src/lib/components/auth/AuthShell.svelte',
			'/src/routes/login/+page.svelte',
			'/src/routes/verify/+page.svelte',
			'/src/routes/signup/+page.svelte',
			'/src/routes/change-password/+page.svelte',
			'/src/routes/find-workspace/+page.svelte'
		]) {
			expect(paths).toContain(want);
		}
	});

	it('every animation is finite and ends within five seconds, bar a conditional spinner', () => {
		const findings = files.flatMap(([path, source]) => audit(path, source, true));
		expect(findings).toEqual([]);
	});

	it('the exemption covers only a `.spinner` mounted inside an {#if}', () => {
		const style = `<style>.spinner { animation: spin 0.8s linear infinite; }</style>`;
		const conditional = `{#if loading}<span class="spinner" aria-hidden="true"></span>{/if}${style}`;
		const always = `<span class="spinner" aria-hidden="true"></span>${style}`;
		const otherSelector = `{#if loading}<span class="spinner"></span>{/if}<style>.glow { animation: pulse 1s infinite; }</style>`;
		expect(audit('ok.svelte', conditional, true)).toEqual([]);
		expect(audit('always.svelte', always, true).map((f) => f.problem)).toEqual(['loops forever']);
		expect(audit('glow.svelte', otherSelector, true).map((f) => f.problem)).toEqual(['loops forever']);
		// The marketing surfaces get no exemption at all.
		expect(audit('marketing.svelte', conditional).map((f) => f.problem)).toEqual(['loops forever']);
	});

	it('reduced motion still stops the spinner (app.css)', () => {
		const appCss = Object.values(
			import.meta.glob('/src/app.css', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
		)[0];
		const block = appCss.match(/@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
		expect(block).toMatch(/animation-iteration-count:\s*1\s*!important/);
	});
});
