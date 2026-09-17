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
 * This scans every `<img` element in every `.svelte` file's TEMPLATE (the
 * `<script>` block is stripped first) and requires `draggable="false"` on all
 * of them, except an explicit, commented allowlist — the whole point of a
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

/**
 * Strips every `<script>…</script>` block so a comment that merely mentions
 * `<img>` as prose (e.g. `InvoiceModal.svelte`'s "`<img src>` and `<iframe
 * src>` can't reach the file…", `EmptyState.svelte`'s "Inline SVG rather than
 * an `<img>`") is never mistaken for a rendered element.
 */
function stripScriptBlocks(source: string): string {
	return source.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
}

/** Every `<img …>` tag in a file's template (script blocks excluded). */
function findImgTags(source: string): string[] {
	return stripScriptBlocks(source).match(/<img\b[^>]*>/gis) ?? [];
}

function isDraggableFalse(tag: string): boolean {
	return /draggable\s*=\s*"false"/.test(tag);
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
		// A regression check for the parser itself, independent of any real
		// file — without this, breaking `findImgTags` / `isDraggableFalse`
		// could turn every assertion below green for the wrong reason.
		const source = [
			'<script>',
			'  // rather than an <img> here, in a comment',
			'</script>',
			'<img class="mark" src="/x.svg" alt="" />'
		].join('\n');
		const tags = findImgTags(source);
		expect(tags).toHaveLength(1);
		expect(isDraggableFalse(tags[0])).toBe(false);
		expect(isDraggableFalse('<img src="/x.svg" draggable="false" />')).toBe(true);
	});

	it('every non-exempt <img> carries draggable="false"', () => {
		const findings: string[] = [];
		for (const file of files) {
			if (ALLOWLIST.has(file.path)) continue;
			for (const tag of findImgTags(file.source)) {
				if (!isDraggableFalse(tag)) {
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
			return !file || findImgTags(file.source).length === 0;
		});
		expect(stale, 'no longer renders an <img> — drop from ALLOWLIST').toEqual([]);
	});
});
