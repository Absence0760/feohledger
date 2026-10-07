import { describe, expect, it } from 'vitest';
import { extractStyleBlocks } from './cssAudit';

/**
 * A table cell must stay a table cell.
 *
 * `display: flex` (or grid) on a `<td>` / `<th>` takes it out of the table
 * model. The column stops sizing it, so its content runs past the column (the
 * /vendors actions overflowed the table's right edge), and its box no longer
 * spans the row, so its bottom border drifts out of line with the rest of the
 * row (/vendors actions, the Users name cell). Lay out a cell's contents with
 * inline elements and margins, or wrap them in an inner element that flexes.
 *
 * Scans every rule whose selector targets a `td` or `th` by element name.
 * A class-only selector applied to a cell (the Users `.name-cell` was one) is
 * out of reach of a static scan, which is why the /vendors e2e also asserts
 * the rendered `display`.
 */
const RAW = import.meta.glob('/src/**/*.{svelte,css}', {
	query: '?raw',
	import: 'default',
	eager: true,
}) as Record<string, string>;

const CELL_SELECTOR = /(^|[\s>+~(,])t[dh](?![\w-])/;
const FLEX_OR_GRID = /(^|;|\s)display\s*:\s*(inline-)?(flex|grid)\b/;

/** `path: selector` for every rule in `css` that makes a cell flex or grid. */
function flexCells(path: string, css: string): string[] {
	const found: string[] = [];
	const rule = /([^{}]+)\{([^{}]*)\}/g;
	let m: RegExpExecArray | null;
	while ((m = rule.exec(css)) !== null) {
		const selector = m[1].replace(/\/\*[\s\S]*?\*\//g, '').trim();
		const body = m[2].replace(/\/\*[\s\S]*?\*\//g, '');
		const cells = selector
			.split(',')
			.map((s) => s.trim())
			// The cell itself, not something inside it (`td .chip` is fine).
			.filter((s) => CELL_SELECTOR.test(s.split(/\s+|>/).filter(Boolean).at(-1) ?? ''));
		if (cells.length && FLEX_OR_GRID.test(body)) found.push(`${path}: ${cells.join(', ')}`);
	}
	return found;
}

describe('table cells keep display: table-cell', () => {
	it('no rule makes a td or th a flex or grid container', () => {
		const found = Object.entries(RAW).flatMap(([path, source]) =>
			extractStyleBlocks(path, source).flatMap(({ css }) => flexCells(path, css))
		);
		expect(found).toEqual([]);
	});

	it('flags the rule that broke /vendors, and leaves content inside cells alone', () => {
		expect(flexCells('app.css', '.grid-container td.actions { display: flex; gap: 6px; }')).toEqual([
			'app.css: .grid-container td.actions',
		]);
		expect(flexCells('x', 'table th { display: inline-grid }')).toHaveLength(1);
		expect(flexCells('x', 'td .chip { display: inline-flex }')).toEqual([]);
		expect(flexCells('x', '.tdx { display: flex }')).toEqual([]);
		expect(flexCells('x', 'td.actions { white-space: nowrap }')).toEqual([]);
	});
});
