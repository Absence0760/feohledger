import { describe, it, expect } from 'vitest';
import {
	CONTRACT_STATUSES,
	CONTRACT_TYPES,
	CONTRACT_TYPE_LABEL_KEYS,
	STATUS_LABEL_KEYS,
	contractStatusLabelKey,
	contractTypeLabelKey
} from './contract';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the contract label maps.
 *
 * `Record<ContractStatus, MessageKey>` already fails `pnpm check` when a
 * status joins the union with no key — but a key that names nothing in the
 * catalogue typechecks fine and renders the raw key string in the badge
 * (`m()` falls back key → raw). So the compile-time half is the union and
 * this is the runtime half: every key a badge can reach must exist in `en`.
 *
 * These maps were hardcoded English until this test existed, which is how the
 * `/contracts` list rendered a translated lifecycle action beside an
 * untranslated `Active` badge.
 */
describe('STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every status', () => {
		for (const status of CONTRACT_STATUSES) {
			const key = STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of CONTRACT_STATUSES) {
			expect(en[STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('contractStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of CONTRACT_STATUSES) {
			expect(contractStatusLabelKey(status)).toBe(STATUS_LABEL_KEYS[status]);
		}
		// The caller renders the raw value — visible and searchable — rather
		// than a blank badge, so a status the backend adds first degrades
		// gracefully until this map catches up.
		expect(contractStatusLabelKey('renegotiating')).toBeNull();
	});
});

describe('CONTRACT_TYPE_LABEL_KEYS', () => {
	it('names a real catalogue key for every contract type', () => {
		for (const type of CONTRACT_TYPES) {
			const key = CONTRACT_TYPE_LABEL_KEYS[type];
			expect(key, `${type} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${type} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('contractTypeLabelKey returns null for an unknown type', () => {
		expect(contractTypeLabelKey('other')).toBe('contracts.type.other');
		expect(contractTypeLabelKey('barter')).toBeNull();
	});
});
