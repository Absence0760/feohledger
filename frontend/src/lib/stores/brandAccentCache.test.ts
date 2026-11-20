import { describe, expect, it } from 'vitest';
import {
	accentCacheKey,
	clearAccentCache,
	readAccentCache,
	sanitizeAccentVars,
	writeAccentCache,
	type AccentStorage
} from './brandAccentCache';
import { tenantStorageKeyForHost } from '#lib/hostRouting.ts';

const RAW = import.meta.glob(['/src/app.html'], {
	query: '?raw',
	import: 'default',
	eager: true
}) as Record<string, string>;
const appHtml = RAW['/src/app.html'];

function memoryStorage(init: Record<string, string> = {}, throws = false) {
	const data = new Map(Object.entries(init));
	const storage: AccentStorage = {
		getItem(key) {
			if (throws) throw new DOMException('denied', 'SecurityError');
			return data.has(key) ? data.get(key)! : null;
		},
		setItem(key, value) {
			if (throws) throw new DOMException('quota', 'QuotaExceededError');
			data.set(key, value);
		},
		removeItem(key) {
			if (throws) throw new DOMException('denied', 'SecurityError');
			data.delete(key);
		}
	};
	return { storage, data };
}

const GOOD = {
	'--brand-accent-dark': '#cd6256',
	'--brand-accent-light': '#c0392b',
	'--accent-strong': '#7b1d14'
};
const HOSTILE = [
	'red',
	'#fff; background: url(https://evil.test/x)',
	'#12345',
	'#1234567',
	'expression(alert(1))',
	'',
	42,
	null
];

describe('accent cache: read / write / clear', () => {
	it('round-trips the applied values under the tenant key', () => {
		const { storage, data } = memoryStorage();
		writeAccentCache(storage, 'acme', GOOD);
		expect([...data.keys()]).toEqual(['feoh_brand_accent:acme']);
		expect(readAccentCache(storage, 'acme')).toEqual(GOOD);
	});

	it('is per workspace: another tenant key reads nothing', () => {
		const { storage } = memoryStorage();
		writeAccentCache(storage, 'acme', GOOD);
		expect(readAccentCache(storage, 'globex')).toEqual({});
	});

	it('writes nothing without a tenant (platform apex)', () => {
		const { storage, data } = memoryStorage();
		writeAccentCache(storage, null, GOOD);
		expect(data.size).toBe(0);
		expect(readAccentCache(storage, null)).toEqual({});
	});

	it('an unbranded org removes a stale entry instead of storing an empty one', () => {
		const { storage, data } = memoryStorage({ [accentCacheKey('acme')]: JSON.stringify(GOOD) });
		writeAccentCache(storage, 'acme', {});
		expect(data.size).toBe(0);
	});

	it('the fetched branding overwrites a stale entry', () => {
		const { storage } = memoryStorage({ [accentCacheKey('acme')]: JSON.stringify(GOOD) });
		writeAccentCache(storage, 'acme', { '--brand-accent-dark': '#00ff00' });
		expect(readAccentCache(storage, 'acme')).toEqual({ '--brand-accent-dark': '#00ff00' });
	});

	it('clear removes the entry', () => {
		const { storage, data } = memoryStorage({ [accentCacheKey('acme')]: JSON.stringify(GOOD) });
		clearAccentCache(storage, 'acme');
		expect(data.size).toBe(0);
	});

	it('never throws when storage does, and reads as empty', () => {
		const { storage } = memoryStorage({}, true);
		expect(() => writeAccentCache(storage, 'acme', GOOD)).not.toThrow();
		expect(() => clearAccentCache(storage, 'acme')).not.toThrow();
		expect(readAccentCache(storage, 'acme')).toEqual({});
		expect(readAccentCache(null, 'acme')).toEqual({});
	});

	it('reads malformed JSON as empty', () => {
		const { storage } = memoryStorage({ [accentCacheKey('acme')]: '{not json' });
		expect(readAccentCache(storage, 'acme')).toEqual({});
	});
});

describe('accent cache: validation', () => {
	for (const bad of HOSTILE) {
		it(`drops ${JSON.stringify(bad)} on read and on write`, () => {
			const tampered = { '--brand-accent-light': bad, '--accent-strong': '#7b1d14' };
			const { storage } = memoryStorage({ [accentCacheKey('acme')]: JSON.stringify(tampered) });
			expect(readAccentCache(storage, 'acme')).toEqual({ '--accent-strong': '#7b1d14' });
			expect(sanitizeAccentVars(tampered)).toEqual({ '--accent-strong': '#7b1d14' });
		});
	}

	it('keeps only the brand accent props', () => {
		expect(sanitizeAccentVars({ ...GOOD, '--bg': '#000000', color: '#fff' })).toEqual(GOOD);
	});

	it('drops a pre-§477 `--accent` entry: an inline --accent would beat both palettes', () => {
		expect(sanitizeAccentVars({ '--accent': '#c0392b', '--accent-strong': '#7b1d14' })).toEqual({
			'--accent-strong': '#7b1d14'
		});
		expect(sanitizeAccentVars('#fff')).toEqual({});
		expect(sanitizeAccentVars(null)).toEqual({});
	});
});

/**
 * `<script id="accent-boot">` runs before any bundle, so it repeats the read
 * above instead of importing it. Execute it against fakes and compare with
 * `readAccentCache` keyed by the real `tenantStorageKeyForHost`.
 */
describe('app.html accent-boot script', () => {
	const OPEN = '<script id="accent-boot">';
	const start = appHtml.indexOf(OPEN);
	const end = start < 0 ? -1 : appHtml.indexOf('</script>', start + OPEN.length);
	const script = start < 0 || end < 0 ? undefined : appHtml.slice(start + OPEN.length, end);

	function run(hostname: string, entries: Record<string, string>, storageThrows = false) {
		const applied = new Map<string, string>();
		const doc = {
			documentElement: {
				style: { setProperty: (p: string, v: string) => applied.set(p, v) }
			}
		};
		const { storage } = memoryStorage(entries);
		const win: Record<string, unknown> = { location: { hostname } };
		Object.defineProperty(win, 'localStorage', {
			get() {
				if (storageThrows) throw new DOMException('denied', 'SecurityError');
				return storage;
			}
		});
		new Function('window', 'document', script!)(win, doc);
		return Object.fromEntries(applied);
	}

	it('exists, before %sveltekit.head%', () => {
		expect(script, 'src/app.html must carry <script id="accent-boot">').toBeDefined();
		expect(appHtml.indexOf(OPEN)).toBeLessThan(appHtml.indexOf('%sveltekit.head%'));
	});

	// Each host with the platform-domain lists it may be served under; the
	// script knows none of them, so it must agree with every one.
	const HOSTS: [string, string[]][] = [
		['acme.localhost', []],
		['acme.localhost', ['localhost']],
		['acme.feohledger.com', []],
		['acme.feohledger.com', ['feohledger.com']],
		['ap.acmecorp.com', ['feohledger.com', 'localhost']]
	];

	for (const [host, domains] of HOSTS) {
		it(`${host} (${domains.join(',') || 'legacy'}) applies what the store would read`, () => {
			const key = tenantStorageKeyForHost(host, domains)!;
			const entries = { [accentCacheKey(key)]: JSON.stringify(GOOD) };
			expect(run(host, entries)).toEqual(readAccentCache(memoryStorage(entries).storage, key));
			expect(run(host, entries)).toEqual(GOOD);
		});
	}

	it('ignores hostile values exactly as the store does', () => {
		for (const bad of HOSTILE) {
			const tampered = JSON.stringify({
				'--brand-accent-dark': bad,
				'--accent': '#c0392b',
				'--accent-strong': '#7b1d14',
				'--bg': '#000'
			});
			const entries = { [accentCacheKey('acme')]: tampered };
			expect(run('acme.localhost', entries)).toEqual(
				readAccentCache(memoryStorage(entries).storage, 'acme')
			);
		}
	});

	it('applies nothing with no entry, another tenant\'s entry, bad JSON, or throwing storage', () => {
		expect(run('acme.localhost', {})).toEqual({});
		expect(run('acme.localhost', { [accentCacheKey('globex')]: JSON.stringify(GOOD) })).toEqual({});
		expect(run('acme.localhost', { [accentCacheKey('acme')]: '{nope' })).toEqual({});
		expect(run('acme.localhost', { [accentCacheKey('acme')]: JSON.stringify(GOOD) }, true)).toEqual({});
	});
});
