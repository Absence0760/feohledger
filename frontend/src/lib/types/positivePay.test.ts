import { describe, it, expect } from 'vitest';
import {
	BANK_FORMATS,
	BANK_FORMAT_LABEL_KEYS,
	POSITIVE_PAY_FILE_TYPES,
	POSITIVE_PAY_FILE_TYPE_LABEL_KEYS,
	POSITIVE_PAY_STATUSES,
	POSITIVE_PAY_STATUS_LABEL_KEYS,
	bankFormatLabelKey,
	positivePayFileTypeLabelKey,
	positivePayStatusLabelKey
} from './positivePay';
import { CATALOGUE_LOADERS } from '#lib/i18n/catalogues.ts';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the Positive Pay label maps.
 *
 * `Record<…, MessageKey>` already fails `pnpm check` when a value joins a
 * union with no key — but a key naming nothing in the catalogue typechecks
 * fine and renders the raw key string (`m()` falls back key → raw). This is
 * the runtime half.
 */
describe('POSITIVE_PAY_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every status', () => {
		for (const status of POSITIVE_PAY_STATUSES) {
			const key = POSITIVE_PAY_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of POSITIVE_PAY_STATUSES) {
			expect(positivePayStatusLabelKey(status)).toBe(POSITIVE_PAY_STATUS_LABEL_KEYS[status]);
		}
		expect(positivePayStatusLabelKey('acknowledged')).toBeNull();
	});
});

describe('BANK_FORMAT_LABEL_KEYS', () => {
	it('names a real, non-empty catalogue key for every formatter', () => {
		for (const fmt of BANK_FORMATS) {
			const key = BANK_FORMAT_LABEL_KEYS[fmt];
			expect(key, `${fmt} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${fmt} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key].trim().length).toBeGreaterThan(0);
			// The raw wire value is what the list's Format column and the detail
			// meta pill used to print; a key resolving back to it would be no fix.
			expect(en[key]).not.toBe(fmt);
		}
	});

	it('resolves a known formatter and returns null otherwise', () => {
		for (const fmt of BANK_FORMATS) {
			expect(bankFormatLabelKey(fmt)).toBe(BANK_FORMAT_LABEL_KEYS[fmt]);
		}
		// The backend's formatter registry is pluggable (`get_positive_pay_formatter`
		// falls an unknown key back to csv), so a per-bank adapter can ship before
		// this map catches up — the caller then renders the raw value.
		expect(bankFormatLabelKey('nacha_fixed')).toBeNull();
	});

	it('keeps the CSV format name verbatim in every locale', async () => {
		// A file-format name, not prose — the same data-value convention that
		// keeps the ACH / BACS / CHAPS scheme names untranslated. A locale that
		// "translated" it would name a format no bank publishes.
		for (const loc of SUPPORTED_LOCALES) {
			const dict = (await CATALOGUE_LOADERS[loc]()) as Record<string, string>;
			expect(dict[BANK_FORMAT_LABEL_KEYS.csv], `${loc} renamed the CSV format`).toBe('CSV');
		}
	});
});

describe('POSITIVE_PAY_FILE_TYPE_LABEL_KEYS', () => {
	it('names a real catalogue key for every file type', () => {
		for (const fileType of POSITIVE_PAY_FILE_TYPES) {
			const key = POSITIVE_PAY_FILE_TYPE_LABEL_KEYS[fileType];
			expect(key, `${fileType} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${fileType} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('resolves a known file type and returns null otherwise', () => {
		// The label is interpolated into `positivePay.fileLabel`, so an
		// unrecognised type must degrade to its raw value rather than a blank
		// row label.
		expect(positivePayFileTypeLabelKey('check_issue')).toBe('positivePay.fileType.checkIssue');
		expect(positivePayFileTypeLabelKey('wire_authorization')).toBeNull();
	});
});
