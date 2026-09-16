import { describe, it, expect } from 'vitest';
import {
	accentStrongContrast,
	accentStrongMeetsAA,
	isValidHexColor,
	brandThemeVars,
	brandMark,
	type Brand
} from './brandTheme';

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

	it('emits --accent only when accent_color is a valid hex', () => {
		expect(brandThemeVars(makeBrand({ accent_color: '#112233' }))).toEqual({
			'--accent': '#112233'
		});
	});

	it('emits --accent-strong only when accent_strong_color is a valid hex', () => {
		expect(brandThemeVars(makeBrand({ accent_strong_color: '#0a1622' }))).toEqual({
			'--accent-strong': '#0a1622'
		});
	});

	it('emits both when both are valid', () => {
		expect(
			brandThemeVars(makeBrand({ accent_color: '#abc', accent_strong_color: '#def' }))
		).toEqual({ '--accent': '#abc', '--accent-strong': '#def' });
	});

	it('omits an invalid color so the app.css default is kept', () => {
		// A malformed accent must NOT be written — the default token wins.
		expect(brandThemeVars(makeBrand({ accent_color: 'not-a-color' }))).toEqual({});
		expect(
			brandThemeVars(makeBrand({ accent_color: '#112233', accent_strong_color: 'bogus' }))
		).toEqual({ '--accent': '#112233' });
	});

	it('trims whitespace around a valid color', () => {
		expect(brandThemeVars(makeBrand({ accent_color: '  #112233  ' }))).toEqual({
			'--accent': '#112233'
		});
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
