#!/usr/bin/env node
// Tests for the followups headline-count guard.
// `node --test scripts/check_followups_count.test.mjs`
//
// The guard is arithmetic, so the interesting cases are not "does it add up" —
// they are the three real drifts it exists to have caught, and the shapes that
// would make it fire against a file that is actually correct. A guard that
// reports drift on a correct file gets switched off, and a switched-off guard
// protects nothing.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { check, derive } from './check_followups_count.mjs';

/** A minimal file whose header agrees with its body. */
function file({ header = '**3 open: 2 (c) · 1 (a) · 0 (b)**', c = 2, a = 1, b = 0 } = {}) {
	const item = (n) => `- [ ] item ${n}\n\n      **Durable fix:** something.\n`;
	return [
		'# Open follow-ups\n',
		`${header} — re-derived from the file, never carried forward.\n`,
		'## (c) Feature work — sized and unstarted\n',
		...Array.from({ length: c }, (_, i) => item(`c${i}`)),
		'## (a) Blocked on external credentials, accounts, or hardware\n',
		...Array.from({ length: a }, (_, i) => item(`a${i}`)),
		'## (b) Operator steps on merged code\n',
		...Array.from({ length: b }, (_, i) => item(`b${i}`))
	].join('\n');
}

test('an agreeing file reports nothing', () => {
	assert.deepEqual(check(file()), []);
});

test('derive counts per section, not by the entry marker', () => {
	// The preamble makes the section heading authoritative precisely because an
	// entry's own marker has disagreed with the section it sits under before
	// (the shard-baseline entry, labelled `(c)` beneath `## (b)`). So a marker
	// in the body text must not move the count.
	const markdown = [
		'**1 open: 0 (c) · 0 (a) · 1 (b)**\n',
		'## (b) Operator steps on merged code\n',
		'- [ ] an entry whose body mentions (c) and (a) in prose\n'
	].join('\n');

	assert.deepEqual(derive(markdown), { total: 1, a: 0, b: 1, c: 0 });
	assert.deepEqual(check(markdown), []);
});

test('the three real drifts would each have been caught', () => {
	// Every one of these shipped to `main`.
	const drifts = [
		// The seven `/polish-ui` entries: `52 open: 37 (c)` over 59.
		{ header: '**52 open: 37 (c) · 1 (a) · 0 (b)**', c: 44, a: 1, b: 0 },
		// #455's dashboard-greeting entry: `50 open: 35 (c)` over 51/36.
		{ header: '**50 open: 35 (c) · 10 (a) · 5 (b)**', c: 36, a: 10, b: 5 },
		// #467's three panelisation entries: `55 open: 40 (c)` over 58/43.
		{ header: '**55 open: 40 (c) · 9 (a) · 6 (b)**', c: 43, a: 9, b: 6 }
	];

	for (const drift of drifts) {
		const problems = check(file(drift));
		assert.ok(problems.length > 0, `not caught: ${drift.header}`);
		assert.ok(
			problems.some((p) => p.includes('the header claims')),
			`caught, but not as a count mismatch: ${drift.header}`
		);
	}
});

test('a category that drifts while the total stays right is still caught', () => {
	// One `(c)` added and one `(a)` pruned in the same change: the total is
	// honest and both categories are wrong. A guard checking only the total
	// would pass this.
	const problems = check(file({ header: '**3 open: 1 (c) · 2 (a) · 0 (b)**', c: 2, a: 1 }));

	assert.equal(problems.filter((p) => p.includes('the header claims')).length, 2);
	assert.ok(!problems.some((p) => p.includes('total')));
});

test('an item outside every category heading is named as such', () => {
	// The preamble's other documented shape. Here the total is right and the
	// categories are right; only their sum disagrees, which no per-figure
	// comparison can explain on its own.
	const markdown = [
		'**2 open: 1 (c) · 0 (a) · 0 (b)**\n',
		'- [ ] an item stranded above every category heading\n',
		'## (c) Feature work — sized and unstarted\n',
		'- [ ] a properly filed item\n'
	].join('\n');

	const problems = check(markdown);
	assert.ok(problems.some((p) => p.includes('sit outside')));
});

test('a nested checkbox inside an entry body is not a sibling entry', () => {
	// Sub-checklists appear inside entries. They are indented, so the
	// line-anchored match must not count them — if it did, the guard would fire
	// against a correct file, which is how guards get deleted.
	const markdown = [
		'**1 open: 1 (c) · 0 (a) · 0 (b)**\n',
		'## (c) Feature work — sized and unstarted\n',
		'- [ ] one entry\n',
		'      - [ ] a sub-step of that entry\n',
		'      - [ ] another sub-step\n'
	].join('\n');

	assert.deepEqual(derive(markdown), { total: 1, a: 0, b: 0, c: 1 });
	assert.deepEqual(check(markdown), []);
});

test('a completed item does not count as open', () => {
	const markdown = [
		'**1 open: 1 (c) · 0 (a) · 0 (b)**\n',
		'## (c) Feature work — sized and unstarted\n',
		'- [x] landed already\n',
		'- [ ] still open\n'
	].join('\n');

	assert.deepEqual(check(markdown), []);
});

test('a missing header is reported rather than passing silently', () => {
	const problems = check('# Open follow-ups\n\nNo headline here.\n');
	assert.equal(problems.length, 1);
	assert.ok(problems[0].includes('no headline count found'));
});

test('a failure says how to fix it', () => {
	const problems = check(file({ header: '**9 open: 9 (c) · 0 (a) · 0 (b)**' }));
	assert.ok(problems.at(-1).includes('Re-derive with'));
});
