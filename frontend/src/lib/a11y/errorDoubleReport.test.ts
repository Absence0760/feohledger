import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard: one error, one signal (`docs/ui-patterns.md` § Toast).
 *
 * An error toast stays until it is dismissed (decisions §313). Where a dialog
 * or panel already states the same failure in place — a `role="alert"` element
 * rendering the error variable — a second copy in a toast adds nothing for
 * anyone: `role="alert"` is itself announced the moment it appears, and the
 * text is already beside the control that failed. What the copy did add was a
 * persistent card floating over the dialog: the payment run's NACHA refusal was
 * toasted as well as stated in its panel, and the toast sat over "Record run as
 * paid…" until dismissed (`tests-e2e/payments/record-outside.spec.ts`).
 *
 * The rule this enforces, per `.svelte` file: an error toast never repeats a
 * variable the template renders inside a `role="alert"` element — neither by
 * passing that variable to `toast(…, 'error')` nor by assigning it in the same
 * block that raises the toast. It walks the Svelte AST (template and script),
 * so a comment that mentions the idiom cannot trip it.
 *
 * Out of scope, on purpose: a page whose LOAD failed shows a generic in-place
 * block with a Retry and may toast the server's own detail — the two say
 * different things (`ui/LoadError`, § Toast).
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

type Node = { type: string; [key: string]: unknown };

function isNode(v: unknown): v is Node {
	return !!v && typeof v === 'object' && typeof (v as Node).type === 'string';
}

function walk(node: unknown, visit: (n: Node, parents: Node[]) => void, parents: Node[] = []) {
	if (Array.isArray(node)) {
		for (const child of node) walk(child, visit, parents);
		return;
	}
	if (!isNode(node)) return;
	visit(node, parents);
	const next = [...parents, node];
	for (const [key, value] of Object.entries(node)) {
		if (key === 'parent' || key === 'metadata') continue;
		if (value && typeof value === 'object') walk(value, visit, next);
	}
}

/**
 * The variables an in-place error is made of. `text`: identifiers rendered as
 * `{name}` inside a `role="alert"` element — the error's own words. `flags`:
 * identifiers of the `{#if}` test that shows such an element, which renders a
 * fixed message (`historyError`).
 */
function alertVariables(ast: Node): { text: Set<string>; flags: Set<string> } {
	const text = new Set<string>();
	const flags = new Set<string>();
	walk(ast.fragment, (n, parents) => {
		if (n.type !== 'RegularElement') return;
		const attrs = (n.attributes as Node[]) ?? [];
		const role = attrs.find((a) => a.type === 'Attribute' && a.name === 'role');
		const value = role?.value;
		const roleText = Array.isArray(value) && value.length === 1 ? (value[0] as Node).data : null;
		if (roleText !== 'alert') return;
		walk(n.fragment, (inner) => {
			if (inner.type !== 'ExpressionTag') return;
			const expr = inner.expression as Node;
			if (expr.type === 'Identifier') text.add(expr.name as string);
		});
		const guard = [...parents].reverse().find((p) => p.type === 'IfBlock');
		walk(guard?.test, (e) => {
			if (e.type === 'Identifier') flags.add(e.name as string);
		});
	});
	return { text, flags };
}

const FUNCTIONS = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression']);

/** A reset (`x = ''`, `null`, `false`) clears the in-place error; it states nothing. */
function isReset(expr: Node): boolean {
	return expr.type === 'Literal' && (expr.value === '' || expr.value === null || expr.value === false);
}

function isErrorToast(n: Node): boolean {
	if (n.type !== 'CallExpression') return false;
	const callee = n.callee as Node;
	const args = n.arguments as Node[];
	return (
		callee.type === 'Identifier' &&
		callee.name === 'toast' &&
		args.length >= 2 &&
		args[1].type === 'Literal' &&
		args[1].value === 'error'
	);
}

/** A catalogue message (`m('key')`): fixed words, not the server's detail. */
function isFixedMessage(n: Node): boolean {
	const callee = n.callee as Node | undefined;
	return n.type === 'CallExpression' && callee?.type === 'Identifier' && callee.name === 'm';
}

const nearestBlock = (chain: Node[]) => [...chain].reverse().find((p) => p.type === 'BlockStatement');

/** `toast(…, 'error')` calls that repeat an error the template states in place. */
function doubleReports(source: string): string[] {
	const ast = parse(source, { modern: true }) as unknown as Node;
	const { text, flags } = alertVariables(ast);
	if (text.size === 0 && flags.size === 0) return [];
	const found: string[] = [];
	walk((ast.instance as Node | null)?.content, (toastCall, toastChain) => {
		if (!isErrorToast(toastCall)) return;
		const message = (toastCall.arguments as Node[])[0];
		if (message.type === 'Identifier' && text.has(message.name as string)) {
			found.push(message.name as string);
			return;
		}
		// Which in-place errors this toast would repeat: one carrying the error's
		// own words always; a flag's fixed message only when the toast is a fixed
		// message too. A load failure's generic block beside a toast of the
		// server's detail says two different things, which § Toast allows.
		const repeats = new Set([...text, ...(isFixedMessage(message) ? flags : [])]);
		// The same failure stated twice: an assignment to one of those on the
		// toast's own path — in a block enclosing the toast, or inside the
		// toast's own block — within the same catch clause (else function).
		// Assignments in a sibling branch (`if (409) … else toast(…)`) are a
		// different outcome, not a repeat.
		const scope = [...toastChain]
			.reverse()
			.find((p) => p.type === 'CatchClause' || FUNCTIONS.has(p.type));
		if (!scope) return;
		const toastBlock = nearestBlock(toastChain);
		walk(scope.body, (a, chain) => {
			if (a.type !== 'AssignmentExpression') return;
			const left = a.left as Node;
			if (left.type !== 'Identifier' || !repeats.has(left.name as string)) return;
			if (isReset(a.right as Node)) return;
			const block = nearestBlock(chain) ?? (scope.body as Node);
			// A branch that returns after the assignment never reaches a toast
			// outside it (`if (editing) { err = …; return; } toast(…)`).
			const exits = (block.body as Node[]).some(
				(st) => st.type === 'ReturnStatement' || st.type === 'ThrowStatement'
			);
			const samePath =
				toastChain.includes(block) || (!exits && !!toastBlock && chain.includes(toastBlock));
			if (samePath) found.push(left.name as string);
		}, [...toastChain.slice(0, toastChain.indexOf(scope) + 1)]);
	});
	return found;
}

describe('one error, one signal', () => {
	it('detects both shapes of the double report', () => {
		const passed = `<script>
	let err = $state('');
	async function go() {
		try { await x(); } catch (e) { err = String(e); toast(err, 'error'); }
	}
</script>
{#if err}<p role="alert">{err}</p>{/if}`;
		const assigned = `<script>
	let err = $state('');
	async function go() {
		try { await x(); } catch (e) { err = 'no'; toast(String(e), 'error'); }
	}
</script>
{#if err}<p role="alert">{err}</p>{/if}`;
		const clean = `<script>
	let err = $state('');
	async function go() {
		try { await x(); } catch (e) { err = String(e); }
		try { await y(); } catch (e) { toast(String(e), 'error'); }
	}
</script>
{#if err}<p role="alert">{err}</p>{/if}`;
		const flag = `<script>
	let failed = $state(false);
	async function go() {
		failed = false;
		try { await x(); } catch (e) {
			failed = true;
			if (e) toast(m('k'), 'error');
		}
	}
</script>
{#if failed}<p role="alert">{m('k')}</p>{/if}`;
		const branches = `<script>
	let stale = $state('');
	async function go() {
		try { await x(); } catch (e) {
			if (e.status === 409) { stale = 'moved'; } else { toast(String(e), 'error'); }
		}
	}
</script>
{#if stale}<p role="alert">{stale}</p>{/if}`;
		const load = `<script>
	let errored = $state(false);
	async function load() {
		try { await x(); } catch (e) { errored = true; toast(e.message, 'error'); }
	}
</script>
{#if errored}<div role="alert">{m('couldNotLoad')}</div>{/if}`;
		const early = `<script>
	let err = $state('');
	async function go(editing) {
		try { await x(); } catch (e) {
			if (editing) { err = String(e); return; }
			toast(String(e), 'error');
		}
	}
</script>
{#if err}<p role="alert">{err}</p>{/if}`;
		expect(doubleReports(branches)).toEqual([]);
		expect(doubleReports(early)).toEqual([]);
		expect(doubleReports(load)).toEqual([]);
		expect(doubleReports(passed)).toEqual(['err']);
		expect(doubleReports(flag)).toEqual(['failed']);
		expect(doubleReports(assigned)).toEqual(['err']);
		expect(doubleReports(clean)).toEqual([]);
	});

	it('no component toasts an error it already states in a role="alert" element', () => {
		const offenders = Object.entries(RAW)
			.flatMap(([path, source]) =>
				doubleReports(source).map((name) => `${path.replace(/^\/src\//, '')}: ${name}`)
			)
			.sort();
		expect(
			offenders,
			'an error stated in place (role="alert") is not also toasted — drop the toast'
		).toEqual([]);
	});
});
