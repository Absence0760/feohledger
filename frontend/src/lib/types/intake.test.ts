import { describe, it, expect } from 'vitest';
import {
	INTAKE_STATUSES,
	INTAKE_STATUS_LABEL_KEYS,
	INTAKE_TYPES,
	INTAKE_TYPE_LABEL_KEYS,
	INTAKE_FORM_FIELDS,
	intakeStatusLabelKey,
	intakeTypeLabelKey
} from './intake';
import { en } from '#lib/i18n/locales/en.ts';

/**
 * Map-completeness guard for the intake label maps.
 *
 * `Record<IntakeStatus, MessageKey>` already fails `pnpm check` when a status
 * is added to the union with no key — but a key that names nothing in the
 * catalogue typechecks fine and renders the raw key string in the badge
 * (`m()` falls back key → raw). So the compile-time half is the union and
 * this is the runtime half: every key a badge can reach must exist in `en`.
 *
 * These maps were hardcoded English until this test existed, which is how a
 * German user got a translated Reopen confirm ("Zurück auf „Offen“") beside
 * an untranslated `Open` badge.
 */
describe('INTAKE_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every status', () => {
		for (const status of INTAKE_STATUSES) {
			const key = INTAKE_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('never uses the raw enum value as its own label', () => {
		for (const status of INTAKE_STATUSES) {
			expect(en[INTAKE_STATUS_LABEL_KEYS[status]]).not.toBe(status);
		}
	});

	it('intakeStatusLabelKey resolves a known status and returns null otherwise', () => {
		for (const status of INTAKE_STATUSES) {
			expect(intakeStatusLabelKey(status)).toBe(INTAKE_STATUS_LABEL_KEYS[status]);
		}
		// The caller renders the raw value — visible and searchable — rather
		// than a blank badge, so a status the backend adds first degrades
		// gracefully until this map catches up.
		expect(intakeStatusLabelKey('some_future_status')).toBeNull();
	});
});

describe('INTAKE_TYPE_LABEL_KEYS', () => {
	it('names a real catalogue key for every request type', () => {
		for (const type of INTAKE_TYPES) {
			const key = INTAKE_TYPE_LABEL_KEYS[type];
			expect(key, `${type} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${type} → "${key}" is not in the catalogue`).toContain(key);
		}
	});

	it('intakeTypeLabelKey returns null for an unknown type', () => {
		expect(intakeTypeLabelKey('other')).toBe('intake.type.other');
		expect(intakeTypeLabelKey('quantum_hardware')).toBeNull();
	});
});

describe('INTAKE_FORM_FIELDS', () => {
	it('every questionnaire field names a real catalogue key', () => {
		for (const type of INTAKE_TYPES) {
			for (const field of INTAKE_FORM_FIELDS[type]) {
				expect(
					Object.keys(en),
					`${type}.${field.key} → "${field.labelKey}" is not in the catalogue`
				).toContain(field.labelKey);
			}
		}
	});

	it('covers every request type, so no type falls back to another’s questions', () => {
		for (const type of INTAKE_TYPES) {
			expect(INTAKE_FORM_FIELDS[type]?.length, `${type} has no questionnaire`).toBeGreaterThan(0);
		}
	});
});
