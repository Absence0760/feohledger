import { describe, expect, it } from 'vitest';
import {
	compositeOver,
	contrastRatio,
	formatRatio,
	parseColor,
	parseColorWithAlpha,
	type Rgb,
	type RgbaParts,
	WCAG_AA_NORMAL
} from './contrast';
import {
	auditStyles,
	collectAssignedTokens,
	describeFinding,
	extractStyleBlocks,
	lightPaletteBlocks,
	PALETTE_THEMES,
	parsePalette,
	type PaletteTheme,
	type StyleSource
} from './cssAudit';

/**
 * Repo-wide token-pairing guard (WCAG 2.2 SC 1.4.3).
 *
 * The complement to `tests-e2e/a11y/axe.spec.ts`: axe checks what a listed
 * route happens to render, this checks every stylesheet in the app. The
 * contrast bug this initiative kept re-finding (`--text-muted` on
 * `--surface-2`, 4.34:1) recurs per *surface*, so it appeared identically on
 * pages inside the axe route list and pages outside it. A scan of the sources
 * finds all of them at once, including markup behind a modal, an empty state,
 * or a role the e2e user doesn't hold.
 *
 * **Every check runs once per theme** (docs/decisions.md §289). The app has a
 * dark and a light palette, and a pair that clears the bar in one can fail in
 * the other — a literal red chosen to read on navy is a different colour on
 * white. A light theme that only the dark guard watched would be a theme
 * nobody checks.
 *
 * **Fixing a failure means changing the colour, never relaxing the rule.**
 * The palette carries a "strong" companion for every text-bearing fill
 * (`--accent-strong`, `--success-strong`, `--danger-strong`) precisely so
 * there is always a correct answer.
 */

/**
 * Vite's `import.meta.glob` rather than a `node:fs` walk: it needs no
 * `@types/node` (the frontend has none, deliberately), it is resolved by the
 * same bundler that ships these files, and a pattern that matches nothing
 * fails the size assertion below rather than silently passing.
 */
const RAW = import.meta.glob('/src/**/*.{svelte,css}', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const appCss = files.find((f) => f.path === 'app.css');
const palettes: Record<PaletteTheme, Record<string, string>> = {
	dark: parsePalette(appCss?.source ?? '', 'dark'),
	light: parsePalette(appCss?.source ?? '', 'light')
};

const assignedTokens = new Set<string>();
for (const file of files) for (const t of collectAssignedTokens(file.source)) assignedTokens.add(t);

const sources: StyleSource[] = files.flatMap((f) => extractStyleBlocks(f.path, f.source));

/**
 * Files that only ever render inside the marketing landing page's
 * `data-theme-scope="dark"` subtree, so the light palette never reaches them
 * (docs/decisions.md §289). They are held to the DARK audit like everything
 * else and skipped by the light one — and the `dark-pinned subtree` block
 * below proves the premise, so a component cannot be added here and then
 * rendered somewhere the light theme applies.
 */
const DARK_PINNED_ROOT = 'lib/components/marketing/Landing.svelte';
const DARK_PINNED = new Set([
	DARK_PINNED_ROOT,
	'lib/components/marketing/Pricing.svelte',
	'lib/components/marketing/HeroPipeline.svelte',
	'lib/components/marketing/AdapterRail.svelte'
]);
const sourcesFor = (theme: PaletteTheme) =>
	theme === 'dark' ? sources : sources.filter((s) => !DARK_PINNED.has(s.path));

describe('style token pairing', () => {
	it('scans a realistic number of stylesheets — the walk itself must not silently break', () => {
		expect(appCss, 'src/app.css must be discoverable by the walk').toBeDefined();
		expect(Object.keys(palettes.dark).length).toBeGreaterThanOrEqual(8);
		expect(sources.length).toBeGreaterThan(100);
	});

	for (const theme of PALETTE_THEMES) {
		it(`[${theme}] has no colour pair below the WCAG AA bar, no stale fallback, and no dead token`, () => {
			const findings = auditStyles(sourcesFor(theme), {
				palette: palettes[theme],
				assignedTokens,
				// The surfaces body text actually renders on. See `AuditOptions`
				// for why --surface-2 is not in this list.
				textSurfaces: ['--bg', '--surface']
			});
			const report = findings.map((f) => `  • ${describeFinding(f)}`).join('\n');
			expect(findings, `\n${report}\n`).toEqual([]);
		});
	}
});

describe('dark-pinned subtree', () => {
	it('the landing page root carries data-theme-scope="dark"', () => {
		const landing = files.find((f) => f.path === DARK_PINNED_ROOT);
		expect(landing, `${DARK_PINNED_ROOT} must exist`).toBeDefined();
		expect(landing!.source).toMatch(/<div class="landing" data-theme-scope="dark">/);
	});

	it('app.css redeclares the dark palette on [data-theme-scope=\'dark\']', () => {
		expect(appCss!.source).toMatch(/^:root,\s*(?:\/\*[\s\S]*?\*\/\s*)?\[data-theme-scope='dark'\] \{/m);
	});

	it('every other pinned component is imported only from inside the pinned set', () => {
		for (const pinned of DARK_PINNED) {
			if (pinned === DARK_PINNED_ROOT) continue;
			const name = pinned.split('/').pop()!;
			const importers = files
				.filter((f) => f.path !== pinned && f.source.includes(`/${name}'`))
				.map((f) => f.path);
			expect(importers.length, `${name} must be imported somewhere`).toBeGreaterThan(0);
			const outside = importers.filter((p) => !DARK_PINNED.has(p));
			expect(
				outside,
				`${name} renders outside the dark-pinned landing page, so the light theme reaches ` +
					'it — take it out of DARK_PINNED and fix its light-theme findings'
			).toEqual([]);
		}
	});
});

/**
 * The light palette's own shape. The cascade is forgiving in exactly the wrong
 * way here: a token the light block forgets keeps rendering its DARK value, so
 * a light page would grow a navy patch with no error anywhere.
 */
describe('light palette coverage', () => {
	const { explicit, system } = lightPaletteBlocks(appCss?.source ?? '');

	it('declares the explicit Light block and the System copy, identically', () => {
		expect(explicit, ":root[data-theme='light'] must exist in app.css").not.toBeNull();
		expect(
			system,
			"@media (prefers-color-scheme: light) { :root:not([data-theme='dark']) { … } } must exist"
		).not.toBeNull();
		// CSS cannot share one block between the two selectors, so the copy is
		// unavoidable; this is what keeps it a copy.
		expect(system).toEqual(explicit);
	});

	it('sets color-scheme: light in both copies, so native controls follow the theme', () => {
		for (const block of [explicit, system]) {
			expect(Object.fromEntries(block ?? [])['color-scheme']).toBe('light');
		}
	});

	it('redeclares every colour token the dark palette declares', () => {
		// A token whose value carries a colour LITERAL. A token derived from
		// another (`--accent-glow: color-mix(… var(--accent) …)`) follows the
		// theme by itself and needs no copy; a font stack or a radius has no
		// colour at all.
		const colourLiteral = /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i;
		const lightDeclared = new Set((explicit ?? []).map(([name]) => name));
		const missing = Object.entries(palettes.dark)
			.filter(([, value]) => colourLiteral.test(value))
			.map(([name]) => name)
			.filter((name) => !lightDeclared.has(name));
		expect(missing, `tokens rendering their dark value on a light page: ${missing}`).toEqual([]);
	});
});

/**
 * The palette's own contract, asserted directly rather than inferred from
 * whichever rules happen to use it. Each "strong" token exists only to carry
 * white text; if one drifts light, every button using it fails at once and the
 * scan above would report dozens of sites for a single root cause.
 */
for (const theme of PALETTE_THEMES) describe(`palette contract [${theme}]`, () => {
	const palette = palettes[theme];
	for (const token of ['--accent-strong', '--success-strong', '--danger-strong']) {
		it(`${token} carries white text at AA`, () => {
			const value = palette[token];
			expect(value, `${token} must be declared in :root`).toBeDefined();
			const ratio = contrastRatio('#ffffff', value);
			expect(ratio, `white on ${token} (${value})`).not.toBeNull();
			expect(
				ratio as number,
				`white on ${token} (${value}) is ${formatRatio(ratio as number)}`
			).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
		});
	}

	for (const token of ['--text', '--text-muted', '--accent', '--success', '--danger', '--warning']) {
		it(`${token} is legible on --bg and --surface`, () => {
			const value = palette[token];
			expect(value, `${token} must be declared in :root`).toBeDefined();
			for (const surface of ['--bg', '--surface'] as const) {
				const ratio = contrastRatio(value, palette[surface]);
				expect(
					ratio as number,
					`${token} (${value}) on ${surface} (${palette[surface]}) is ${formatRatio(
						ratio as number
					)}`
				).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
			}
		});
	}

	/**
	 * The badge-tint pairs, asserted on the thing they actually promise: the
	 * `-on-tint` text, over its own tint, over each backdrop a badge sits on.
	 *
	 * The scan above only sees tints someone *used*. This sees the contract
	 * itself, so a tone stays honest even while no rule references it — and a
	 * failure names one token rather than the dozen sites that spelled it.
	 */
	const TINT_PAIRS = ['accent', 'success', 'warning', 'muted', 'danger', 'violet'] as const;
	for (const tone of TINT_PAIRS) {
		it(`--${tone}-on-tint is legible on --${tone}-tint over both backdrops`, () => {
			const tint = palette[`--${tone}-tint`];
			const text = palette[`--${tone}-on-tint`];
			expect(tint, `--${tone}-tint must be declared in :root`).toBeDefined();
			expect(text, `--${tone}-on-tint must be declared in :root`).toBeDefined();

			const parts = parseColorWithAlpha(tint);
			expect(parts, `--${tone}-tint (${tint}) must parse as a colour`).not.toBeNull();
			expect(
				(parts as RgbaParts).alpha,
				`--${tone}-tint is meant to be translucent — an opaque one needs no compositing`
			).toBeLessThan(1);

			for (const surface of ['--bg', '--surface'] as const) {
				const backdrop = parseColor(palette[surface]) as Rgb;
				const rendered = compositeOver(
					(parts as RgbaParts).color,
					backdrop,
					(parts as RgbaParts).alpha
				);
				const ratio = contrastRatio(text, rendered) as number;
				expect(
					ratio,
					`--${tone}-on-tint (${text}) on --${tone}-tint over ${surface} renders ` +
						`${formatRatio(ratio)}`
				).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
			}
		});
	}

	/**
	 * `--surface-2` is the app's most hostile surface: only `--text` clears
	 * 4.5:1 on it in the dark theme. Asserting that keeps the app.css note
	 * honest — if someone lightens the surface enough that `--text-muted`
	 * becomes legal there, the comment telling authors not to use it becomes
	 * wrong and this fails.
	 *
	 * The refusal is a DARK-theme fact. The light palette happens to admit
	 * muted text there (5.30:1), but a stylesheet serves both themes, so the
	 * dark refusal is the rule authors design to; the light theme only has to
	 * keep `--text` legible on it.
	 */
	it('--surface-2 admits --text', () => {
		expect(contrastRatio(palette['--text'], palette['--surface-2']) as number).toBeGreaterThanOrEqual(
			WCAG_AA_NORMAL
		);
	});
	if (theme === 'dark') {
		it('--surface-2 refuses --text-muted', () => {
			expect(contrastRatio(palette['--text-muted'], palette['--surface-2']) as number).toBeLessThan(
				WCAG_AA_NORMAL
			);
		});
	}
});
