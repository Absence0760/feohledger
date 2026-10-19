#!/usr/bin/env node
// Tests for the dev-accounts build guard.
// `node --test scripts/check_dev_accounts_stripped.test.mjs`
//
// The cases worth pinning are the ones that would make the guard pass for the
// wrong reason: a needle pattern that reads nothing out of the real module,
// and a leak in a nested chunk the walk failed to reach.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	ACCOUNTS_MODULE,
	PANEL_MARKER,
	findLeaks,
	needlesFrom
} from './check_dev_accounts_stripped.mjs';

const GUARD = fileURLToPath(new URL('./check_dev_accounts_stripped.mjs', import.meta.url));
const REPO = fileURLToPath(new URL('..', import.meta.url));

test('reads every seeded address out of the real accounts module', () => {
	const needles = needlesFrom(readFileSync(join(REPO, ACCOUNTS_MODULE), 'utf8'));
	assert.ok(needles.includes('demo@acme.com'));
	assert.ok(needles.includes('admin@techflow.com'));
	assert.ok(needles.includes(PANEL_MARKER));
	assert.ok(needles.length >= 7, `only ${needles.length} needles`);
});

test('ignores prose that is not a quoted address', () => {
	assert.deepEqual(needlesFrom("// see `demo@acme.com`\nconst x = 'not an address';"), [
		PANEL_MARKER
	]);
});

test('finds a leak in a nested chunk, and nothing in a clean build', () => {
	const dir = mkdtempSync(join(tmpdir(), 'devacct-'));
	mkdirSync(join(dir, '_app', 'immutable', 'chunks'), { recursive: true });
	writeFileSync(join(dir, 'index.html'), '<html>sign in</html>');
	writeFileSync(join(dir, '_app', 'immutable', 'chunks', 'a.js'), 'const x=1;');
	assert.deepEqual(findLeaks(dir, ['demo@acme.com', PANEL_MARKER]), []);

	writeFileSync(
		join(dir, '_app', 'immutable', 'chunks', 'b.js'),
		'e("section",{"data-dev-accounts":""});const a="demo@acme.com";'
	);
	const leaks = findLeaks(dir, ['demo@acme.com', PANEL_MARKER]).map((l) => l.needle).sort();
	assert.deepEqual(leaks, ['data-dev-accounts', 'demo@acme.com']);
});

test('the CLI fails on a leak and on a missing build, passes on a clean one', () => {
	const run = (dir) => {
		try {
			execFileSync('node', [GUARD, dir], { cwd: REPO, stdio: 'pipe' });
			return 0;
		} catch (err) {
			return err.status;
		}
	};
	const clean = mkdtempSync(join(tmpdir(), 'devacct-clean-'));
	writeFileSync(join(clean, 'index.html'), '<html></html>');
	assert.equal(run(clean), 0);

	const dirty = mkdtempSync(join(tmpdir(), 'devacct-dirty-'));
	writeFileSync(join(dirty, 'app.js'), 'x="admin@techflow.com"');
	assert.equal(run(dirty), 1);

	assert.equal(run(join(clean, 'does-not-exist')), 1);
});
