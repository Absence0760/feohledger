// Which help a page's "How this page works" link opens. Its own module over
// the small page directory (./pages.ts) alone, because PageHeader renders on
// every page: importing ./content.ts there would ship every guide's prose with
// every route.

import { PAGE_HELP } from './pages.ts';

/**
 * The page-directory entry for the page at `pathname` (+ `search`): the exact
 * href first (so `/admin?tab=roles` finds Roles), then the longest href whose
 * path is a prefix (so `/invoices/123` finds Invoices). Off the directory →
 * undefined.
 */
export function pageHelpFor(pathname: string, search = ''): { href: string; guide?: string } | undefined {
	const here = pathname.replace(/\/$/, '') || '/';
	const params = new URLSearchParams(search);
	let best: string | undefined;
	let bestDefault: string | undefined;
	for (const href of Object.keys(PAGE_HELP)) {
		const [path, qs] = href.split('?');
		if (qs) {
			if (path !== here) continue;
			const want = [...new URLSearchParams(qs)];
			if (want.every(([k, v]) => params.get(k) === v)) return { href, ...PAGE_HELP[href] };
			// No query of its own on the URL: the first tab on this path is the default (nav.ts's sectionTabActive rule).
			if (!bestDefault && want.every(([k]) => params.get(k) === null)) bestDefault = href;
			continue;
		}
		const match = path === '/' ? here === '/' : here === path || here.startsWith(path + '/');
		if (match && (!best || path.length > best.split('?')[0].length)) best = href;
	}
	const hit = bestDefault ?? best;
	return hit ? { href: hit, ...PAGE_HELP[hit] } : undefined;
}

/** Where that link goes: the page's guide, or its row in the page directory. */
export function pageHelpHref(pathname: string, search = ''): string | undefined {
	const help = pageHelpFor(pathname, search);
	if (!help) return undefined;
	return help.guide ? `/help/guides/${help.guide}` : `/help/pages#${encodeURIComponent(help.href)}`;
}
