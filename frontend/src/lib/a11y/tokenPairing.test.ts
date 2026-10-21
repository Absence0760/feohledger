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

describe('style token pairing', () => {
	it('scans a realistic number of stylesheets — the walk itself must not silently break', () => {
		expect(appCss, 'src/app.css must be discoverable by the walk').toBeDefined();
		expect(Object.keys(palettes.dark).length).toBeGreaterThanOrEqual(8);
		expect(sources.length).toBeGreaterThan(100);
	});

	for (const theme of PALETTE_THEMES) {
		it(`[${theme}] has no colour pair below the WCAG AA bar, no stale fallback, and no dead token`, () => {
			const findings = auditStyles(sources, {
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

/**
 * The marketing landing page follows the theme like every other surface
 * (docs/decisions.md §306, superseding §289's dark pin). It is held to both
 * audits above with no exemption; these checks keep it there.
 *
 * The audit only measures pairs it can resolve, and a colour typed as a
 * literal is a pair it measures in one palette at best: `rgba(24, 26, 35, …)`
 * was a navy glass panel in both themes. So the marketing components may name
 * no colour literal at all except `#fff` — the text on a `-strong` fill, which
 * is white in both palettes by contract — and the `#000` of a mask, which is
 * alpha, not colour.
 */
describe('marketing landing page follows the theme', () => {
	const MARKETING = files.filter((f) => f.path.startsWith('lib/components/marketing/'));

	it('finds the marketing components', () => {
		expect(MARKETING.map((f) => f.path)).toEqual(
			expect.arrayContaining([
				'lib/components/marketing/Landing.svelte',
				'lib/components/marketing/Pricing.svelte',
				'lib/components/marketing/HeroPipeline.svelte',
				'lib/components/marketing/AdapterRail.svelte',
				'lib/components/marketing/Atmosphere.svelte'
			])
		);
	});

	it('nothing pins a subtree to one palette', () => {
		const pinned = files.filter((f) => /data-theme-scope/.test(f.source)).map((f) => f.path);
		expect(pinned, 'a palette pin exempts its subtree from the visitor\'s theme').toEqual([]);
	});

	/**
	 * Any way of typing a colour that is not a token: hex, the functional
	 * notations old and new, and the two named colours that mean "a literal
	 * tuned for one ground" in practice. `transparent` and `currentColor` are
	 * not colours of a palette and stay legal.
	 */
	const COLOUR_LITERAL =
		/#[0-9a-f]{3,8}\b|(?<![-\w])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color)\(|(?<![-\w])(?:white|black)(?![-\w])/gi;

	/** `{ … }` around `index`: the declarations of the rule it sits in. */
	const ruleAround = (css: string, index: number) =>
		css.slice(css.lastIndexOf('{', index) + 1, css.indexOf('}', index));
	/** The one declaration `index` sits in, however many lines it spans. */
	const declarationAround = (css: string, index: number) =>
		css.slice(Math.max(css.lastIndexOf(';', index), css.lastIndexOf('{', index)) + 1, css.indexOf(';', index));

	it('the marketing components type no colour literal', () => {
		const MARKETING_PATH = 'lib/components/marketing/';
		const found: string[] = [];
		const scan = (path: string, css: string) => {
			const clean = css.replace(/\/\*[\s\S]*?\*\//g, '');
			for (const match of clean.matchAll(COLOUR_LITERAL)) {
				const at = match[0].toLowerCase();
				const decl = declarationAround(clean, match.index!);
				// White text on a `-strong` fill: white in both palettes by the
				// palette's own contract (`palette contract` below). Only there.
				if (
					(at === '#fff' || at === '#ffffff') &&
					/^\s*color\s*:/.test(decl) &&
					/background(?:-color)?\s*:\s*var\(--[a-z-]+-strong\)/.test(ruleAround(clean, match.index!))
				) {
					continue;
				}
				// A mask's black is alpha, not colour.
				if (at === '#000' && /^\s*mask(?:-image)?\s*:/.test(decl)) continue;
				found.push(`${path}: ${decl.trim().replace(/\s+/g, ' ')}`);
			}
		};
		for (const { path, css } of sources.filter((s) => s.path.startsWith(MARKETING_PATH))) scan(path, css);
		// Inline `style="…"` in the markup is a stylesheet the block scan never sees.
		for (const { path, source } of MARKETING) {
			const markup = source.replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
			for (const attr of markup.matchAll(/\sstyle="([^"]*)"/g)) scan(`${path} (style=)`, `{${attr[1]};}`);
		}
		expect(found, `\n${found.join('\n')}\n`).toEqual([]);
	});

	it('the literal scan catches what it claims to', () => {
		// A guard that can't fail proves nothing; feed it each shape it names.
		const probe = (css: string) =>
			[...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(COLOUR_LITERAL)].map((m) => m[0]);
		for (const bad of ['#7d9bff', 'rgba(1,2,3,.5)', 'oklch(70% .1 250)', 'color(srgb 1 0 0)', 'white', 'hsl(0 0% 0%)']) {
			expect(probe(`.x { background: ${bad}; }`), bad).not.toEqual([]);
		}
		expect(
			probe(
				'.x { color: var(--text); white-space: nowrap; background: color-mix(in srgb, var(--accent) 20%, transparent); }'
			)
		).toEqual([]);
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

	// --gilt and --brand-violet are gradient stops on the landing page's
	// headline and figures (background-clip: text), which the rule scan cannot
	// measure — so the stops themselves are held to the body-text bar, which
	// makes any gradient through them legible end to end (decisions §306).
	for (const token of [
		'--text',
		'--text-muted',
		'--accent',
		'--success',
		'--danger',
		'--warning',
		'--accent-on-tint',
		'--gilt',
		'--brand-violet'
	]) {
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
