import { describe, expect, it } from 'vitest';
import { NAV, type NavGroup } from './nav.ts';

// Every Settings tab shares one tab bar, so switching tabs must not change how
// wide the page is. The shell's `.workspace` frame owns the width
// (frontend/docs/ui-patterns.md § Page layout: "do not change `max-width` …
// per-route"). /billing once wrapped its whole body in `max-width: 64rem`, and
// Subscription stopped at ~1024px while Organization beside it filled the frame.
//
// This checks the elements that wrap the page body: those opened at most two
// tabs deep inside <PageHeader> (direct children, or inside one {#if}). A
// width cap on prose deeper in the page (a hint paragraph, a form field) is a
// line length, not the page width, and is not checked.

const RAW = import.meta.glob('/src/routes/**/+page.svelte', {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;

const settings = NAV.find(
	(e): e is NavGroup => e.kind === 'group' && e.labelKey === 'nav.group.settings'
);

function routeFile(href: string): string {
	const path = href.split('?')[0].replace(/\/$/, '');
	return `/src/routes${path}/+page.svelte`;
}

/** Classes on block elements opened at most two tabs deep in <PageHeader>'s body. */
function bodyWrapperClasses(src: string): string[] {
	const start = src.indexOf('<PageHeader');
	const end = src.indexOf('</PageHeader>');
	if (start < 0 || end < 0) return [];
	const body = src.slice(start, end);
	const classes: string[] = [];
	let inSnippet = false;
	for (const line of body.split('\n')) {
		if (/^\t\{#snippet\b/.test(line)) inSnippet = true;
		if (/^\t\{\/snippet\}/.test(line)) {
			inSnippet = false;
			continue;
		}
		if (inSnippet) continue;
		const m = /^\t{1,2}<(div|section|main|article)\b[^>]*\bclass="([^"]+)"/.exec(line);
		if (m) classes.push(...m[2].split(/\s+/));
	}
	return classes;
}

/** Declarations of a top-level `.cls { … }` rule in the page's <style>. */
function ruleBody(src: string, cls: string): string {
	const style = src.slice(src.indexOf('<style'));
	const re = new RegExp(`^\\t\\.${cls}\\s*\\{([^}]*)\\}`, 'm');
	return re.exec(style)?.[1] ?? '';
}

describe('Settings tabs fill the shared workspace width', () => {
	const tabs = settings?.children ?? [];

	it('finds the Settings tabs and their pages (the scan is live)', () => {
		expect(tabs.length).toBeGreaterThan(5);
		for (const tab of tabs) expect(RAW[routeFile(tab.href)], tab.href).toBeDefined();
		expect(bodyWrapperClasses(RAW['/src/routes/billing/+page.svelte'])).toContain('billing');
		expect(bodyWrapperClasses(RAW['/src/routes/organization/+page.svelte'])).toContain(
			'settings-layout'
		);
	});

	it('no tab caps its page body with max-width', () => {
		const offenders: string[] = [];
		for (const tab of tabs) {
			const file = routeFile(tab.href);
			const src = RAW[file];
			for (const cls of bodyWrapperClasses(src)) {
				if (/\bmax-width\s*:/.test(ruleBody(src, cls))) offenders.push(`${file} .${cls}`);
			}
		}
		expect([...new Set(offenders)]).toEqual([]);
	});
});
