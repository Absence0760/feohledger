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

function audit(file: string, source: string): Finding[] {
	const css = styleOf(source);
	const props = customProps(css);
	const findings: Finding[] = [];

	for (const m of css.matchAll(/animation(-iteration-count|-duration|-delay)?\s*:\s*([^;]+);/g)) {
		const decl = m[0];
		const longhand = m[1];
		const value = m[2].trim();
		if (/\binfinite\b/.test(value)) {
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
