// The help centre's content shapes. Type-only, so the modules that hold the
// text import nothing at run time and each stays its own chunk.
//
// Type-only apart from DIAGRAM_IDS, a literal list.
//
// Two halves, translated differently (docs/decisions.md §244):
//
//  - What a reader meets INSIDE the app — the help centre's chrome, every
//    glossary term's name and one-line definition (the ⓘ HelpTip text) — is
//    ordinary UI copy and lives in the message catalogues under `help.*`, in
//    all six locales.
//  - The long-form guide and glossary prose below is authored in English and
//    rendered with `lang="en"` under any other UI locale (WCAG 3.1.2), behind a
//    translated notice saying so. UI labels inside that prose are NOT typed out
//    in English: `{ui:message.key}` renders the catalogue label itself, so a
//    guide names a button in the reader's language and can never drift from a
//    rename (lib/help/content.test.ts resolves every reference).

import type { SystemRole } from '#lib/types/admin.ts';

/** The four system roles a guide can be written for. */
export type HelpRole = SystemRole;

export type GuideKind = 'start' | 'howto' | 'concept';

/**
 * Inline markup, parsed by `inline()` in ./inline.ts — no HTML, so nothing is
 * ever `{@html}` (frontend/CLAUDE.md § Sanitization):
 *
 *   **Upload**                  bold (a label the catalogue has no key for)
 *   *payable*                   emphasis
 *   {ui:invoices.status.approved}  a UI label, from the catalogue, in bold
 *   [[three-way-match]]         glossary link, labelled with the term's name
 *   [[three-way-match|matched]] glossary link with its own label
 *   [[guide:run-payments|label]]   link to another guide
 *   [[page:/payments]]          link into the app, labelled with its nav label
 *   [[page:/payments|label]]    the same, with its own label (needed off the nav)
 */
export type GuideBlock =
	| { type: 'p'; text: string }
	/** Numbered steps: things to do, in order. */
	| { type: 'steps'; items: string[] }
	| { type: 'list'; items: string[] }
	/**
	 * A callout. `tip` helps, `caution` warns about a control that will stop
	 * you (and why it exists), `role` says who may do this.
	 */
	| { type: 'note'; tone: 'tip' | 'caution' | 'role'; text: string }
	/** The interactive invoice-lifecycle walkthrough (InvoiceLifecycle.svelte). */
	| { type: 'lifecycle' }
	/** An illustration (components/help/diagrams/), with a caption in help prose. */
	| { type: 'diagram'; id: DiagramId; caption: string };

/**
 * Every illustration a guide can show. Each is an inline-SVG Svelte component
 * in `components/help/diagrams/`, drawn from the app's colour tokens (so it
 * follows the tenant's accent) and labelled from the message catalogue (so it
 * reads in the reader's language) — never a raster with English baked in.
 */
export const DIAGRAM_IDS = [
	'capture-channels',
	'three-way-match',
	'approval-chain',
	'segregation-of-duties',
	'bank-change-dual-control',
	'payment-run',
	'exception-flow',
	'roles-overview'
] as const;
export type DiagramId = (typeof DIAGRAM_IDS)[number];

export interface GuideSection {
	heading: string;
	blocks: GuideBlock[];
}

export interface Guide {
	/** Stable slug: /help/guides/<id>. Lowercase, hyphenated. Part of the URL. */
	id: string;
	title: string;
	/** One or two sentences, for lists and search. */
	summary: string;
	kind: GuideKind;
	/**
	 * Who the guide is for. Drives the landing page's "for your role" list and
	 * the "Who this is for" line. Omitted = everyone.
	 */
	roles?: HelpRole[];
	/** The app page the work happens on, for the guide's "Open … " button. */
	route?: string;
	sections: GuideSection[];
	/** Glossary ids worth reading next. */
	terms?: string[];
	/** Other guide ids. */
	related?: string[];
}

export type GlossaryCategory = 'invoices' | 'matching' | 'approvals' | 'payments' | 'vendors' | 'controls' | 'platform';

export interface GlossaryEntry {
	/**
	 * Stable anchor (`/help/glossary#<id>`) and the catalogue stem: the term's
	 * name is `help.term.<id>` and its one-line definition `help.term.<id>.short`.
	 */
	id: string;
	category: GlossaryCategory;
	/** The fuller explanation, English. Paragraphs separated by a blank line. Inline markup allowed. */
	long: string;
	/** Other names people search for (abbreviations, ERP vocabulary). English. */
	aliases?: string[];
	/** Ids of related entries. */
	related?: string[];
}
