#!/usr/bin/env node
// The production frontend build must carry no dev-only seeded login.
// Plain node, no deps.
//
// The login page's "Dev accounts" panel (`frontend/src/lib/dev/`) lists the
// accounts `backend/scripts/seed.py` creates, with their passwords, so a
// developer can sign in with one click. It is loaded only inside an
// `import.meta.env.DEV` branch, which Vite replaces with `false` in
// `pnpm build` so the branch — and the dynamic import in it — is dropped. That
// is a property of the bundler, not of anything this repo states, so it is
// checked against the real output: the build is what ships to S3/CloudFront,
// and a seeded `demo` credential in it would be published to every visitor.
//
// The needles are read out of `devAccounts.ts` itself (every quoted address)
// plus the panel's `data-dev-accounts` marker, so a new seeded account is
// covered the day it is listed. Zero needles fails too: a regex that stopped
// matching would otherwise pass every build.
//
// Usage: node scripts/check_dev_accounts_stripped.mjs [build-dir]
//        (default frontend/build; run after `pnpm build`)

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const ACCOUNTS_MODULE = 'frontend/src/lib/dev/devAccounts.ts';
export const PANEL_MARKER = 'data-dev-accounts';
const DEFAULT_BUILD = 'frontend/build';

/** Every quoted email literal in the accounts module, plus the panel marker. */
export function needlesFrom(source) {
	const emails = [...source.matchAll(/'([^'\s@]+@[^'\s@]+\.[a-z]+)'/g)].map((m) => m[1]);
	return [...new Set(emails), PANEL_MARKER];
}

/** Every file under `dir`, recursively. */
function walk(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) out.push(...walk(path));
		else out.push(path);
	}
	return out;
}

/** `[{file, needle}]` for every needle found in any file of `dir`. */
export function findLeaks(dir, needles) {
	const leaks = [];
	for (const file of walk(dir)) {
		const text = readFileSync(file, 'latin1');
		for (const needle of needles) {
			if (text.includes(needle)) leaks.push({ file, needle });
		}
	}
	return leaks;
}

export function main(argv = process.argv.slice(2)) {
	const buildDir = argv[0] ?? DEFAULT_BUILD;
	let stats;
	try {
		stats = statSync(buildDir);
	} catch {
		stats = null;
	}
	if (!stats?.isDirectory()) {
		console.error(`No build at ${buildDir} — run \`pnpm build\` in frontend/ first.`);
		return 1;
	}
	const needles = needlesFrom(readFileSync(ACCOUNTS_MODULE, 'utf8'));
	if (needles.length < 2) {
		console.error(`Read no seeded addresses out of ${ACCOUNTS_MODULE} — the pattern broke.`);
		return 1;
	}
	const leaks = findLeaks(buildDir, needles);
	if (leaks.length > 0) {
		console.error('Dev-only seeded logins reached the production build:');
		for (const { file, needle } of leaks) console.error(`  ${file}: ${needle}`);
		console.error(
			'\nThe Dev accounts panel must stay inside an `import.meta.env.DEV` branch ' +
				'(see frontend/src/routes/login/+page.svelte).'
		);
		return 1;
	}
	console.log(`OK — none of ${needles.length} dev-account markers in ${buildDir}.`);
	return 0;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
	process.exit(main());
}
