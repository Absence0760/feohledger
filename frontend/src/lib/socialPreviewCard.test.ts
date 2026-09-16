import { describe, expect, it } from 'vitest';

// The link-preview card (Slack, LinkedIn, X, iMessage unfurls) is plain markup
// in `src/app.html`. It cannot live in a `<svelte:head>`: this is an
// adapter-static SPA, unfurl crawlers run no JavaScript, and the only document
// they ever receive is the shell SvelteKit renders from that template
// (`build/index.html`, served for every path and every tenant host). So the
// template is the unit under test, read as text.
//
// Files are read through Vite's `import.meta.glob`, not `node:fs` — the
// frontend's `tsconfig.json` sets `"types": []`, so a node import type-checks
// as an error under `pnpm check` even though vitest runs it fine. Same idiom as
// `canonicalRepoLinks.test.ts`. `exhaustive` is what lets a glob reach a
// dot-path (`.env.development`, `.github/`); the paths are literal, so it
// widens nothing else.

function readOne(glob: Record<string, string>, label: string): string {
	const values = Object.values(glob);
	if (values.length !== 1) throw new Error(`expected exactly one file for ${label}, got ${values.length}`);
	return values[0];
}

const TEMPLATE = readOne(
	import.meta.glob('/src/app.html', { query: '?raw', import: 'default', eager: true }),
	'src/app.html',
);
const LANDING = readOne(
	import.meta.glob('/src/lib/components/marketing/Landing.svelte', {
		query: '?raw',
		import: 'default',
		eager: true,
	}),
	'Landing.svelte',
);
const ENV_DEVELOPMENT = readOne(
	import.meta.glob('/.env.development', {
		query: '?raw',
		import: 'default',
		eager: true,
		exhaustive: true,
	}),
	'.env.development',
);
const AWS_DEPLOY = readOne(
	import.meta.glob('../../../.github/workflows/aws-deploy.yml', {
		query: '?raw',
		import: 'default',
		eager: true,
		exhaustive: true,
	}),
	'aws-deploy.yml',
);
const VM_DEPLOY = readOne(
	import.meta.glob('../../../deploy/deploy.sh', { query: '?raw', import: 'default', eager: true }),
	'deploy/deploy.sh',
);
// `?inline` yields a base64 data URL, which keeps the bytes intact. `?raw`
// would decode the PNG as UTF-8 and mangle every byte above 0x7F — including
// the low byte of a 1200 px width.
const STATIC_PNGS = import.meta.glob('/static/*.png', {
	query: '?inline',
	import: 'default',
	eager: true,
}) as Record<string, string>;

const SITE_URL_VAR = 'PUBLIC_SITE_URL';
const PLACEHOLDER = `%sveltekit.env.${SITE_URL_VAR}%`;

/**
 * `html` with its comments removed, so a commented-out tag never counts.
 *
 * One pass is not enough: removing a comment can join the text around it into
 * a new `<!--`. So removal repeats until nothing changes, and an unterminated
 * comment runs to the end, as it does in HTML. The result never contains `<!--`.
 */
function withoutComments(html: string): string {
	let out = html;
	let previous: string;
	do {
		previous = out;
		out = out.replace(/<!--[\s\S]*?-->/g, '');
	} while (out !== previous);
	const unterminated = out.indexOf('<!--');
	return unterminated === -1 ? out : out.slice(0, unterminated);
}

const MARKUP = withoutComments(TEMPLATE);

type Meta = { key: string; content: string };

/** Every `<meta name|property="…" content="…">` in the template, in order. */
function metaTags(markup: string): Meta[] {
	const tags: Meta[] = [];
	for (const [, attrs] of markup.matchAll(/<meta\s([^>]*?)\/?>/g)) {
		const attr = Object.fromEntries(
			[...attrs.matchAll(/([a-z:-]+)="([^"]*)"/gi)].map(([, k, v]) => [k.toLowerCase(), v]),
		);
		const key = attr.property ?? attr.name;
		if (key !== undefined && attr.content !== undefined) tags.push({ key, content: attr.content });
	}
	return tags;
}

/** The single value of one meta key; fails if it is absent or duplicated. */
function meta(key: string, markup = MARKUP): string {
	const found = metaTags(markup).filter((t) => t.key === key);
	expect(found, `<meta> "${key}" must appear exactly once in src/app.html`).toHaveLength(1);
	return found[0].content;
}

/**
 * SvelteKit's own substitution for `app.html` env placeholders
 * (`@sveltejs/kit/src/core/sync/write_server.js`): each `%sveltekit.env.NAME%`
 * becomes `env[NAME] ?? ""` at render time — the adapter-static fallback render
 * that writes `build/index.html` included. An unset variable is therefore an
 * empty string, never a build error and never a leftover literal.
 */
function renderTemplate(env: Record<string, string>): string {
	return MARKUP.replace(/%sveltekit\.env\.([^%]+)%/g, (_m, name: string) => env[name] ?? '');
}

/** Width and height from a PNG's IHDR chunk, read from a base64 data URL. */
function pngSize(dataUrl: string): { width: number; height: number } {
	const [, base64] = dataUrl.split(',', 2);
	const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
	const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
	expect([...bytes.subarray(0, 8)], 'not a PNG file').toEqual(signature);
	expect(String.fromCharCode(...bytes.subarray(12, 16)), 'first chunk must be IHDR').toBe('IHDR');
	const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
	return { width: view.getUint32(16), height: view.getUint32(20) };
}

/** The root-relative path an image URL resolves to once the origin is stripped. */
function imagePath(content: string): string {
	expect(content.startsWith(PLACEHOLDER), `image URL must be prefixed by ${PLACEHOLDER}`).toBe(true);
	return content.slice(PLACEHOLDER.length);
}

describe('social preview card in src/app.html', () => {
	it('declares every Open Graph and Twitter tag the card needs, once each', () => {
		expect(meta('og:type')).toBe('website');
		expect(meta('og:site_name')).toBe('FeohLedger');
		expect(meta('twitter:card')).toBe('summary_large_image');
		for (const key of [
			'description',
			'og:title',
			'og:description',
			'og:image',
			'og:image:width',
			'og:image:height',
			'og:image:alt',
			'twitter:image',
			'twitter:image:alt',
		]) {
			expect(meta(key).trim(), `<meta> "${key}" must not be empty`).not.toBe('');
		}
	});

	it('carries the landing page copy, so the card and the page cannot drift', () => {
		const eyebrow = LANDING.match(/<span class="eyebrow">([^<]+)<\/span>/)?.[1].trim();
		const lede = LANDING.match(/<p class="lede">([\s\S]*?)<\/p>/)?.[1].replace(/\s+/g, ' ').trim();
		expect(eyebrow, 'Landing.svelte eyebrow not found').toBeTruthy();
		expect(lede, 'Landing.svelte lede not found').toBeTruthy();

		expect(meta('og:title')).toBe(`FeohLedger — ${eyebrow}`);
		expect(meta('og:description')).toBe(lede);
		expect(meta('description')).toBe(lede);
	});

	it('points og:image and twitter:image at the same PNG under frontend/static', () => {
		expect(meta('twitter:image')).toBe(meta('og:image'));
		expect(meta('twitter:image:alt')).toBe(meta('og:image:alt'));

		const path = imagePath(meta('og:image'));
		expect(path).toMatch(/^\/[^/]/);
		expect(
			Object.keys(STATIC_PNGS),
			`${path} is not in frontend/static — the card would unfurl without an image`,
		).toContain(`/static${path}`);
	});

	it('declares the dimensions the PNG actually has', () => {
		const dataUrl = STATIC_PNGS[`/static${imagePath(meta('og:image'))}`];
		expect(dataUrl, 'og:image file missing from frontend/static').toBeDefined();
		const { width, height } = pngSize(dataUrl);

		expect(meta('og:image:width')).toBe(String(width));
		expect(meta('og:image:height')).toBe(String(height));
		// The size every major unfurler crops `summary_large_image` to (1.91:1).
		expect({ width, height }).toEqual({ width: 1200, height: 630 });
	});

	it('uses PUBLIC_SITE_URL as its only env placeholder', () => {
		const names = [...TEMPLATE.matchAll(/%sveltekit\.env\.([^%]+)%/g)].map((m) => m[1]);
		expect(new Set(names)).toEqual(new Set([SITE_URL_VAR]));
	});

	it('renders an absolute image URL when PUBLIC_SITE_URL is set', () => {
		const html = renderTemplate({ [SITE_URL_VAR]: 'https://feohledger.com' });
		for (const key of ['og:image', 'twitter:image']) {
			const url = new URL(meta(key, html)); // throws on a relative URL
			expect(url.href).toBe('https://feohledger.com/og-image.png');
		}
	});

	it('renders a root-relative path, not a broken literal, when PUBLIC_SITE_URL is unset', () => {
		// The documented degraded case (docs/environment.md): a bare `pnpm build`
		// succeeds and the card still names the right file. Deployed builds never
		// reach it — the next test pins that both deploy paths set the variable.
		const html = renderTemplate({});
		for (const key of ['og:image', 'twitter:image']) {
			expect(meta(key, html)).toBe('/og-image.png');
		}
		expect(html).not.toContain('%sveltekit.env');
		expect(html).not.toContain('undefined');
	});

	it('is given a real origin by every deploy build, and a dev default locally', () => {
		const devValue = ENV_DEVELOPMENT.match(/^PUBLIC_SITE_URL=(.*)$/m)?.[1].trim();
		expect(devValue, 'frontend/.env.development must set PUBLIC_SITE_URL').toBeTruthy();
		// An origin and nothing else: a trailing slash would render `//og-image.png`.
		expect(new URL(devValue!).origin).toBe(devValue);

		// aws-deploy.yml derives it from APP_URL (failing when empty, trailing
		// slash stripped); deploy.sh from APP_DOMAIN, which its preflight requires.
		expect(AWS_DEPLOY).toContain('PUBLIC_SITE_URL="${APP_URL%/}" pnpm build');
		expect(AWS_DEPLOY).toMatch(/if \[ -z "\$APP_URL" \]; then[\s\S]*?exit 1/);
		expect(VM_DEPLOY).toContain('-e PUBLIC_SITE_URL="https://${APP_DOMAIN}"');
	});

	it('has no title element, so a route-set (white-label) title still wins', () => {
		expect(MARKUP).not.toMatch(/<title[\s>]/i);
	});
});
