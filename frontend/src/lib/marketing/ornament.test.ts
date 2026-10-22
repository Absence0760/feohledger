import { describe, expect, it } from 'vitest';
import { TALLY_ORNAMENT, TALLY_ORNAMENT_SIZE } from './ornament.ts';

/**
 * The committed ornament variants, read as bytes. `?inline` hands back a data
 * URL, which keeps this free of `node:fs` (the frontend has no Node types,
 * deliberately) and resolves through the same bundler that serves `static/`.
 */
const FILES = import.meta.glob('/static/marketing/*.webp', {
	query: '?inline',
	import: 'default',
	eager: true
}) as Record<string, string>;

function bytesOf(dataUrl: string): Uint8Array {
	const b64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
	return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

/** Canvas size from a WebP's RIFF header (VP8X, VP8 or VP8L). */
function webpSize(b: Uint8Array): { width: number; height: number } {
	const tag = String.fromCharCode(...b.slice(0, 4)) + String.fromCharCode(...b.slice(8, 12));
	expect(tag, 'not a RIFF/WEBP file').toBe('RIFFWEBP');
	const chunk = String.fromCharCode(...b.slice(12, 16));
	const u24 = (o: number) => b[o] | (b[o + 1] << 8) | (b[o + 2] << 16);
	if (chunk === 'VP8X') return { width: u24(24) + 1, height: u24(27) + 1 };
	if (chunk === 'VP8 ') {
		return { width: (b[26] | (b[27] << 8)) & 0x3fff, height: (b[28] | (b[29] << 8)) & 0x3fff };
	}
	if (chunk === 'VP8L') {
		const bits = b[21] | (b[22] << 8) | (b[23] << 16) | (b[24] << 24);
		return { width: (bits & 0x3fff) + 1, height: ((bits >> 14) & 0x3fff) + 1 };
	}
	throw new Error(`unknown WebP chunk ${chunk}`);
}

describe('tally ornament variants', () => {
	for (const [theme, path] of Object.entries(TALLY_ORNAMENT)) {
		it(`[${theme}] ${path} is committed at the shared intrinsic size`, () => {
			const file = FILES[`/static/${path}`];
			expect(file, `static/${path} must exist — run \`pnpm gen:marketing\``).toBeDefined();
			// A variant trimmed to another frame would shift the layout on a
			// theme switch, and the <img>'s width/height would lie about it.
			expect(webpSize(bytesOf(file))).toEqual(TALLY_ORNAMENT_SIZE);
		});
	}

	it('the variants are different renders, not one file twice', () => {
		const [dark, light] = [TALLY_ORNAMENT.dark, TALLY_ORNAMENT.light].map(
			(p) => FILES[`/static/${p}`]
		);
		expect(dark).not.toEqual(light);
	});
});
