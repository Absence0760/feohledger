#!/usr/bin/env node
// `docs/followups.md`'s headline count must equal what the file actually holds.
// Plain node, no deps.
//
// The file opens with a line of the form
//
//   **58 open: 43 (c) · 9 (a) · 6 (b)** — re-derived from the file, never
//   carried forward.
//
// and that count is stored beside the thing it counts, which is a shape with no
// way to notice when it goes wrong. It has now gone wrong three times, each
// time because a change appended an entry and did not re-derive the header:
//
//   1. `52 open: 37 (c)` over a file holding 59 — the seven `/polish-ui`
//      entries.
//   2. #455's dashboard-greeting entry, leaving `main` on `50 open: 35 (c)`
//      over 51/36, an off-by-one the next batch then inherited.
//   3. The settings-page panelisation (#467) added three `(c)` entries, leaving
//      `55 open: 40 (c)` over 58/43.
//
// The file's own preamble documents the first two AND states the lesson —
// "re-deriving it is the last step of any change that adds or prunes an entry".
// The third happened anyway. That is the case for a guard rather than more
// prose: a convention that has failed every time it was tested is not a
// convention, and this is the repo's established answer (`check:icons`,
// `check:einvoice-messages`, `check:warning-messages` all exist for drift a
// human is supposed to maintain by hand).
//
// Three questions, and the second two are the failure shapes the preamble
// warns about, which a bare total would miss:
//
//   1. Does the header's total equal the number of `- [ ]` items in the file?
//   2. Does each per-category figure equal the items under that `## (x)`
//      heading? A change that adds one `(c)` and prunes one `(a)` keeps the
//      total honest while making both categories wrong.
//   3. Do the three categories sum to the total? This is what catches an item
//      that sits above every category heading, or under a heading this script
//      does not know — an item the per-section walk cannot see and which would
//      otherwise make (1) and (2) disagree with no indication of why.
//
// The section heading is authoritative, per the preamble, so that is what this
// counts — an entry's own `(c)`/`(a)`/`(b)` marker is not read at all.
//
// This FAILS rather than warns. It is an exact comparison of a number against
// the list it describes, so a finding is arithmetic, not a prompt to go look.
//
// Usage: node scripts/check_followups_count.mjs

import { readFileSync } from 'node:fs';

export const FOLLOWUPS = 'docs/followups.md';

/** The headline, e.g. `**58 open: 43 (c) · 9 (a) · 6 (b)**`. */
const HEADER = /\*\*(\d+) open:\s*(\d+) \(c\)\s*·\s*(\d+) \(a\)\s*·\s*(\d+) \(b\)\*\*/;

/** `## (c) Feature work — sized and unstarted` → `c`. */
const CATEGORY_HEADING = /^## \(([abc])\)/;

/**
 * An open item. Deliberately anchored at the line start: a `- [ ]` nested
 * inside an entry's own body is part of that entry's prose, not a sibling.
 */
const OPEN_ITEM = /^- \[ \]/;

export function derive(markdown) {
	const counts = { a: 0, b: 0, c: 0 };
	let total = 0;
	let category = null;

	for (const line of markdown.split('\n')) {
		const heading = CATEGORY_HEADING.exec(line);
		if (heading) {
			category = heading[1];
			continue;
		}
		if (!OPEN_ITEM.test(line)) continue;
		total += 1;
		// An item before any category heading counts toward the total and
		// toward no category, which is precisely the disagreement question 3
		// exists to surface.
		if (category) counts[category] += 1;
	}

	return { total, ...counts };
}

export function check(markdown) {
	const stated = HEADER.exec(markdown);
	if (!stated) {
		return [
			`${FOLLOWUPS}: no headline count found. Expected a line matching ` +
				'`**<N> open: <N> (c) · <N> (a) · <N> (b)**`.'
		];
	}

	const [, total, c, a, b] = stated.map(Number);
	const actual = derive(markdown);
	const problems = [];

	for (const [label, claimed, found] of [
		['total', total, actual.total],
		['(c)', c, actual.c],
		['(a)', a, actual.a],
		['(b)', b, actual.b]
	]) {
		if (claimed !== found) {
			problems.push(
				`${FOLLOWUPS}: the header claims ${claimed} ${label}, the file holds ${found}.`
			);
		}
	}

	const summed = actual.a + actual.b + actual.c;
	if (summed !== actual.total) {
		problems.push(
			`${FOLLOWUPS}: ${actual.total} open items but the three categories sum to ` +
				`${summed} — ${actual.total - summed} sit outside a \`## (a)\`/\`(b)\`/\`(c)\` ` +
				'heading and are invisible to the per-category count.'
		);
	}

	if (problems.length) {
		problems.push(
			`Re-derive with: grep -c '^- \\[ \\]' ${FOLLOWUPS} (and per section), then ` +
				'update the header. It is the last step of any change that adds or prunes ' +
				'an entry — see that file\'s own preamble.'
		);
	}

	return problems;
}

// `node --test` imports this file, so only run when invoked directly.
if (import.meta.url === `file://${process.argv[1]}`) {
	const problems = check(readFileSync(FOLLOWUPS, 'utf8'));
	if (problems.length) {
		for (const problem of problems) console.error(problem);
		process.exit(1);
	}
	console.log(`${FOLLOWUPS}: headline count agrees with the file.`);
}
