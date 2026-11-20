import { describe, expect, it } from 'vitest';
import { compositeOver, parseColor, parseColorWithAlpha, type Rgb } from './contrast';
import {
	auditStyles,
	collectAssignedTokens,
	describeFinding,
	extractStyleBlocks,
	findVarReferences,
	isLargeText,
	parseDeclarations,
	parsePalette,
	parseRules,
	resolveColorValue,
	stripCssComments,
	type StyleFinding
} from './cssAudit';

const PALETTE = {
	'--bg': '#0f1117',
	// --surface is one of the two `textSurfaces` below, and the surface the
	// real `.kpi-sub` defect failed on — a fixture without it can't reproduce
	// the class at all, because --bg is darker and more forgiving.
	'--surface': '#181a23',
	'--surface-2': '#232b44',
	'--text': '#e2e4ea',
	'--text-muted': '#8a8fa0',
	'--accent': '#638cff',
	'--accent-strong': '#3f5fd6',
	// The badge-tint pair, so a fixture can exercise both the recipe that fails
	// (base token on its own tint) and the one that replaced it.
	'--accent-tint': 'rgba(99, 140, 255, 0.15)',
	'--accent-on-tint': '#7d9bff'
};

const audit = (css: string, assigned: string[] = []) =>
	auditStyles([{ path: 'fixture.css', css }], {
		palette: PALETTE,
		assignedTokens: new Set(assigned)
	});

/** The same audit with the bare-literal rule armed, as the repo guard runs it. */
const auditText = (css: string) =>
	auditStyles([{ path: 'fixture.css', css }], {
		palette: PALETTE,
		assignedTokens: new Set(),
		textSurfaces: ['--bg', '--surface']
	});

describe('stripCssComments', () => {
	it('removes block comments, including multi-line ones', () => {
		expect(stripCssComments('a{/* x\n y */color:red}')).toBe('a{color:red}');
	});

	/**
	 * app.css documents the failing pairs in prose right next to the tokens.
	 * If comments weren't stripped, the guard would flag its own explanation.
	 */
	it('keeps prose about a failing pair from parsing as CSS', () => {
		const css = `:root{ /* --text-muted on --surface-2 is 4.34:1 */ --text: #e2e4ea; }`;
		expect(parsePalette(css)).toEqual({ '--text': '#e2e4ea' });
	});
});

describe('extractStyleBlocks', () => {
	it('returns a .css file whole', () => {
		expect(extractStyleBlocks('app.css', 'a{color:red}')).toEqual([
			{ path: 'app.css', css: 'a{color:red}' }
		]);
	});

	it('returns every <style> block of a .svelte file and nothing else', () => {
		const source = `<div class="x">not css</div>\n<style>.a{color:red}</style>\n<style module>.b{color:blue}</style>`;
		expect(extractStyleBlocks('X.svelte', source).map((b) => b.css)).toEqual([
			'.a{color:red}',
			'.b{color:blue}'
		]);
	});

	it('returns nothing for a component with no styles', () => {
		expect(extractStyleBlocks('X.svelte', '<p>hi</p>')).toEqual([]);
	});
});

describe('parseDeclarations', () => {
	it('keeps a semicolon inside parentheses out of the split', () => {
		expect(parseDeclarations('background: url("a;b"); color: red')).toEqual([
			['background', 'url("a;b")'],
			['color', 'red']
		]);
	});

	it('preserves source order and lowercases only the property', () => {
		expect(parseDeclarations('Color: #FFF; color: #000')).toEqual([
			['color', '#FFF'],
			['color', '#000']
		]);
	});
});

describe('parseRules', () => {
	it('finds the leaf rule inside a media query', () => {
		const rules = parseRules('@media (max-width: 900px) { .a { color: red; background: blue } }');
		expect(rules).toHaveLength(1);
		expect(rules[0].selector).toBe('.a');
	});

	it('collapses whitespace in a multi-line selector list', () => {
		const rules = parseRules('.a,\n\t.b {\n color: red\n}');
		expect(rules[0].selector).toBe('.a, .b');
	});
});

describe('collectAssignedTokens', () => {
	it('finds a token assigned in an inline style attribute', () => {
		expect(collectAssignedTokens('<li style="--type-color: #f00">x</li>')).toEqual([
			'--type-color'
		]);
	});

	it('does not treat a var() *reference* as an assignment', () => {
		expect(collectAssignedTokens('.a{ color: var(--accent, red) }')).toEqual([]);
	});

	it('finds a token script assigns through style.setProperty', () => {
		expect(
			collectAssignedTokens("root.style.setProperty('--bulk-bar-reserve', `${h}px`);")
		).toEqual(['--bulk-bar-reserve']);
		expect(collectAssignedTokens('el.style.setProperty("--x-y", v)')).toEqual(['--x-y']);
	});

	it('does not treat removeProperty or getPropertyValue as an assignment', () => {
		expect(collectAssignedTokens("s.removeProperty('--gone'); s.getPropertyValue('--read')")).toEqual(
			[]
		);
	});
});

describe('resolveColorValue', () => {
	it('follows a var() to its palette value', () => {
		expect(resolveColorValue('var(--accent)', PALETTE)).toBe('#638cff');
	});

	it('prefers the declared token over a fallback', () => {
		expect(resolveColorValue('var(--accent, #ff0000)', PALETTE)).toBe('#638cff');
	});

	it('falls back only when the token is undefined', () => {
		expect(resolveColorValue('var(--nope, #ff0000)', PALETTE)).toBe('#ff0000');
		expect(resolveColorValue('var(--nope)', PALETTE)).toBeNull();
	});

	it('pulls the colour out of a background shorthand', () => {
		expect(resolveColorValue('var(--bg) url(x.png) no-repeat', PALETTE)).toBe('#0f1117');
	});

	it('refuses a gradient — there is no single background colour', () => {
		expect(resolveColorValue('linear-gradient(var(--bg), var(--accent))', PALETTE)).toBeNull();
	});

	it('refuses a translucent value', () => {
		expect(resolveColorValue('rgba(0, 0, 0, 0.4)', PALETTE)).toBeNull();
	});
});

describe('isLargeText', () => {
	it('treats 24px+ as large at any weight', () => {
		expect(isLargeText([['font-size', '1.5rem']])).toBe(true);
		expect(isLargeText([['font-size', '24px']])).toBe(true);
	});

	it('treats 18.66px+ as large only when bold', () => {
		expect(isLargeText([['font-size', '1.25rem']])).toBe(false);
		expect(
			isLargeText([
				['font-size', '1.25rem'],
				['font-weight', '700']
			])
		).toBe(true);
	});

	/** An em/percent/calc size is relative to something we can't see here. */
	it('treats an unresolvable size as normal text', () => {
		expect(isLargeText([['font-size', '2em']])).toBe(false);
		expect(isLargeText([['font-size', 'calc(1rem + 2px)']])).toBe(false);
		expect(isLargeText([])).toBe(false);
	});
});

describe('auditStyles — contrast', () => {
	it('flags the --text-muted on --surface-2 pair at 4.34:1', () => {
		const findings = audit('.chip{background:var(--surface-2);color:var(--text-muted)}');
		expect(findings).toHaveLength(1);
		const f = findings[0] as Extract<StyleFinding, { kind: 'contrast' }>;
		expect(f.kind).toBe('contrast');
		expect(f.ratio).toBeCloseTo(4.34, 2);
		expect(f.required).toBe(4.5);
		expect(describeFinding(f)).toContain('below the 4.5:1 bar');
	});

	it('passes the same surface with --text', () => {
		expect(audit('.chip{background:var(--surface-2);color:var(--text)}')).toEqual([]);
	});

	it('flags white on --accent but not on --accent-strong', () => {
		expect(audit('.btn{background:var(--accent);color:#fff}')).toHaveLength(1);
		expect(audit('.btn{background:var(--accent-strong);color:#fff}')).toEqual([]);
	});

	it('applies the 3:1 large-text bar when the rule declares a large size', () => {
		expect(
			audit('.h{background:var(--accent);color:#fff;font-size:1.5rem}')
		).toEqual([]);
	});

	it('ignores a rule that sets only one side — the cascade is axe’s job', () => {
		expect(audit('.a{color:var(--text-muted)}')).toEqual([]);
		expect(audit('.a{background:var(--surface-2)}')).toEqual([]);
	});

	it('honours later declarations winning', () => {
		// The failing background is overwritten by a passing one.
		expect(
			audit('.a{background:var(--accent);background:var(--accent-strong);color:#fff}')
		).toEqual([]);
	});

	it('skips a pair it cannot resolve rather than guessing', () => {
		expect(audit('.a{background:linear-gradient(#fff,#000);color:#fff}')).toEqual([]);
		expect(audit('.a{background:rgba(0,0,0,.4);color:#fff}')).toEqual([]);
	});
});

describe('auditStyles — token drift', () => {
	it('flags a fallback that contradicts the declared token', () => {
		const findings = audit('.a{color:var(--text-muted, #94a3b8)}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({
			kind: 'stale-fallback',
			token: '--text-muted',
			fallback: '#94a3b8',
			declared: '#8a8fa0'
		});
	});

	it('accepts a fallback that agrees with the token, however it is spelled', () => {
		expect(audit('.a{color:var(--text-muted, #8A8FA0)}')).toEqual([]);
	});

	it('flags a var() whose token nothing ever assigns', () => {
		const findings = audit('.a{color:var(--danger, #f87171)}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({ kind: 'dead-token', token: '--danger' });
		expect(describeFinding(findings[0])).toContain('the fallback is what always renders');
	});

	it('does not flag a token assigned per-element rather than in :root', () => {
		expect(audit('.a{border-color:var(--type-color, #fff)}', ['--type-color'])).toEqual([]);
	});

	it('says nothing about a var() with no fallback at all', () => {
		expect(audit('.a{color:var(--text-muted)}')).toEqual([]);
	});

	/**
	 * A fallback can itself be a `var()`. A `[^()]*` capture matches nothing
	 * there, which would have been a hole in the exact guard this module is —
	 * `var(--bg, var(--surface))` shipped in two routes and was invisible to
	 * both drift checks.
	 */
	it('sees through a fallback that is itself a var()', () => {
		const findings = audit('.a{background:var(--bg, var(--surface))}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({ kind: 'stale-fallback', token: '--bg' });
	});

	it('compares a token-valued fallback by the colour it resolves to', () => {
		// --accent and its fallback spell the same colour two ways: not stale.
		expect(audit('.a{color:var(--accent, var(--accent))}')).toEqual([]);
	});

	it('still exempts a locally-assigned token behind a nested fallback', () => {
		expect(
			audit('.a{border-color:var(--type-color, var(--accent))}', ['--type-color'])
		).toEqual([]);
	});
});

describe('findVarReferences', () => {
	it('captures a plain reference, with and without a fallback', () => {
		expect(findVarReferences('color: var(--a); background: var(--b, #fff)')).toEqual([
			{ token: '--a', fallback: null },
			{ token: '--b', fallback: '#fff' }
		]);
	});

	it('captures a nested var() fallback whole, and visits the inner one too', () => {
		expect(findVarReferences('background: var(--bg, var(--surface))')).toEqual([
			{ token: '--bg', fallback: 'var(--surface)' },
			{ token: '--surface', fallback: null }
		]);
	});

	it('splits on the FIRST top-level comma, so a multi-part fallback survives', () => {
		expect(findVarReferences('font-family: var(--mono, ui-monospace, monospace)')).toEqual([
			{ token: '--mono', fallback: 'ui-monospace, monospace' }
		]);
	});

	it('keeps a function call in the fallback intact', () => {
		expect(findVarReferences('color: var(--x, rgba(0, 0, 0, 1))')).toEqual([
			{ token: '--x', fallback: 'rgba(0, 0, 0, 1)' }
		]);
	});

	it('ignores an unbalanced var( rather than guessing where it ends', () => {
		expect(findVarReferences('color: var(--x, #fff')).toEqual([]);
	});
});

describe('auditStyles — bare literal text colour', () => {
	/**
	 * The class the same-rule pair check structurally cannot see: the rule
	 * sets only `color`, so the background arrives through the cascade. The
	 * sound question left is whether the literal is legible on the surfaces
	 * body text renders on.
	 */
	it('flags a literal below the bar on an app surface', () => {
		// #e04040 — 4.47:1 on --bg, 4.11:1 on --surface.
		const findings = auditText('.err{color:#e04040}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({
			kind: 'literal-text-color',
			colorValue: '#e04040',
			surface: '--bg'
		});
		expect(describeFinding(findings[0])).toContain('use a palette token');
	});

	it('reports one finding per rule even when several surfaces fail', () => {
		expect(auditText('.err{color:#e04040}')).toHaveLength(1);
	});

	it('passes a literal that clears the bar everywhere', () => {
		// #f87171 — 6.82:1 on --bg, 6.27:1 on --surface.
		expect(auditText('.err{color:#f87171}')).toEqual([]);
	});

	/** A token is exempt: `palette contract` asserts it against these surfaces. */
	it('never flags a palette token', () => {
		expect(auditText('.a{color:var(--text-muted)}')).toEqual([]);
	});

	/**
	 * White and black are the deliberate on-a-coloured-fill choices; their
	 * background legitimately comes from a parent rule the scanner can't see.
	 */
	it('exempts white and black', () => {
		expect(auditText('.a{color:#fff}')).toEqual([]);
		expect(auditText('.a{color:white}')).toEqual([]);
		expect(auditText('.a{color:#000}')).toEqual([]);
	});

	it('stands down once the rule declares its own OPAQUE background — the pair check owns it', () => {
		// Same failing literal, but now on a stated background: reported as a
		// contrast pair (against #fff), not as a bare literal.
		const findings = auditText('.a{color:#e04040;background:#fff}');
		expect(findings.map((f) => f.kind)).toEqual(['contrast']);
	});

	/**
	 * A gradient has no single colour, so it stays unjudgeable and the literal
	 * is held to the bare surfaces instead.
	 *
	 * A *translucent* background used to be lumped in with it on the reasoning
	 * that a tint composites close to the surface behind it. It doesn't: the
	 * tint moves the surface toward the text, so that approximation is
	 * optimistic and it passed 29 real badges at 4.15–4.48:1. Compositing is
	 * exact and cheap once the backdrop is named, so the tint case now belongs
	 * to `composited-contrast` and only the gradient falls through here.
	 */
	it('still applies when the declared background is a gradient', () => {
		const gradient = auditText('.g{background:linear-gradient(#000,#fff);color:#e04040}');
		expect(gradient.map((f) => f.kind)).toEqual(['literal-text-color']);
	});

	it('hands a translucent background to the composited check instead', () => {
		const tint = auditText('.badge{background:rgba(224,64,64,0.15);color:#e04040}');
		expect(tint.map((f) => f.kind)).toEqual(['composited-contrast']);
	});

	it('honours the large-text bar', () => {
		// 4.47:1 clears the 3:1 large-text threshold.
		expect(auditText('.h{color:#e04040;font-size:1.5rem}')).toEqual([]);
	});

	it('does nothing when no text surfaces are configured', () => {
		expect(audit('.err{color:#e04040}')).toEqual([]);
	});
});

describe('auditStyles — a rule that fades itself with opacity', () => {
	/**
	 * The class BOTH other contrast checks structurally miss. Check 3 compares
	 * the declared pair; check 4 exempts a palette token on the reasoning that
	 * the palette contract already vouches for it — and opacity is precisely
	 * what invalidates that, because it composites the text down toward the
	 * backdrop. `.kpi-sub` (--text-muted at .85) rendered 4.24:1 on --surface
	 * and was invisible to the scan until axe caught it on /cfo.
	 */
	it('flags a muted token faded below the bar by the rule’s own opacity', () => {
		const findings = auditText('.sub{color:var(--text-muted);opacity:0.85}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({
			kind: 'composited-contrast',
			foreground: 'var(--text-muted)',
			opacity: 0.85
		});
		// The reported colour is what RENDERS, not what was declared.
		expect(findings[0]).not.toMatchObject({ foregroundColor: PALETTE['--text-muted'] });
		expect(describeFinding(findings[0])).toContain('opacity 0.85');
	});

	it('passes the same token at full opacity', () => {
		expect(auditText('.sub{color:var(--text-muted)}')).toEqual([]);
	});

	it('treats opacity:1 and a non-numeric value as no fade', () => {
		expect(auditText('.a{color:var(--text-muted);opacity:1}')).toEqual([]);
		expect(auditText('.b{color:var(--text-muted);opacity:inherit}')).toEqual([]);
	});

	it('fades onto the rule’s own opaque background, not the backdrop', () => {
		// --text on --surface is 11:1, so a light fade still clears — but only
		// when the box is --surface. Measured against the page behind it the
		// same rule would read far darker.
		expect(
			auditText('.c{color:var(--text);background:var(--surface);opacity:0.9}')
		).toEqual([]);
	});

	it('honours the large-text bar', () => {
		expect(auditText('.h{color:var(--text-muted);opacity:0.85;font-size:1.5rem}')).toEqual([]);
	});

	it('reports one finding per rule even when several surfaces fail', () => {
		expect(auditText('.d{color:var(--text-muted);opacity:0.5}')).toHaveLength(1);
	});

	/**
	 * An ANCESTOR's opacity is a cascade question, so it stays axe's half —
	 * this rule is legible on its own and must not be flagged here.
	 */
	it('does not try to model an ancestor’s opacity', () => {
		expect(auditText('tr.faded td{opacity:0.6}.pill{color:var(--text-muted)}')).toEqual([]);
	});
});

describe('auditStyles — a rule that tints its background translucently', () => {
	/**
	 * The status-badge recipe: a background tinted in the tone's own hue, and
	 * text set in that tone. The tint lightens the dark surface *toward* the
	 * text, so the pair renders below the bar even though both halves are
	 * individually fine — 29 badges in this app sat between 4.15:1 and 4.48:1.
	 */
	it('flags the badge recipe — accent text on its own 15% tint', () => {
		const findings = auditText('.badge{color:var(--accent);background:rgba(99,140,255,0.15)}');
		expect(findings).toHaveLength(1);
		expect(findings[0]).toMatchObject({
			kind: 'composited-contrast',
			selector: '.badge',
			// The rule declares no fade of its own — the tint alone does this.
			opacity: 1,
			surface: '--surface',
			backgroundColor: '#232b44'
		});
		expect((findings[0] as { ratio: number }).ratio).toBeCloseTo(4.48, 2);
		expect(describeFinding(findings[0])).toContain('the translucent background');
	});

	/**
	 * The regression this whole class turns on. Before the tint was composited,
	 * such a rule reached the bare-literal check instead, which measures the
	 * text against the UNTINTED surface — 5.55:1 here, a comfortable pass. That
	 * approximation is optimistic, not conservative, and it is why 29 failures
	 * went unreported by a scanner that already owned every primitive.
	 */
	it('does not let a translucent background fall through to the bare-surface check', () => {
		const findings = auditText('.badge{color:#638cff;background:rgba(99,140,255,0.15)}');
		expect(findings).toHaveLength(1);
		expect(findings[0].kind).toBe('composited-contrast');
	});

	it('passes the calibrated pair the tint tokens exist to supply', () => {
		expect(
			auditText('.badge{color:var(--accent-on-tint);background:var(--accent-tint)}')
		).toEqual([]);
	});

	/**
	 * The failure text is the whole UX of this guard, and the two compositing
	 * causes need opposite advice. Telling someone whose rule declares no
	 * opacity to "drop the fade" sends them hunting for one that isn't there.
	 */
	it('gives tint-specific remediation, not the opacity advice', () => {
		const tint = describeFinding(
			auditText('.badge{color:var(--accent);background:rgba(99,140,255,0.15)}')[0]
		);
		expect(tint).toContain('var(--<tone>-on-tint)');
		expect(tint).not.toContain('drop the fade');

		const fade = describeFinding(auditText('.sub{color:var(--text-muted);opacity:0.85}')[0]);
		expect(fade).toContain('drop the fade');
		expect(fade).not.toContain('var(--<tone>-on-tint)');
	});

	/**
	 * Both compositing causes at once — and the one case where the two plausible
	 * formulas for the TEXT side disagree, so the numbers are pinned rather than
	 * just "a finding fired".
	 *
	 * `opacity` is group opacity: the element renders to an offscreen buffer and
	 * that buffer is composited over the backdrop. An opaque glyph hides the box
	 * behind it inside the buffer, so the text fades toward the BACKDROP. Blending
	 * it onto the tinted box instead counts the tint twice on the text side, and
	 * errs optimistic — the direction a contrast guard must never err in.
	 */
	it('fades text toward the backdrop, not toward its own tint', () => {
		const tintOnly = auditText('.a{color:var(--accent-on-tint);background:var(--accent-tint)}');
		expect(tintOnly).toEqual([]);

		const findings = auditText(
			'.a{color:var(--accent-on-tint);background:var(--accent-tint);opacity:0.7}'
		);
		expect(findings).toHaveLength(1);
		const f = findings[0] as Extract<StyleFinding, { kind: 'composited-contrast' }>;
		expect(f.opacity).toBe(0.7);

		// Whichever backdrop it reported — the scan stops at the first failing
		// one, and at .7 opacity this pair already fails on --bg.
		const backdrop = parseColor(f.surfaceColor)!;
		const tint = parseColorWithAlpha(PALETTE['--accent-tint'])!;
		const hex = ({ r, g, b }: Rgb) =>
			`#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`;

		// Text: the glyph over the backdrop at the group's opacity.
		expect(f.foregroundColor).toBe(
			hex(compositeOver(parseColor(PALETTE['--accent-on-tint'])!, backdrop, 0.7))
		);
		// Box: the tint over the backdrop, then faded onto it — equivalently one
		// blend at the product of the alphas, which is what the browser draws.
		expect(f.backgroundColor).toBe(hex(compositeOver(tint.color, backdrop, tint.alpha * 0.7)));
	});

	it('leaves an opaque background to the same-rule pair check', () => {
		const findings = auditText('.b{color:var(--text-muted);background:var(--surface-2)}');
		expect(findings).toHaveLength(1);
		// `contrast`, not `composited-contrast` — nothing here needs compositing.
		expect(findings[0].kind).toBe('contrast');
	});

	it('holds a tinted rule to the large-text bar when the rule declares one', () => {
		expect(
			auditText(
				'.h{color:var(--accent);background:rgba(99,140,255,0.15);font-size:1.5rem}'
			)
		).toEqual([]);
	});

	/** A gradient has no single colour to composite, so there is nothing to judge. */
	it('stays silent on a background it cannot resolve', () => {
		expect(
			auditText('.g{color:var(--accent);background:linear-gradient(#fff,#000)}')
		).toEqual([]);
	});
});

describe('parsePalette', () => {
	it('reads the :root token block', () => {
		expect(parsePalette(':root{--bg:#0f1117;--text:#e2e4ea;font-family:sans-serif}')).toEqual({
			'--bg': '#0f1117',
			'--text': '#e2e4ea'
		});
	});
});
