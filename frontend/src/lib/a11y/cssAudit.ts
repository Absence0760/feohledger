/**
 * Static token-pairing audit over the app's stylesheets.
 *
 * The route-by-route axe guard (`tests-e2e/a11y/axe.spec.ts`) checks whatever
 * a page happens to render at scan time. That leaves two blind spots this
 * module closes:
 *
 *  - A contrast failure recurs per **surface**, not per route. `--text-muted`
 *    on `--surface-2` was 4.34:1 wherever it appeared; axe caught it on the two
 *    `/admin` pages that were in the route list and missed the identical bug on
 *    `/billing`, which wasn't. A scan of the *stylesheets* finds every instance
 *    at once, including the ones behind a modal, an empty state, or a role the
 *    e2e user doesn't hold.
 *  - A `var(--token, fallback)` whose fallback disagrees with the token is a
 *    lie waiting to render. `--surface-2` shipped for months as
 *    `var(--surface-2, #232b44)` with the token undefined — the fallback was
 *    what rendered, and two call sites disagreed about its value.
 *
 * Everything here is pure: callers hand in already-read sources, so the module
 * is unit-testable on fixture strings and the repo-wide guard
 * (`tokenPairing.test.ts`) is just the file walk plus an assertion.
 *
 * **What it deliberately does not do:** resolve the cascade. A rule that sets
 * only `color` inherits its background from an ancestor, which is a runtime
 * question — axe owns that half. This audit checks pairs that are decided in a
 * single rule, which is where the palette's own mistakes live.
 */

import {
	compositeOver,
	contrastRatio,
	parseColor,
	parseColorWithAlpha,
	type RgbaParts,
	WCAG_AA_LARGE,
	WCAG_AA_NORMAL
} from './contrast';

export interface StyleSource {
	/** Repo-relative path, used only for reporting. */
	path: string;
	/** The CSS text — a whole `.css` file, or one `<style>` block's contents. */
	css: string;
}

export interface ContrastFinding {
	kind: 'contrast';
	path: string;
	selector: string;
	/** The declaration as written, e.g. `var(--text-muted)`. */
	foreground: string;
	background: string;
	/** …and what it resolved to, e.g. `#8a8fa0`. */
	foregroundColor: string;
	backgroundColor: string;
	ratio: number;
	required: number;
}

export interface StaleFallbackFinding {
	kind: 'stale-fallback';
	path: string;
	token: string;
	fallback: string;
	declared: string;
}

export interface DeadTokenFinding {
	kind: 'dead-token';
	path: string;
	token: string;
	fallback: string;
}

export interface LiteralTextColorFinding {
	kind: 'literal-text-color';
	path: string;
	selector: string;
	/** The literal as written. */
	color: string;
	colorValue: string;
	/** The surface token it failed against, and by how much. */
	surface: string;
	surfaceColor: string;
	ratio: number;
	required: number;
}

/**
 * A pair whose rendered colour only exists after compositing — the rule fades
 * itself with `opacity`, or tints its background translucently. Both are
 * measured against the backdrop surfaces body text sits on.
 */
export interface CompositedContrastFinding {
	kind: 'composited-contrast';
	path: string;
	selector: string;
	/** The declarations as written. */
	foreground: string;
	background: string | null;
	/** The rule's own opacity (1 when it declares none). */
	opacity: number;
	/** The backdrop it failed against. */
	surface: string;
	surfaceColor: string;
	/** What actually renders, after compositing. */
	foregroundColor: string;
	backgroundColor: string;
	ratio: number;
	required: number;
}

export type StyleFinding =
	| ContrastFinding
	| StaleFallbackFinding
	| DeadTokenFinding
	| LiteralTextColorFinding
	| CompositedContrastFinding;

const TOKEN_RE = '--[a-z0-9-]+';

/** Strip `/* … *\/` comments so their contents never parse as CSS. */
export function stripCssComments(css: string): string {
	return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * The CSS a file contributes: a `.css` file is all of it; a `.svelte` file is
 * the contents of its `<style>` blocks (a component can legally have more than
 * one, e.g. a `<style>` plus a `<style module>`).
 */
export function extractStyleBlocks(path: string, source: string): StyleSource[] {
	if (path.endsWith('.css')) return [{ path, css: source }];
	const blocks: StyleSource[] = [];
	const re = /<style[^>]*>([\s\S]*?)<\/style>/gi;
	let m: RegExpExecArray | null;
	while ((m = re.exec(source)) !== null) blocks.push({ path, css: m[1] });
	return blocks;
}

/** The two palettes `app.css` declares (docs/decisions.md §289). */
export type PaletteTheme = 'dark' | 'light';
export const PALETTE_THEMES: readonly PaletteTheme[] = ['dark', 'light'];

/** The selector carrying the explicit Light choice's palette. */
export const LIGHT_THEME_SELECTOR = ":root[data-theme='light']";

/**
 * The `--token: value` pairs a theme renders with.
 *
 * `dark` is the bare `:root` block(s) — the default palette. `light` is that
 * base with the `:root[data-theme='light']` block laid over it, which is
 * exactly what the cascade does in the browser: a token the light block
 * forgot keeps its dark value, and the guard measures that dark value on the
 * light surfaces rather than pretending the token is undefined.
 *
 * Only an exact `:root` / light selector counts. The old reader matched any
 * selector STARTING with `:root`, which was harmless while there was one
 * palette and would now have merged the light values over the dark ones —
 * every dark-theme assertion silently measuring the light palette. The
 * System copy inside `@media (prefers-color-scheme: light)` is deliberately
 * not read here; `lightPaletteBlocks` exposes it so a test can hold it
 * identical to this one.
 */
export function parsePalette(css: string, theme: PaletteTheme = 'dark'): Record<string, string> {
	const palette: Record<string, string> = {};
	const blocks = rootBlocks(css);
	const light = normalizeSelector(LIGHT_THEME_SELECTOR);
	// The base layer is any top-level block whose selector LIST includes a
	// bare `:root` — `:root, [data-theme-scope='dark'] { … }` is the dark
	// palette, shared with the subtree that pins it.
	const isBase = (selector: string) => selector.split(',').some((part) => part.trim() === ':root');
	const layers: ((selector: string) => boolean)[] =
		theme === 'dark' ? [isBase] : [isBase, (selector) => selector === light];
	for (const layer of layers) {
		for (const block of blocks) {
			if (!layer(block.selector)) continue;
			for (const [name, value] of parseDeclarations(block.body)) {
				if (name.startsWith('--')) palette[name] = value;
			}
		}
	}
	return palette;
}

function normalizeSelector(selector: string): string {
	return selector.trim().replace(/\s+/g, ' ').replace(/"/g, "'");
}

/** Every top-level (non-media) `:root…{}` block, in source order. */
function rootBlocks(css: string): { selector: string; body: string }[] {
	const out: { selector: string; body: string }[] = [];
	// Blank out at-rule blocks first so a `:root` nested in a media query (the
	// System copy of the light palette) is not read as a top-level one.
	const flat = stripAtRuleBlocks(stripCssComments(css));
	const re = /([^{}]+)\{([^{}]*)\}/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(flat)) !== null) {
		const selector = normalizeSelector(m[1]);
		if (selector.split(',').some((part) => part.trim().startsWith(':root'))) {
			out.push({ selector, body: m[2] });
		}
	}
	return out;
}

/** Remove every `@… { … }` block, nested braces included. */
function stripAtRuleBlocks(css: string): string {
	let out = '';
	let i = 0;
	while (i < css.length) {
		const at = css.indexOf('@', i);
		if (at < 0) {
			out += css.slice(i);
			break;
		}
		out += css.slice(i, at);
		const open = css.indexOf('{', at);
		const semi = css.indexOf(';', at);
		if (open < 0 || (semi >= 0 && semi < open)) {
			// A statement at-rule (`@import …;`) — drop it and carry on.
			i = semi < 0 ? css.length : semi + 1;
			continue;
		}
		let depth = 0;
		let j = open;
		for (; j < css.length; j++) {
			if (css[j] === '{') depth++;
			else if (css[j] === '}' && --depth === 0) break;
		}
		i = j + 1;
	}
	return out;
}

/**
 * The two copies of the light palette, as declaration lists: the explicit
 * `:root[data-theme='light']` block, and the System copy inside
 * `@media (prefers-color-scheme: light) { :root:not([data-theme='dark']) {…} }`.
 * `null` for a copy that is missing. CSS cannot share one block between a
 * selector and a media-gated selector, so the copy is unavoidable — this is
 * what lets a test hold the two identical.
 */
export function lightPaletteBlocks(css: string): {
	explicit: [string, string][] | null;
	system: [string, string][] | null;
} {
	const clean = stripCssComments(css);
	const explicit = rootBlocks(clean).find(
		(b) => b.selector === normalizeSelector(LIGHT_THEME_SELECTOR)
	);
	const system =
		/@media\s*\(\s*prefers-color-scheme\s*:\s*light\s*\)\s*\{\s*:root:not\(\[data-theme=['"]dark['"]\]\)\s*\{([^{}]*)\}\s*\}/.exec(
			clean
		);
	return {
		explicit: explicit ? parseDeclarations(explicit.body) : null,
		system: system ? parseDeclarations(system[1]) : null
	};
}

/**
 * Every custom property *assigned* anywhere in a file — CSS rules and inline
 * `style="--x: y"` attributes alike. A token assigned per-element (the
 * exceptions page's `--type-color`) is legitimately absent from `:root`, so it
 * must not read as dead.
 */
export function collectAssignedTokens(source: string): string[] {
	const found = new Set<string>();
	const re = new RegExp(`(${TOKEN_RE})\\s*:`, 'g');
	let m: RegExpExecArray | null;
	while ((m = re.exec(stripCssComments(source))) !== null) found.add(m[1]);
	return [...found];
}

/** `prop: value` pairs of a declaration block, in source order. */
export function parseDeclarations(block: string): [string, string][] {
	const out: [string, string][] = [];
	let depth = 0;
	let current = '';
	const flush = () => {
		const i = current.indexOf(':');
		if (i > 0) out.push([current.slice(0, i).trim().toLowerCase(), current.slice(i + 1).trim()]);
		current = '';
	};
	for (const ch of block) {
		if (ch === '(') depth++;
		else if (ch === ')') depth--;
		if (ch === ';' && depth === 0) flush();
		else current += ch;
	}
	flush();
	return out;
}

export interface CssRule {
	selector: string;
	declarations: [string, string][];
}

/**
 * Leaf rules (`selector { … }`). At-rule preludes (`@media (…) {`) are skipped
 * rather than parsed: the regex only matches blocks with no nested braces, so
 * a rule inside a media query is still found — it just loses the query in its
 * reported selector, which costs nothing here.
 */
export function parseRules(css: string): CssRule[] {
	const rules: CssRule[] = [];
	const re = /([^{}]+)\{([^{}]*)\}/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(stripCssComments(css))) !== null) {
		rules.push({
			selector: m[1].trim().replace(/\s+/g, ' '),
			declarations: parseDeclarations(m[2])
		});
	}
	return rules;
}

export interface VarReference {
	token: string;
	/** The fallback as written, or `null` when the `var()` has none. */
	fallback: string | null;
}

/**
 * Every `var()` reference in a stylesheet, with its fallback.
 *
 * Paren-aware rather than a regex, because a fallback can itself be a
 * `var()` — `var(--bg, var(--surface))` is in the codebase — and a
 * `[^()]*` capture silently matches nothing there. That would be a hole in
 * the exact guard this module exists to be: a rename staling one of those
 * nested fallbacks would go unreported by both the dead-token and the
 * stale-fallback check.
 */
export function findVarReferences(css: string): VarReference[] {
	const refs: VarReference[] = [];
	const tokenRe = new RegExp(`var\\(\\s*(${TOKEN_RE})`, 'gi');
	let match: RegExpExecArray | null;
	while ((match = tokenRe.exec(css)) !== null) {
		// Walk from the `var(` to its matching close paren, tracking depth.
		let depth = 1;
		let i = match.index + 'var('.length;
		let commaAt = -1;
		for (; i < css.length && depth > 0; i++) {
			const ch = css[i];
			if (ch === '(') depth++;
			else if (ch === ')') depth--;
			else if (ch === ',' && depth === 1 && commaAt < 0) commaAt = i;
		}
		if (depth !== 0) continue; // unbalanced — not something to judge
		const close = i - 1;
		refs.push({
			token: match[1],
			fallback: commaAt < 0 ? null : css.slice(commaAt + 1, close).trim()
		});
		// `exec` has already left `lastIndex` just past `var(--token`, which is
		// what we want: scanning resumes INSIDE the fallback, so a nested
		// var() is visited on its own terms rather than skipped with its
		// parent. `match[0]` is never empty, so this always advances.
	}
	return refs;
}

/** Split a value on top-level whitespace, keeping `rgba(1, 2, 3)` intact. */
function splitTopLevel(value: string): string[] {
	const parts: string[] = [];
	let depth = 0;
	let current = '';
	for (const ch of value) {
		if (ch === '(') depth++;
		else if (ch === ')') depth--;
		if (/\s/.test(ch) && depth === 0) {
			if (current) parts.push(current);
			current = '';
		} else current += ch;
	}
	if (current) parts.push(current);
	return parts;
}

/**
 * Resolve a declaration value to a literal colour, following `var()` chains
 * through the palette (and into a fallback when the token is undefined).
 * Returns `null` for anything whose rendered colour isn't statically known —
 * a gradient, a translucent value, `currentColor`, an unresolvable token.
 */
export function resolveColorValue(
	value: string,
	palette: Record<string, string>,
	depth = 0
): string | null {
	const literal = resolveColorLiteral(value, palette, depth);
	if (literal === null) return null;
	const opaque = parseColor(literal);
	return opaque ? toHex(opaque) : null;
}

/**
 * The same `var()`-following resolution, **keeping** alpha — so a caller that
 * can name the backdrop can composite a translucent value instead of giving up
 * on it.
 *
 * `resolveColorValue` above discards these, which is right for the checks that
 * have no backdrop to offer. A translucent `background` does have one: the
 * surfaces body text sits on. Without this, such a rule reads to the audit as
 * having *no* background at all, and a tint that lightens the surface toward
 * its own text — the status-badge recipe — measured as if the text sat on the
 * bare surface, which is the optimistic direction.
 */
export function resolveColorParts(
	value: string,
	palette: Record<string, string>,
	depth = 0
): RgbaParts | null {
	const literal = resolveColorLiteral(value, palette, depth);
	return literal === null ? null : parseColorWithAlpha(literal);
}

/**
 * The shared `var()`-following core: resolves a declaration value to the colour
 * literal it renders as, **keeping** a translucent one (which
 * `resolveColorValue` then discards as unresolvable).
 */
function resolveColorLiteral(
	value: string,
	palette: Record<string, string>,
	depth = 0
): string | null {
	if (!value || depth > 8) return null;
	const v = value.trim().replace(/\s*!important$/i, '').trim();
	if (!v) return null;
	if (/gradient\(/i.test(v)) return null;

	const varMatch = new RegExp(`^var\\(\\s*(${TOKEN_RE})\\s*(?:,([\\s\\S]*))?\\)$`, 'i').exec(v);
	if (varMatch) {
		const declared = palette[varMatch[1]];
		if (declared !== undefined) return resolveColorLiteral(declared, palette, depth + 1);
		if (varMatch[2] !== undefined) return resolveColorLiteral(varMatch[2], palette, depth + 1);
		return null;
	}

	const mixed = resolveColorMix(v, palette, depth);
	if (mixed !== undefined) return mixed;

	if (parseColorWithAlpha(v)) return v;

	// A shorthand like `background: var(--surface) url(x) no-repeat` — the
	// colour is one of the top-level parts.
	const parts = splitTopLevel(v);
	if (parts.length > 1) {
		for (const part of parts) {
			const resolved = resolveColorLiteral(part, palette, depth + 1);
			if (resolved) return resolved;
		}
	}
	return null;
}

/**
 * `color-mix(in srgb, A p%, B)` — the spelling a token-derived tint takes
 * (`color-mix(in srgb, var(--danger) 12%, transparent)`). Resolved rather than
 * treated as unknowable, because the light theme turned ~150 literal tints
 * into exactly this form: had the audit gone blind to it, converting a literal
 * to a theme-following token would have *removed* its contrast check.
 *
 * Two shapes are exact and resolved: B is `transparent` (the result is A with
 * its alpha scaled by p — premultiplied mixing with a fully transparent colour
 * changes nothing else), and A and B both opaque (a straight per-channel
 * interpolation in sRGB). Anything else — another colour space, a translucent
 * B — returns `null` (known to be a colour, not known which). `undefined`
 * means the value is not a `color-mix()` at all.
 */
function resolveColorMix(
	value: string,
	palette: Record<string, string>,
	depth: number
): string | null | undefined {
	const m = /^color-mix\(\s*in\s+srgb\s*,([\s\S]*)\)$/i.exec(value);
	if (!m) return undefined;
	const args = splitTopLevelCommas(m[1]);
	if (args.length !== 2) return null;
	const parse = (arg: string): { parts: RgbaParts | 'transparent' | null; pct: number | null } => {
		const pm = /^([\s\S]*?)\s+([\d.]+)%$/.exec(arg.trim());
		const colour = (pm ? pm[1] : arg).trim();
		const pct = pm ? parseFloat(pm[2]) / 100 : null;
		if (colour.toLowerCase() === 'transparent') return { parts: 'transparent', pct };
		const literal = resolveColorLiteral(colour, palette, depth + 1);
		return { parts: literal === null ? null : parseColorWithAlpha(literal), pct };
	};
	const a = parse(args[0]);
	const b = parse(args[1]);
	if (!a.parts || !b.parts) return null;
	// CSS: a missing percentage is the complement of the other; both missing is 50/50.
	const pa = a.pct ?? (b.pct !== null ? 1 - b.pct : 0.5);
	if (a.parts === 'transparent' || pa < 0 || pa > 1) return null;
	if (b.parts === 'transparent') {
		const { r, g, b: bl } = a.parts.color;
		return `rgba(${r}, ${g}, ${bl}, ${+(a.parts.alpha * pa).toFixed(4)})`;
	}
	if (a.parts.alpha < 1 || b.parts.alpha < 1) return null;
	const c = compositeOver(a.parts.color, b.parts.color, pa);
	return toHex(c);
}

/** Split on top-level commas, keeping `rgba(1, 2, 3)` and `var(--x, y)` intact. */
function splitTopLevelCommas(value: string): string[] {
	const parts: string[] = [];
	let depth = 0;
	let current = '';
	for (const ch of value) {
		if (ch === '(') depth++;
		else if (ch === ')') depth--;
		if (ch === ',' && depth === 0) {
			parts.push(current);
			current = '';
		} else current += ch;
	}
	parts.push(current);
	return parts.map((p) => p.trim());
}

function toHex({ r, g, b }: { r: number; g: number; b: number }): string {
	return `#${[r, g, b].map((n) => n.toString(16).padStart(2, '0')).join('')}`;
}

/**
 * Is the text in this rule "large" per SC 1.4.3 — 24px at any weight, or
 * 18.66px bold? Only a size declared *in the same rule* counts; an inherited
 * or `em`-relative size is unknowable here and is treated as normal text,
 * which is the stricter direction.
 */
export function isLargeText(declarations: [string, string][]): boolean {
	const decls = Object.fromEntries(declarations);
	const size = decls['font-size'];
	if (!size) return false;
	let px: number | null = null;
	const rem = /^([\d.]+)rem$/.exec(size.trim());
	const abs = /^([\d.]+)px$/.exec(size.trim());
	if (rem) px = parseFloat(rem[1]) * 16;
	else if (abs) px = parseFloat(abs[1]);
	if (px === null) return false;
	if (px >= 24) return true;
	const weight = (decls['font-weight'] ?? '').trim().toLowerCase();
	const bold = weight === 'bold' || weight === 'bolder' || parseInt(weight, 10) >= 700;
	return bold && px >= 18.66;
}

export interface AuditOptions {
	palette: Record<string, string>;
	/** Every custom property assigned anywhere in the scanned tree. */
	assignedTokens: Set<string>;
	/**
	 * Palette tokens a bare literal `color:` is held against — the surfaces
	 * body text actually sits on. `--surface-2` is deliberately NOT here: it's
	 * a raised panel used in a handful of places, and a rule that puts text on
	 * it declares the background, so the same-rule pair check already covers
	 * it. Including it would flag every red/green status literal in the app on
	 * the strength of a surface it never renders against.
	 */
	textSurfaces?: string[];
}

/**
 * Colours exempt from the bare-literal rule: they are the deliberate
 * on-a-coloured-fill choices, whose background legitimately comes from a
 * parent or sibling rule the scanner can't see.
 */
const LITERAL_EXEMPT = new Set(['#ffffff', '#000000']);

/**
 * The rule's own `opacity`, or 1 when it declares none / declares something
 * non-numeric. Only a rule's OWN opacity is knowable here — an ancestor's is
 * not, which is the seam `tests-e2e/a11y/axe.spec.ts` covers at runtime.
 */
function ruleOpacity(declarations: Array<[string, string]>): number {
	let opacity = 1;
	for (const [prop, value] of declarations) {
		if (prop !== 'opacity') continue;
		const raw = value.trim().replace(/\s*!important$/i, '').trim();
		const n = raw.endsWith('%') ? parseFloat(raw) / 100 : parseFloat(raw);
		if (Number.isFinite(n) && n >= 0 && n <= 1) opacity = n; // later wins
	}
	return opacity;
}


/**
 * Run all three checks over the given stylesheets. Findings are returned, not
 * thrown, so the caller decides how to report them.
 */
export function auditStyles(sources: StyleSource[], options: AuditOptions): StyleFinding[] {
	const findings: StyleFinding[] = [];
	const { palette, assignedTokens, textSurfaces = [] } = options;

	for (const source of sources) {
		const css = stripCssComments(source.css);

		// 1 + 2 — every var() reference: is the token real, and does a fallback
		// contradict it?
		for (const { token, fallback } of findVarReferences(css)) {
			if (fallback === null) continue; // nothing to contradict, nothing dead to hide
			const declared = palette[token];
			if (declared === undefined) {
				if (!assignedTokens.has(token)) {
					findings.push({ kind: 'dead-token', path: source.path, token, fallback });
				}
			} else if (!sameValue(declared, fallback, palette)) {
				findings.push({
					kind: 'stale-fallback',
					path: source.path,
					token,
					fallback,
					declared
				});
			}
		}

		// 3 — contrast of pairs decided within one rule.
		for (const rule of parseRules(source.css)) {
			let foreground: string | null = null;
			let background: string | null = null;
			for (const [prop, value] of rule.declarations) {
				if (prop === 'color') foreground = value;
				// Later declaration wins, and the `background` shorthand resets
				// `background-color`, so plain assignment models both.
				else if (prop === 'background' || prop === 'background-color') background = value;
			}
			// A background is one of three things, and conflating the last two is
			// what left the tint class unmeasured: opaque (compare directly),
			// translucent (composite over the backdrop first), or unresolvable
			// (a gradient — nothing to judge).
			const backgroundParts = background ? resolveColorParts(background, palette) : null;
			const resolvedBackground =
				backgroundParts && backgroundParts.alpha >= 1 ? toHex(backgroundParts.color) : null;
			const translucentBackground =
				backgroundParts && backgroundParts.alpha < 1 ? backgroundParts : null;

			// 4 — a literal `color:` whose rule states no OPAQUE background.
			// Whatever is behind it comes from the cascade — nothing at all, a
			// translucent tint, a gradient — so the only sound question is
			// whether the literal is legible on the surfaces body text sits on.
			// A palette token is exempt because `palette contract` asserts each
			// one against those same surfaces directly.
			//
			// A *translucent* background is excluded here and measured properly
			// by check 5 below. Holding the literal to the bare surface was the
			// old approximation, and it is the optimistic one, not the
			// conservative one: the tint lightens the surface toward the text,
			// so the pair renders worse than this check models — which is
			// exactly how 29 status badges passed at 4.15–4.48:1.
			if (foreground && !resolvedBackground && !translucentBackground) {
				const fg = resolveColorValue(foreground, palette);
				const isToken = /^var\(/i.test(foreground.trim());
				if (fg && !isToken && !LITERAL_EXEMPT.has(fg)) {
					const required = isLargeText(rule.declarations) ? WCAG_AA_LARGE : WCAG_AA_NORMAL;
					for (const token of textSurfaces) {
						const surfaceColor = palette[token];
						if (!surfaceColor) continue;
						const ratio = contrastRatio(fg, surfaceColor);
						if (ratio === null || ratio + 1e-9 >= required) continue;
						findings.push({
							kind: 'literal-text-color',
							path: source.path,
							selector: rule.selector,
							color: foreground,
							colorValue: fg,
							surface: token,
							surfaceColor,
							ratio,
							required
						});
						break; // one finding per rule — the first failing surface names it
					}
				}
			}

			// 5 — a rule whose rendered colour only exists after compositing.
			// Two shapes, one calculation:
			//
			//   • the rule fades ITSELF with `opacity`, which composites the
			//     element — text and its background together — down onto
			//     whatever is behind it, so a colour that clears the bar at full
			//     strength can render well under it (`--text-muted` at `.85` is
			//     4.24:1 on `--surface`, which is what axe reported on /cfo);
			//   • the rule tints its background translucently, which lightens
			//     the dark surface *toward* text set in the same hue — the
			//     status-badge recipe, and the reason 29 of them sat between
			//     4.15:1 and 4.48:1.
			//
			// Nothing above sees either: check 3 compares the declared pair, and
			// check 4 exempts a palette token on the reasoning that `palette
			// contract` already vouches for it — which is precisely what
			// compositing invalidates.
			//
			// Only the rule's OWN opacity and OWN background are knowable here.
			// An ANCESTOR's opacity is not — see the module header.
			const opacity = ruleOpacity(rule.declarations);
			if (foreground && (opacity < 1 || translucentBackground)) {
				const fg = resolveColorValue(foreground, palette);
				const required = isLargeText(rule.declarations) ? WCAG_AA_LARGE : WCAG_AA_NORMAL;
				if (fg) {
					for (const token of textSurfaces) {
						const backdrop = palette[token] ? parseColor(palette[token]) : null;
						if (!backdrop) continue;
						// The element's own box colour, as rendered: an opaque
						// background is itself; a translucent one is that tint
						// composited over the backdrop; declaring none lets the
						// backdrop show through untouched.
						const opaqueBg = resolvedBackground ? parseColor(resolvedBackground) : null;
						const tinted = translucentBackground
							? compositeOver(
									translucentBackground.color,
									backdrop,
									translucentBackground.alpha
								)
							: null;
						const box = opaqueBg ?? tinted ?? backdrop;
						// `opacity` is GROUP opacity: the element's subtree is
						// rendered to an offscreen buffer, then that buffer is
						// composited over the backdrop. Inside the buffer an
						// opaque glyph completely covers the box behind it, so
						// the text's blend target is the BACKDROP — not its own
						// background, which never shows through the glyph.
						// Blending text onto `box` double-counts the tint on the
						// text side, and does so optimistically.
						//
						// The box side does pass through its background twice,
						// and that is correct: compositing a tint over B and
						// then fading the result onto B is algebraically the
						// same as one blend at the product of the two alphas,
						// which is what the browser draws.
						const text = compositeOver(parseColor(fg)!, backdrop, opacity);
						const faded = compositeOver(box, backdrop, opacity);

						const ratio = contrastRatio(text, faded);
						if (ratio === null || ratio + 1e-9 >= required) continue;
						findings.push({
							kind: 'composited-contrast',
							path: source.path,
							selector: rule.selector,
							foreground,
							background,
							opacity,
							surface: token,
							surfaceColor: palette[token],
							foregroundColor: toHex(text),
							backgroundColor: toHex(faded),
							ratio,
							required
						});
						break; // one finding per rule — the first failing surface names it
					}
				}
				// Fully described by the check above; the opaque same-rule pair
				// below would measure colours that never render.
				continue;
			}

			if (!foreground || !background || !resolvedBackground) continue;

			const fg = resolveColorValue(foreground, palette);
			const bg = resolvedBackground;
			if (!fg) continue;

			const ratio = contrastRatio(fg, bg);
			if (ratio === null) continue;
			const required = isLargeText(rule.declarations) ? WCAG_AA_LARGE : WCAG_AA_NORMAL;
			if (ratio + 1e-9 < required) {
				findings.push({
					kind: 'contrast',
					path: source.path,
					selector: rule.selector,
					foreground,
					background,
					foregroundColor: fg,
					backgroundColor: bg,
					ratio,
					required
				});
			}
		}
	}
	return findings;
}

/**
 * Compare two CSS values for "the same colour", so `#FFF` === `white` and a
 * fallback written as another token (`var(--bg, var(--surface))`) is compared
 * by the colour it resolves to, not by its spelling. Non-colour values (a font
 * stack) fall back to a normalized string compare.
 */
function sameValue(a: string, b: string, palette: Record<string, string>): boolean {
	const normalize = (v: string) => {
		const resolved = resolveColorValue(v, palette);
		if (resolved) return resolved;
		const parsed = parseColor(v);
		return parsed ? toHex(parsed) : v.trim().toLowerCase().replace(/\s+/g, ' ');
	};
	return normalize(a) === normalize(b);
}

/** One-line, copy-pasteable description used in the guard's failure output. */
export function describeFinding(finding: StyleFinding): string {
	switch (finding.kind) {
		case 'contrast':
			return (
				`${finding.path} — {${finding.selector}}: ` +
				`color ${finding.foreground} (${finding.foregroundColor}) on ` +
				`${finding.background} (${finding.backgroundColor}) is ` +
				`${finding.ratio.toFixed(2)}:1, below the ${finding.required}:1 bar`
			);
		case 'stale-fallback':
			return (
				`${finding.path} — var(${finding.token}, ${finding.fallback}) contradicts the ` +
				`declared ${finding.token}: ${finding.declared}; drop the fallback`
			);
		case 'dead-token':
			return (
				`${finding.path} — var(${finding.token}, ${finding.fallback}) references a token ` +
				`nothing ever assigns, so the fallback is what always renders; ` +
				`define ${finding.token} or inline the value`
			);
		case 'literal-text-color':
			return (
				`${finding.path} — {${finding.selector}}: color ${finding.color} ` +
				`(${finding.colorValue}) is ${finding.ratio.toFixed(2)}:1 on ${finding.surface} ` +
				`(${finding.surfaceColor}), below the ${finding.required}:1 bar. The rule sets no ` +
				`background, so this text renders on an app surface — use a palette token`
			);
		case 'composited-contrast': {
			// Name the cause AND the remedy for that cause. The two shapes need
			// opposite advice — "drop the fade" is nonsense to someone whose rule
			// declares no opacity, and would send them looking for one.
			const faded = finding.opacity < 1;
			const cause = faded
				? `opacity ${finding.opacity}` +
					(finding.background ? ` and a ${finding.background} background` : '')
				: `the translucent background ${finding.background}`;
			const remedy = faded
				? 'Opacity fades text and its background onto the backdrop; pick a colour ' +
					'that clears the bar once composited, or drop the fade — if the text is ' +
					'already on a muted token, the fade only spends contrast'
				: 'A translucent tint lightens the surface toward text set in the same tone. ' +
					'Use the calibrated pair for it — background: var(--<tone>-tint) with ' +
					'color: var(--<tone>-on-tint) — rather than picking a new hex';
			return (
				`${finding.path} — {${finding.selector}}: color ${finding.foreground} under ` +
				`${cause} renders as ${finding.foregroundColor} on ${finding.backgroundColor} ` +
				`over ${finding.surface} (${finding.surfaceColor}) — ${finding.ratio.toFixed(2)}:1, ` +
				`below the ${finding.required}:1 bar. ${remedy}`
			);
		}
	}
}
