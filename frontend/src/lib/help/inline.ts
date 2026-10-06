// The help centre's inline markup → typed parts (the grammar is in ./types.ts).
// Pure: no catalogue, no `$app`. RichText.svelte resolves each part (a `ui`
// key through m(), a `term` id to its catalogue name) and renders it as an
// element, so guide text is never `{@html}`.

export type InlinePart =
	| { kind: 'text'; text: string }
	| { kind: 'strong'; text: string }
	| { kind: 'em'; text: string }
	/** A catalogue message key whose label is rendered (bold). */
	| { kind: 'ui'; key: string }
	/** A glossary link; `label` absent = the term's own name. */
	| { kind: 'term'; id: string; label?: string }
	| { kind: 'guide'; id: string; label: string }
	/** A link into the app; `label` absent = the page's own nav label. */
	| { kind: 'page'; href: string; label?: string };

// One alternation, leftmost match wins. `[^\]|]` keeps a link's target and
// label from running into the next link on the same line.
const TOKEN =
	/\*\*([^*]+)\*\*|\*([^*\s][^*]*)\*|\{ui:([a-zA-Z0-9_.-]+)\}|\[\[guide:([a-z0-9-]+)\|([^\]]+)\]\]|\[\[page:(\/[^\]|]*)(?:\|([^\]]+))?\]\]|\[\[([a-z0-9-]+)(?:\|([^\]]+))?\]\]/g;

export function inline(text: string): InlinePart[] {
	const parts: InlinePart[] = [];
	let last = 0;
	for (const mt of text.matchAll(TOKEN)) {
		const at = mt.index ?? 0;
		if (at > last) parts.push({ kind: 'text', text: text.slice(last, at) });
		if (mt[1] !== undefined) parts.push({ kind: 'strong', text: mt[1] });
		else if (mt[2] !== undefined) parts.push({ kind: 'em', text: mt[2] });
		else if (mt[3] !== undefined) parts.push({ kind: 'ui', key: mt[3] });
		else if (mt[4] !== undefined) parts.push({ kind: 'guide', id: mt[4], label: mt[5] });
		else if (mt[6] !== undefined) parts.push(mt[7] !== undefined ? { kind: 'page', href: mt[6], label: mt[7] } : { kind: 'page', href: mt[6] });
		else parts.push(mt[9] !== undefined ? { kind: 'term', id: mt[8], label: mt[9] } : { kind: 'term', id: mt[8] });
		last = at + mt[0].length;
	}
	if (last < text.length) parts.push({ kind: 'text', text: text.slice(last) });
	return parts;
}

/** The text a reader sees, with every reference reduced to its label — for search. `label` resolves ui keys and term ids. */
export function plainText(text: string, label: (part: Extract<InlinePart, { kind: 'ui' | 'term' | 'page' }>) => string): string {
	return inline(text)
		.map((p) => {
			switch (p.kind) {
				case 'ui':
					return label(p);
				case 'term':
					return p.label ?? label(p);
				case 'text':
				case 'strong':
				case 'em':
					return p.text;
				case 'page':
					return p.label ?? label(p);
				case 'guide':
					return p.label;
			}
		})
		.join('');
}
