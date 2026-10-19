import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: every password-typed `<input>` says what it is for (#575).
 *
 * The admin Edit-user modal carried an optional `type="password"` input with
 * no `autocomplete`, so the browser autofilled the ADMIN's own saved password
 * into it and an unrelated rename silently reset someone else's credential
 * (#566, `docs/decisions.md` §276). That defect class is "a password field the
 * password manager has to guess about", so this scans every `.svelte` template
 * and requires each input that is — or can become, through a `type={…}`
 * expression naming `'password'` — a password field to declare one of:
 *
 *   - `current-password` — the account's existing password (sign-in, the
 *     "current" field of a change form, a step-up re-authentication);
 *   - `new-password` — a password being SET (the new + confirm fields), and
 *     also every write-only secret field in this app (API keys, client
 *     secrets: `ui/SecretField.svelte`, the SSO client secret, the extraction
 *     keys). Those use `new-password` rather than `off` on purpose: browsers'
 *     password managers ignore `autocomplete="off"` on a password input, and a
 *     "key id" text box followed by a password box is exactly the login-form
 *     shape they autofill — the #566 failure. `new-password` is the value they
 *     honour by never filling a saved password;
 *   - `off` — accepted for a field where neither applies.
 *
 * And a form that changes a password (it holds both a `current-password` and a
 * `new-password` input) must also carry an `autocomplete="username"` field —
 * hidden, read-only, as `routes/change-password/+page.svelte` does — or the
 * password manager files the new password against no account. A sign-in form
 * (a `current-password` input and an email/text identifier) must mark that
 * identifier `autocomplete="username"` — `email` is not the token a password
 * manager pairs a saved password with, even when the identifier is an address.
 *
 * Like `a11y/imageDragging.test.ts`, the template is parsed with the Svelte
 * compiler and only `ast.fragment` is walked, so a `<script>` comment that
 * mentions `type="password"` is structurally out of reach.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

const ALLOWED = new Set(['current-password', 'new-password', 'off']);

interface Node {
	type: string;
	name?: string;
	start: number;
	end: number;
	attributes?: Attr[];
	[key: string]: unknown;
}

interface Attr {
	type: string;
	name?: string;
	value?: unknown;
}

function collect(node: unknown, name: string, out: Node[] = []): Node[] {
	if (node === null || typeof node !== 'object') return out;
	if (Array.isArray(node)) {
		for (const child of node) collect(child, name, out);
		return out;
	}
	const record = node as Record<string, unknown>;
	if (record.type === 'RegularElement' && record.name === name) out.push(record as Node);
	for (const [key, value] of Object.entries(record)) {
		if (key === 'parent') continue;
		collect(value, name, out);
	}
	return out;
}

function attr(element: Node, name: string): Attr | undefined {
	return element.attributes?.find((a) => a.type === 'Attribute' && a.name === name);
}

/** A static attribute's literal text, or `null` when it is (or contains) an expression. */
function literal(a: Attr | undefined): string | null {
	if (!a || !Array.isArray(a.value)) return null;
	const parts = a.value as Record<string, unknown>[];
	if (!parts.every((p) => p.type === 'Text')) return null;
	return parts.map((p) => p.data as string).join('');
}

/** The source text of an attribute's value, expressions included. */
function valueSource(a: Attr | undefined, source: string): string {
	if (!a || a.value === true || a.value == null) return '';
	const parts = (Array.isArray(a.value) ? a.value : [a.value]) as Node[];
	return parts.map((p) => source.slice(p.start, p.end)).join('');
}

/** `type="password"`, or a `type={…}` expression that can evaluate to it. */
function canBePassword(input: Node, source: string): boolean {
	const typeAttr = attr(input, 'type');
	const fixed = literal(typeAttr);
	if (fixed !== null) return fixed.trim().toLowerCase() === 'password';
	return /['"`]password['"`]/.test(valueSource(typeAttr, source));
}

function autocompleteOf(input: Node): string | null {
	const value = literal(attr(input, 'autocomplete'));
	return value === null ? null : value.trim().toLowerCase();
}

function excerpt(input: Node, source: string): string {
	return source.slice(input.start, input.end).replace(/\s+/g, ' ').trim();
}

function findings(source: string, path = 'inline.svelte'): string[] {
	const ast = parse(source, { modern: true });
	const out: string[] = [];
	for (const input of collect(ast.fragment, 'input')) {
		if (!canBePassword(input, source)) continue;
		const value = autocompleteOf(input);
		if (value === null || !ALLOWED.has(value)) {
			out.push(`${path}: autocomplete=${value ?? '(missing)'} on ${excerpt(input, source)}`);
		}
	}
	for (const form of collect(ast.fragment, 'form')) {
		const inputs = collect(form.fragment, 'input');
		const values = new Set(inputs.map(autocompleteOf));
		// A SIGN-IN form (current password, nothing new): password managers pair
		// the saved password with the `username` field, and `email` is not that
		// token — the form's identifier must say `username` even when it is an
		// address (`type="email"` stays; the token is about purpose, not shape).
		// A step-up re-authentication (password only, no identifier field) is
		// not a sign-in form and is left alone.
		if (values.has('current-password') && !values.has('new-password')) {
			for (const input of inputs) {
				const type = (literal(attr(input, 'type')) ?? 'text').trim().toLowerCase();
				if (type !== 'email' && type !== 'text') continue;
				const value = autocompleteOf(input);
				if (value === 'one-time-code' || value === 'username') continue;
				out.push(
					`${path}: a sign-in <form>'s account identifier must be autocomplete="username" ` +
						`(found ${value ?? '(missing)'}) on ${excerpt(input, source)}`
				);
			}
		}
		if (values.has('current-password') && values.has('new-password') && !values.has('username')) {
			out.push(
				`${path}: a password-change <form> has no autocomplete="username" field ` +
					'(add the hidden one routes/change-password/+page.svelte carries)'
			);
		}
	}
	return out;
}

describe('every password input declares its autocomplete purpose (#575)', () => {
	it('scans a realistic number of files — the walk itself must not silently break', () => {
		expect(files.length).toBeGreaterThan(50);
	});

	it('the scanner flags what it should and ignores what it should', () => {
		// Independent of any real file, so a broken walk can't turn the real
		// assertion green for the wrong reason.
		const source = [
			'<script>',
			'  // <input type="password"> mentioned in a comment is not an input',
			'</script>',
			'<input type="password" />',
			'{#if a}<input type="password" autocomplete="current-password" />{/if}',
			'<input type={shown ? "text" : "password"} />',
			'<input type={inputType(key)} />',
			'<input type="text" />',
			'<input type="password" autocomplete="nope" />',
			'<form><input type="password" autocomplete="current-password" />',
			'<input type="password" autocomplete="new-password" /></form>',
			'<form><input hidden autocomplete="username" />',
			'<input type="password" autocomplete="current-password" />',
			'<input type="password" autocomplete="new-password" /></form>'
		].join('\n');
		const found = findings(source);
		expect(found).toHaveLength(4);
		expect(found[0]).toContain('(missing)');
		expect(found[1]).toContain('shown ?');
		expect(found[2]).toContain('autocomplete=nope');
		expect(found[3]).toContain('password-change <form>');
	});

	it('a sign-in form needs a username identifier; a step-up form does not', () => {
		const signIn = (email: string) =>
			`<form><input type="email" ${email} /><input type="password" autocomplete="current-password" /></form>`;
		expect(findings(signIn('autocomplete="email"'))[0]).toContain('found email');
		expect(findings(signIn(''))[0]).toContain('found (missing)');
		expect(findings(signIn('autocomplete="username"'))).toEqual([]);
		// Re-authentication: a password and a one-time code, no identifier.
		expect(
			findings(
				'<form><input type="password" autocomplete="current-password" />' +
					'<input type="text" autocomplete="one-time-code" /></form>'
			)
		).toEqual([]);
	});

	it('every password input in src/ declares current-password / new-password / off', () => {
		const all = files.flatMap((file) => findings(file.source, file.path));
		expect(
			all,
			`\n${all.join('\n')}\n\nA password-typed input must say what it is for — see the ` +
				'module doc for which value to choose. A write-only secret (API key, client ' +
				'secret) takes new-password, not off.'
		).toEqual([]);
	});
});
