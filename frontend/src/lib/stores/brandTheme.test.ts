import { describe, it, expect } from 'vitest';
import { contrastRatio, parseColor, WCAG_AA_NORMAL, type Rgb } from '#lib/a11y/contrast.ts';
import { parsePalette, resolveColorValue } from '#lib/a11y/cssAudit.ts';
import {
	ACCENT_TEXT_SURFACES,
	accentStrongContrast,
	accentStrongMeetsAA,
	accentTextContrast,
	isValidHexColor,
	brandThemeVars,
	brandMark,
	THEME_NAMES,
	themeAccent,
	type Brand,
	type ThemeName
} from './brandTheme';

const appCss = (
	import.meta.glob('/src/app.css', { query: '?raw', import: 'default', eager: true }) as Record<
		string,
		string
	>
)['/src/app.css'];

function makeBrand(overrides: Partial<Brand> = {}): Brand {
	return {
		product_name: '',
		logo_url: '',
		accent_color: '',
		accent_strong_color: '',
		support_url: '',
		legal_url: '',
		...overrides
	};
}

describe('isValidHexColor', () => {
	it('accepts 6- and 3-digit hex', () => {
		expect(isValidHexColor('#638cff')).toBe(true);
		expect(isValidHexColor('#abc')).toBe(true);
		expect(isValidHexColor('  #ABCDEF  ')).toBe(true); // trimmed
	});

	it('rejects non-hex / wrong-length / missing-hash', () => {
		expect(isValidHexColor('638cff')).toBe(false);
		expect(isValidHexColor('#zzzzzz')).toBe(false);
		expect(isValidHexColor('#1234')).toBe(false);
		expect(isValidHexColor('red')).toBe(false);
		expect(isValidHexColor('rgb(1,2,3)')).toBe(false);
		expect(isValidHexColor('')).toBe(false);
		expect(isValidHexColor(null)).toBe(false);
		expect(isValidHexColor(undefined)).toBe(false);
	});
});

describe('brandThemeVars (fallback logic)', () => {
	it('returns no vars when nothing is configured (defaults stand)', () => {
		expect(brandThemeVars(makeBrand())).toEqual({});
	});

	it('emits one accent per theme, each the colour shown in that theme', () => {
		// #c0392b reads on Light as it is (4.99:1 on --bg) but not on Dark.
		expect(brandThemeVars(makeBrand({ accent_color: '#c0392b' }))).toEqual({
			'--brand-accent-light': '#c0392b',
			'--brand-accent-dark': themeAccent('#c0392b', 'dark')
		});
		expect(themeAccent('#c0392b', 'dark')).not.toBe('#c0392b');
	});

	it('never writes --accent inline: an inline value would beat both palettes', () => {
		const vars = brandThemeVars(makeBrand({ accent_color: '#112233', accent_strong_color: '#000' }));
		expect(Object.keys(vars)).not.toContain('--accent');
	});

	it('emits --accent-strong only when accent_strong_color is a valid hex, theme-independent', () => {
		expect(brandThemeVars(makeBrand({ accent_strong_color: '#0a1622' }))).toEqual({
			'--accent-strong': '#0a1622'
		});
	});

	it('omits an invalid color so the app.css default is kept', () => {
		// A malformed accent must NOT be written — the default token wins.
		expect(brandThemeVars(makeBrand({ accent_color: 'not-a-color' }))).toEqual({});
		expect(
			Object.keys(
				brandThemeVars(makeBrand({ accent_color: '#112233', accent_strong_color: 'bogus' }))
			).sort()
		).toEqual(['--brand-accent-dark', '--brand-accent-light']);
	});

	it('trims whitespace around a valid color', () => {
		expect(brandThemeVars(makeBrand({ accent_color: '  #112233  ' }))['--brand-accent-light']).toBe(
			'#112233'
		);
	});
});

/** HSL hue in degrees, for the "keeps its hue" assertions. */
function hue({ r, g, b }: Rgb): number {
	const max = Math.max(r, g, b);
	const min = Math.min(r, g, b);
	const d = max - min;
	if (d === 0) return 0;
	let h: number;
	if (max === r) h = ((g - b) / d) % 6;
	else if (max === g) h = (b - r) / d + 2;
	else h = (r - g) / d + 4;
	return (h * 60 + 360) % 360;
}

/** The binding (lower) text ratio of a colour on a theme's --bg and --surface. */
function worst(color: string, theme: ThemeName): number {
	return Math.min(
		...Object.values(ACCENT_TEXT_SURFACES[theme]).map((s) => contrastRatio(color, s) as number)
	);
}

/**
 * The derivation behind a white-label accent (docs/decisions.md §477): one
 * brand colour, shown per theme as itself when it reads as text there, or as
 * the least-changed shade of its own hue that does.
 */
describe('themeAccent (per-theme accent derivation)', () => {
	it("measures the same surfaces app.css declares, so a palette change can't strand it", () => {
		for (const theme of THEME_NAMES) {
			const palette = parsePalette(appCss, theme);
			expect(ACCENT_TEXT_SURFACES[theme]).toEqual({
				'--bg': palette['--bg'],
				'--surface': palette['--surface']
			});
		}
	});

	it("leaves the platform's own accent unchanged in its own theme", () => {
		for (const theme of THEME_NAMES) {
			const palette = parsePalette(appCss, theme);
			const platform = resolveColorValue(palette['--accent'], palette);
			expect(platform, `${theme} --accent resolves`).not.toBeNull();
			expect(themeAccent(platform, theme)).toBe(platform);
		}
	});

	it('returns a colour already passing unchanged, as typed (trimmed)', () => {
		expect(themeAccent('#c0392b', 'light')).toBe('#c0392b');
		expect(themeAccent('  #abc ', 'dark')).toBe('#abc');
		expect(themeAccent('#ffe066', 'dark')).toBe('#ffe066');
	});

	const FAILING: [string, ThemeName][] = [
		['#638cff', 'light'], // the platform's dark accent on a light page: 2.86:1
		['#ffe066', 'light'],
		['#00ff00', 'light'],
		['#c0392b', 'dark'],
		['#2c4fc4', 'dark'], // the platform's light accent on a dark page
		['#112233', 'dark']
	];
	for (const [color, theme] of FAILING) {
		it(`adjusts ${color} on ${theme} to ≥ 4.5:1, keeping its hue, without overshooting`, () => {
			expect(worst(color, theme)).toBeLessThan(WCAG_AA_NORMAL);
			const applied = themeAccent(color, theme) as string;
			expect(applied).toMatch(/^#[0-9a-f]{6}$/);
			const ratio = worst(applied, theme);
			expect(ratio).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
			// The least change that passes, not "just use black/white".
			expect(ratio).toBeLessThan(WCAG_AA_NORMAL + 0.15);
			expect(Math.abs(hue(parseColor(applied) as Rgb) - hue(parseColor(color) as Rgb))).toBeLessThan(
				2
			);
			// Direction: darker for Light, lighter for Dark.
			const sum = (c: string) => {
				const { r, g, b } = parseColor(c) as Rgb;
				return r + g + b;
			};
			if (theme === 'light') expect(sum(applied)).toBeLessThan(sum(color));
			else expect(sum(applied)).toBeGreaterThan(sum(color));
		});
	}

	it('handles the extremes: black and near-black on Dark, white and near-white on Light', () => {
		for (const color of ['#000000', '#000', '#0a0a0a']) {
			expect(themeAccent(color, 'light')).toBe(color);
			const dark = themeAccent(color, 'dark') as string;
			expect(worst(dark, 'dark')).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
		}
		for (const color of ['#ffffff', '#fff', '#fffff0']) {
			expect(themeAccent(color, 'dark')).toBe(color);
			const light = themeAccent(color, 'light') as string;
			expect(worst(light, 'light')).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
		}
		// A colour lighter than --bg passes through ratio 1 on the way down; the
		// search must still land on the passing side.
		expect(worst(themeAccent('#fafbff', 'light') as string, 'light')).toBeGreaterThanOrEqual(
			WCAG_AA_NORMAL
		);
	});

	it('returns null for anything that is not a usable hex', () => {
		for (const value of ['', '  ', '#12', 'red', null, undefined]) {
			expect(themeAccent(value, 'light'), String(value)).toBeNull();
			expect(themeAccent(value, 'dark'), String(value)).toBeNull();
		}
	});
});

/** The /organization advisory for `accent_color` as text, in both themes. */
describe('accentTextContrast', () => {
	it('reports both themes, naming the binding surface and what is shown', () => {
		const report = accentTextContrast('#c0392b');
		expect(report).not.toBeNull();
		const { light, dark } = report!;
		expect(light.meetsAA).toBe(true);
		expect(light.surface).toBe('--bg');
		expect(light.ratio).toBeCloseTo(4.99, 2);
		expect(light.applied).toBe('#c0392b');
		expect(light.appliedRatio).toBe(light.ratio);

		expect(dark.meetsAA).toBe(false);
		expect(dark.surface).toBe('--surface');
		expect(dark.ratio).toBeCloseTo(3.19, 2);
		expect(dark.applied).toBe(themeAccent('#c0392b', 'dark'));
		expect(dark.appliedRatio).toBeGreaterThanOrEqual(WCAG_AA_NORMAL);
	});

	it('flags the light theme for a colour picked to read on dark', () => {
		const { light, dark } = accentTextContrast('#638cff')!;
		expect(dark.meetsAA).toBe(true);
		expect(light.meetsAA).toBe(false);
		expect(light.ratio).toBeCloseTo(2.86, 2);
	});

	it('returns null while the field holds no usable hex — no warning flash', () => {
		for (const value of ['', '#12', 'rebeccapurple', null, undefined]) {
			expect(accentTextContrast(value), String(value)).toBeNull();
		}
	});
});

/**
 * `--accent-strong` exists so white text has somewhere legible to sit, and
 * `brandThemeVars` hands the tenant's raw hex straight to it. The static
 * token-pairing guard can't see that override, so this is the runtime half of
 * the same check.
 */
describe('accentStrongContrast / accentStrongMeetsAA', () => {
	it('reports the white-on-colour ratio for the shipped default', () => {
		// app.css --accent-strong: #3f5fd6
		expect(accentStrongContrast('#3f5fd6') as number).toBeCloseTo(5.5, 1);
		expect(accentStrongMeetsAA('#3f5fd6')).toBe(true);
	});

	it('fails a brand colour too light to carry white text', () => {
		// A logo yellow is the realistic bad case, not a contrived one.
		expect(accentStrongMeetsAA('#ffe066')).toBe(false);
		// And the plain --accent value, which is exactly why the strong
		// companion exists.
		expect(accentStrongContrast('#638cff') as number).toBeCloseTo(3.12, 2);
		expect(accentStrongMeetsAA('#638cff')).toBe(false);
	});

	it('accepts 3-digit hex and surrounding whitespace, like the validator', () => {
		expect(accentStrongMeetsAA('  #000  ')).toBe(true);
	});

	/**
	 * Null, not false — a half-typed or empty field must not flash a warning,
	 * and "not a colour" is a different state from "fails".
	 */
	it('returns null when there is nothing to judge', () => {
		for (const value of ['', '   ', '#12', 'rebeccapurple', null, undefined]) {
			expect(accentStrongContrast(value), String(value)).toBeNull();
			expect(accentStrongMeetsAA(value), String(value)).toBeNull();
		}
	});
});

/**
 * The fallback used to be a neutral "AP" placeholder, so a tenant that renamed
 * the product but set no logo never displayed platform branding. The platform
 * mark is FeohLedger's identity, so it may only stand in for the platform's own
 * name (docs/decisions.md §173).
 */
describe('brandMark', () => {
	it('shows the platform mark when the org has configured nothing', () => {
		expect(brandMark(makeBrand())).toEqual({ kind: 'platform' });
		expect(brandMark(makeBrand({ product_name: '   ' }))).toEqual({ kind: 'platform' });
	});

	it('keeps the platform mark while the product name is the platform default', () => {
		expect(brandMark(makeBrand({ product_name: 'FeohLedger' }))).toEqual({ kind: 'platform' });
	});

	it('prefers a configured logo over everything, trimmed', () => {
		const brand = makeBrand({
			product_name: 'Acme Payables',
			logo_url: '  https://cdn.acme.test/logo.svg '
		});
		expect(brandMark(brand)).toEqual({ kind: 'logo', src: 'https://cdn.acme.test/logo.svg' });
	});

	it("gives a renamed tenant without a logo its own initial, never the platform's mark", () => {
		expect(brandMark(makeBrand({ product_name: 'acme payables' }))).toEqual({
			kind: 'monogram',
			letter: 'A'
		});
		expect(brandMark(makeBrand({ product_name: '  Östgöta AP' }))).toEqual({
			kind: 'monogram',
			letter: 'Ö'
		});
	});

	it('takes a whole character, not half of one', () => {
		// "é" typed as e + a combining acute is two code points and one character.
		expect(brandMark(makeBrand({ product_name: 'e\u0301tude' }))).toEqual({
			kind: 'monogram',
			letter: 'E\u0301'
		});
		expect(brandMark(makeBrand({ product_name: '👩‍💼 Payables' }))).toEqual({
			kind: 'monogram',
			letter: '👩‍💼'
		});
	});
});
