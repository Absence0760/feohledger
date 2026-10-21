// The help centre's whole content, joined: every guide, the glossary, the page
// directory, and search over them. Only the /help pages import this module —
// a <HelpTip> elsewhere in the app needs nothing but a catalogue key, so the
// guide prose never ships with an ordinary page.
//
// Pure (no `$app`, no store): search takes a `label` resolver so the caller
// passes m(), and vitest can drive it with the English catalogue.

import type { MessageKey } from '#lib/i18n/messages.ts';
import { NAV } from '#lib/nav.ts';
import { GLOSSARY } from './glossary.ts';
import { CONCEPT_GUIDES } from './guides/concepts.ts';
import { INVOICE_GUIDES } from './guides/invoices.ts';
import { PAYMENT_GUIDES } from './guides/payments.ts';
import { SETUP_GUIDES } from './guides/setup.ts';
import { START_GUIDES } from './guides/start.ts';
import { VENDOR_GUIDES } from './guides/vendors.ts';
import { plainText, type InlinePart } from './inline.ts';
import { PAGE_HELP } from './pages.ts';
import { termHref, termKey, termShortKey } from './terms.ts';
import type { GlossaryCategory, GlossaryEntry, Guide, GuideKind, HelpRole } from './types.ts';

export { GLOSSARY, PAGE_HELP, termHref, termKey, termShortKey };
export type { GlossaryCategory, GlossaryEntry, Guide, GuideKind, HelpRole };

/** Every guide, in contents order: start, then the how-tos by area, then concepts. */
export const GUIDES: Guide[] = [
	...START_GUIDES,
	...INVOICE_GUIDES,
	...PAYMENT_GUIDES,
	...VENDOR_GUIDES,
	...SETUP_GUIDES,
	...CONCEPT_GUIDES
];

/** The concept guides, for the landing page. */
export const CONCEPT_GUIDES_LIST: Guide[] = CONCEPT_GUIDES;

export const GUIDE_KINDS: GuideKind[] = ['start', 'howto', 'concept'];

export const GUIDE_KIND_KEYS: Record<GuideKind, MessageKey> = {
	start: 'help.kind.start',
	howto: 'help.kind.howto',
	concept: 'help.kind.concept'
};

/** The how-tos, grouped as the contents list shows them. */
export const HOWTO_GROUPS: { key: MessageKey; guides: Guide[] }[] = [
	{ key: 'help.area.invoices', guides: INVOICE_GUIDES },
	{ key: 'help.area.payments', guides: PAYMENT_GUIDES },
	{ key: 'help.area.vendors', guides: VENDOR_GUIDES },
	{ key: 'help.area.setup', guides: SETUP_GUIDES }
];

export const GLOSSARY_CATEGORIES: GlossaryCategory[] = [
	'invoices',
	'matching',
	'approvals',
	'payments',
	'vendors',
	'controls',
	'platform'
];

export const CATEGORY_KEYS: Record<GlossaryCategory, MessageKey> = {
	invoices: 'help.cat.invoices',
	matching: 'help.cat.matching',
	approvals: 'help.cat.approvals',
	payments: 'help.cat.payments',
	vendors: 'help.cat.vendors',
	controls: 'help.cat.controls',
	platform: 'help.cat.platform'
};

/** The starting guide for each role, in the order the landing page offers them. */
export const ROLE_START: Record<HelpRole, string> = {
	ap_clerk: 'start-ap-clerk',
	ap_manager: 'start-ap-manager',
	cfo: 'start-cfo',
	admin: 'start-admin'
};
export const HELP_ROLES: HelpRole[] = ['ap_clerk', 'ap_manager', 'cfo', 'admin'];

const guidesById = new Map(GUIDES.map((g) => [g.id, g]));
const termsById = new Map(GLOSSARY.map((t) => [t.id, t]));

export const guideFor = (id: string): Guide | undefined => guidesById.get(id);

/**
 * The guides that explain a glossary term — every guide listing it in
 * `terms` — how-tos first, then concepts, then the role starts. What a
 * HelpTip offers as further reading.
 */
export function guidesForTerm(id: string, limit = 3): Guide[] {
	const rank: Record<GuideKind, number> = { howto: 0, concept: 1, start: 2 };
	return GUIDES.filter((g) => g.terms?.includes(id))
		.sort((a, b) => rank[a.kind] - rank[b.kind])
		.slice(0, limit);
}
export const termFor = (id: string): GlossaryEntry | undefined => termsById.get(id);


// ---------------------------------------------------------------------------
// The app's pages, for [[page:…]] links and the page directory
// ---------------------------------------------------------------------------

export interface NavPage {
	href: string;
	labelKey: MessageKey;
	roles?: string[];
	permissions?: string[];
	/** The group's label key, for a page folded into a sidebar group. */
	groupKey?: MessageKey;
}

/** Every sidebar destination, in sidebar order. */
export const NAV_PAGES: NavPage[] = NAV.flatMap((e) =>
	e.kind === 'link'
		? [{ href: e.href, labelKey: e.labelKey, roles: e.roles, permissions: e.permissions }]
		: e.children.map((c) => ({ href: c.href, labelKey: c.labelKey, roles: c.roles, permissions: c.permissions, groupKey: e.labelKey }))
);

const navByHref = new Map(NAV_PAGES.map((p) => [p.href, p]));
export const navPageFor = (href: string): NavPage | undefined => navByHref.get(href);

export { pageHelpFor, pageHelpHref } from './routeHelp.ts';

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export type Label = (key: MessageKey) => string;

export type SearchHit =
	| { kind: 'guide'; guide: Guide; score: number }
	| { kind: 'term'; term: GlossaryEntry; score: number }
	| { kind: 'page'; href: string; score: number };

const norm = (s: string) =>
	s
		.normalize('NFKD')
		.replace(/[̀-ͯ]/g, '')
		.toLowerCase();

/** Resolve inline references to the words a reader sees. */
function resolver(label: Label) {
	return (p: Extract<InlinePart, { kind: 'ui' | 'term' | 'page' }>) => {
		if (p.kind === 'ui') return label(p.key as MessageKey);
		if (p.kind === 'term') return label(termKey(p.id));
		const nav = navPageFor(p.href);
		return nav ? label(nav.labelKey) : '';
	};
}

function score(words: string[], title: string, body: string): number {
	const t = norm(title);
	const b = norm(body);
	let s = 0;
	for (const w of words) {
		if (t.includes(w)) s += 3;
		else if (b.includes(w)) s += 1;
		else return 0;
	}
	if (t.startsWith(words.join(' '))) s += 5;
	return s;
}

/**
 * Guides, glossary terms and pages matching every word of `query`, best first
 * (a title match outranks a body match; ties keep contents order). Titles are
 * matched in the reader's language (term names and page labels come from the
 * catalogue) and in English (guide titles, aliases), so either finds them.
 */
export function searchHelp(query: string, label: Label): SearchHit[] {
	const words = norm(query).split(/\s+/).filter(Boolean);
	if (!words.length) return [];
	const text = resolver(label);
	const hits: (SearchHit & { i: number })[] = [];
	let i = 0;
	for (const guide of GUIDES) {
		const body = [
			guide.summary,
			...guide.sections.flatMap((s) => [
				s.heading,
				...s.blocks.flatMap((b) =>
					b.type === 'p' || b.type === 'note' ? [b.text] : b.type === 'steps' || b.type === 'list' ? b.items : []
				)
			])
		]
			.map((t) => plainText(t, text))
			.join(' ');
		const s = score(words, guide.title, body);
		if (s) hits.push({ kind: 'guide', guide, score: s + 1, i: i++ });
	}
	for (const term of GLOSSARY) {
		const title = [label(termKey(term.id)), term.id.replace(/-/g, ' '), ...(term.aliases ?? [])].join(' ');
		const s = score(words, title, [label(termShortKey(term.id)), plainText(term.long, text)].join(' '));
		if (s) hits.push({ kind: 'term', term, score: s, i: i++ });
	}
	for (const [href, help] of Object.entries(PAGE_HELP)) {
		const nav = navPageFor(href);
		const title = nav ? label(nav.labelKey) : href;
		const s = score(words, title, plainText(help.summary, text));
		if (s) hits.push({ kind: 'page', href, score: s, i: i++ });
	}
	return hits.sort((a, b) => b.score - a.score || a.i - b.i).map(({ i: _i, ...hit }) => hit as SearchHit);
}
