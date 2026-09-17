import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

/**
 * Static guard for #435 (ghost-image drag).
 *
 * Browsers make every `<img>` draggable by default, so a brand mark or a
 * tenant logo peels off under the cursor and trails a ghost image — there is
 * nothing a reader could want to do with it, so it just reads as a bug. The
 * fix (matching `AuthShell.svelte`'s `.panel-art` and
 * `marketing/Landing.svelte`'s `.tally-art`) is `draggable="false"` on the
 * element plus `-webkit-user-drag: none` / `user-select: none` in CSS — the
 * attribute alone doesn't cover WebKit/Blink's own image-drag or the
 * selection highlight a half-started drag leaves behind.
 *
 * This parses every `.svelte` file with the Svelte compiler and walks the
 * TEMPLATE fragment only — `<script>` bodies are a separate branch of the
 * AST, so a comment that merely mentions `<img>` as prose is structurally
 * out of reach rather than regex-stripped. It requires `draggable="false"`
 * on every image, except an explicit, commented allowlist — the whole point of a
 * per-image rule rather than a blanket one over `<img>` is that not every
 * image should be undraggable:
 *
 *   - `InvoiceModal.svelte`'s invoice preview — the user's OWN document.
 *     Dragging it out to save it is a legitimate action and must stay
 *     draggable (issue #435 "Deliberately excluded").
 *   - the two MFA enrolment QR codes (`routes/portal/company/+page.svelte`,
 *     `routes/profile/+page.svelte`) — a QR encodes the TOTP seed, i.e. a
 *     shared secret. Issue #435 flags "can this be dragged into another
 *     window" as a decision not yet taken, not a default to inherit from the
 *     browser — so these are exempted FOR NOW, not declared safe. Revisit
 *     when that decision is made; don't silently expand this list.
 *
 * `routes/portal/login/+page.svelte`'s two tenant-logo `<img>`s are NOT
 * exempted here — issue #435 calls them out for the same fix, landing in a
 * parallel change. If that file hasn't been fixed yet, the third test below
 * fails naming it; that's expected until the other change lands, not a
 * reason to add it to ALLOWLIST.
 */

const RAW = import.meta.glob('/src/**/*.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const files = Object.entries(RAW)
	.map(([path, source]) => ({ path: path.replace(/^\/src\//, ''), source }))
	.sort((a, b) => a.path.localeCompare(b.path));

interface ImgElement {
	start: number;
	end: number;
	attributes: unknown[];
}

/**
 * Collects every `<img>` element under an AST node.
 *
 * The walk is generic — it recurses through every own property rather than
 * enumerating block types — so an image inside an `{#if}`, `{#each}`,
 * `{#await}`, `{#snippet}` or any future block is found without this needing
 * to know that block exists. `parent` is skipped because the AST is cyclic.
 */
function collectImgElements(node: unknown, out: ImgElement[] = []): ImgElement[] {
	if (node === null || typeof node !== 'object') return out;
	if (Array.isArray(node)) {
		for (const child of node) collectImgElements(child, out);
		return out;
	}
	const record = node as Record<string, unknown>;
	if (record.type === 'RegularElement' && record.name === 'img') {
		out.push(record as unknown as ImgElement);
	}
	for (const [key, value] of Object.entries(record)) {
		if (key === 'parent') continue;
		collectImgElements(value, out);
	}
	return out;
}

/**
 * Every `<img>` in a file's template.
 *
 * Only `ast.fragment` is walked. The Svelte AST puts `<script>` bodies on
 * `ast.instance` / `ast.module`, so prose mentioning `<img>` in a comment —
 * `InvoiceModal.svelte`'s "`<img src>` and `<iframe src>` can't reach the
 * file…", `EmptyState.svelte`'s "Inline SVG rather than an `<img>`" — is not
 * in the tree being searched at all. That is the structural version of what a
 * `<script>`-stripping regex tried to approximate, and it cannot be defeated
 * by an end tag the pattern failed to anticipate.
 */
function findImgElements(source: string): ImgElement[] {
	const ast = parse(source, { modern: true });
	return collectImgElements(ast.fragment);
}

/** `draggable="false"` as a literal attribute — not an expression, not `{false}`. */
function isDraggableFalse(element: ImgElement): boolean {
	return element.attributes.some((raw) => {
		const attr = raw as Record<string, unknown>;
		if (attr.type !== 'Attribute' || attr.name !== 'draggable') return false;
		const value = attr.value;
		if (!Array.isArray(value) || value.length !== 1) return false;
		const only = value[0] as Record<string, unknown>;
		return only.type === 'Text' && only.data === 'false';
	});
}

/**
 * Deliberate exemptions — see the module doc above for why each one exists.
 * Every entry needs a reason in a comment; a bare path here would silently
 * authorise a new draggable chrome image.
 */
const ALLOWLIST = new Set<string>([
	// The user's own invoice document — dragging it out to save it is a
	// legitimate action (issue #435 "Deliberately excluded").
	'lib/components/modals/InvoiceModal.svelte',
	// MFA enrolment QR codes — issue #435 flags "can this secret be dragged
	// into another window" as a decision not yet taken, not a default. See
	// the module doc above.
	'routes/portal/company/+page.svelte',
	'routes/profile/+page.svelte'
]);

describe('chrome/brand images resist the browser default drag (#435)', () => {
	it('scans a realistic number of files — the walk itself must not silently break', () => {
		expect(files.length).toBeGreaterThan(50);
	});

	it('the scanner detects an undraggable <img> and ignores a comment mention', () => {
		// A regression check for the walk itself, independent of any real file —
		// without this, breaking `findImgElements` / `isDraggableFalse` could turn
		// every assertion below green for the wrong reason. The `</script >` here
		// is deliberate: a script-stripping regex that anchors on `</script>` lets
		// this block's prose leak into the template it scans, which is the defect
		// that motivated parsing rather than matching.
		const source = [
			'<script>',
			'  // rather than an <img> here, in a comment',
			'</script >',
			'<img class="mark" src="/x.svg" alt="" />',
			'{#if show}<img class="ok" src="/y.svg" alt="" draggable="false" />{/if}'
		].join('\n');
		const found = findImgElements(source);
		expect(found).toHaveLength(2);
		expect(isDraggableFalse(found[0])).toBe(false);
		// Found inside an {#if}, proving the walk reaches block children.
		expect(isDraggableFalse(found[1])).toBe(true);
	});

	it('every non-exempt <img> carries draggable="false"', () => {
		const findings: string[] = [];
		for (const file of files) {
			if (ALLOWLIST.has(file.path)) continue;
			for (const element of findImgElements(file.source)) {
				if (!isDraggableFalse(element)) {
					const tag = file.source.slice(element.start, element.end);
					findings.push(`${file.path}: ${tag.replace(/\s+/g, ' ').trim()}`);
				}
			}
		}
		expect(
			findings,
			`\n${findings.join('\n')}\n\n` +
				'A chrome/decorative <img> is missing draggable="false" (plus the ' +
				'-webkit-user-drag / user-select CSS pair — see the .panel-art comment ' +
				"in AuthShell.svelte). If this is a genuinely new, deliberate exemption, " +
				'add it to ALLOWLIST above with a reason; do not add draggable="false" to ' +
				"InvoiceModal's own preview or the two MFA QR codes, which are exempt on " +
				'purpose.'
		).toEqual([]);
	});

	it('the allowlist names no file that no longer renders an <img> at all', () => {
		// Keeps the exemption list honest: an entry whose <img> disappeared (or
		// whose file was deleted/renamed) should be dropped rather than kept as
		// dead weight that could mask an unrelated future <img> in that file.
		const stale = [...ALLOWLIST].filter((path) => {
			const file = files.find((f) => f.path === path);
			return !file || findImgElements(file.source).length === 0;
		});
		expect(stale, 'no longer renders an <img> — drop from ALLOWLIST').toEqual([]);
	});
});
