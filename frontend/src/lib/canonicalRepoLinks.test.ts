import { describe, expect, it } from 'vitest';

// The public marketing pages link out to this project's own GitHub repo. The
// "View on GitHub" CTA on the self-hosted pricing tier pointed at
// `github.com/jaredhoward/project-account-payables`, which was wrong twice
// over: the repo had been renamed to `feohledger`, and `jaredhoward` is a
// DIFFERENT PERSON's GitHub account (a name collision — this project lives
// under the `Absence0760` org). So the primary call-to-action on the pricing
// page sent every visitor to a 404 in a stranger's namespace.
//
// Nothing caught it because no test asserts where an outbound link goes, and a
// 404 on a third-party host is invisible to the e2e suite. These are pure
// source-text assertions, matching the scan idiom in `tenantSlugUsage.test.ts`
// — the frontend's vitest setup targets pure modules, not rendered components
// (see frontend/CLAUDE.md), so the link is checked where it is written.
//
// The rule is deliberately narrow: third-party GitHub links (pgvector, sops,
// axe-core, …) are none of this test's business. Only a URL that claims to be
// THIS project has to be canonical.

const CANONICAL_OWNER = 'Absence0760';
const CANONICAL_REPO = 'feohledger';
const CANONICAL = `${CANONICAL_OWNER}/${CANONICAL_REPO}`;

const RAW = import.meta.glob('/src/**/*.{svelte,ts}', {
	query: '?raw',
	import: 'default',
	eager: true,
}) as Record<string, string>;

const FILES: [string, string][] = Object.entries(RAW)
	.map(([path, source]) => [path.replace(/^\/src\//, ''), source] as [string, string])
	.filter(([path]) => !path.endsWith('.test.ts'))
	.sort(([a], [b]) => a.localeCompare(b));

/** Every `github.com/<owner>/<repo>` occurrence, with the file that holds it. */
function githubLinks(): { file: string; owner: string; repo: string }[] {
	const found: { file: string; owner: string; repo: string }[] = [];
	for (const [file, src] of FILES) {
		for (const m of src.matchAll(/github\.com\/([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+)/g)) {
			found.push({ file, owner: m[1], repo: m[2] });
		}
	}
	return found;
}

/**
 * Names this project has gone by, or been mislabelled with. A link carrying
 * one of these is claiming to be us, so it must be the canonical repo.
 *
 * `jaredhoward` is here as an OWNER check, not a repo one: it is a real,
 * unrelated GitHub user, so a link under it is always wrong regardless of what
 * repo path follows.
 */
const SELF_REFERENTIAL = /^(feohledger|project-account-payables|account-payables|betterap)$/i;

describe('outbound GitHub links to this project are canonical', () => {
	it('every self-referential link is Absence0760/feohledger', () => {
		const offenders = githubLinks()
			.filter(({ owner, repo }) => SELF_REFERENTIAL.test(repo) || owner === 'jaredhoward')
			.filter(({ owner, repo }) => `${owner}/${repo}` !== CANONICAL)
			.map(({ file, owner, repo }) => `${file}: github.com/${owner}/${repo}`);
		expect(offenders).toEqual([]);
	});

	it('never links under the unrelated `jaredhoward` account', () => {
		// Called out separately from the rule above so a regression names the
		// actual hazard rather than reading as a stale-name nit.
		const offenders = githubLinks()
			.filter(({ owner }) => owner === 'jaredhoward')
			.map(({ file }) => file);
		expect(offenders).toEqual([]);
	});
});

describe('no pre-rename brand names survive in shipped source', () => {
	it.each([
		['betterap', /betterap/i],
		['project-account-payables', /project-account-payables/i],
	])('no file mentions %s', (_label, pattern) => {
		const offenders = FILES.filter(([, src]) => pattern.test(src)).map(([path]) => path);
		expect(offenders).toEqual([]);
	});
});
