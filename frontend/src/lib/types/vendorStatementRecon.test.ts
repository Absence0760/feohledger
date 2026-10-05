import { describe, expect, it } from 'vitest';
import {
	RECON_CLASSIFICATION_LABEL_KEYS,
	RECON_RESOLUTION_LABEL_KEYS,
	RECON_SOURCE_FORMAT_LABEL_KEYS,
	RECON_STATUS_LABEL_KEYS,
	RECON_STATUSES,
	reconSourceFormatLabelKey,
	reconStatusLabelKey,
	ambiguousSkipCount,
	formatExtractionConfidence,
	isMachineRead,
	sourceStatementFilename
} from './vendorStatementRecon';
import { en } from '#lib/i18n/locales/en.ts';

// The vendor-statement provenance surface is the only place a reviewer learns
// that a run's lines were MACHINE-READ off a PDF rather than typed or parsed
// from a CSV — so its display helpers are pinned here. They sit on the far side
// of a network boundary (the adapter's own reported confidence), which is why
// they are defensive rather than trusting.

describe('formatExtractionConfidence', () => {
	it('renders a 0..1 confidence as a whole percentage', () => {
		expect(formatExtractionConfidence(0.6)).toBe('60%');
		expect(formatExtractionConfidence(0)).toBe('0%');
		expect(formatExtractionConfidence(1)).toBe('100%');
	});

	it('rounds rather than truncating', () => {
		expect(formatExtractionConfidence(0.955)).toBe('96%');
		expect(formatExtractionConfidence(0.844)).toBe('84%');
	});

	it('clamps an out-of-range figure instead of showing nonsense', () => {
		// A provider reporting 0..100 instead of 0..1 must not render "14000%".
		expect(formatExtractionConfidence(1.4)).toBe('100%');
		expect(formatExtractionConfidence(-0.2)).toBe('0%');
	});

	it('falls back to the placeholder for a missing or non-numeric value', () => {
		expect(formatExtractionConfidence(null)).toBe('—');
		expect(formatExtractionConfidence(undefined)).toBe('—');
		expect(formatExtractionConfidence(Number.NaN)).toBe('—');
		expect(formatExtractionConfidence(Number.POSITIVE_INFINITY)).toBe('—');
		expect(formatExtractionConfidence(null, 'n/a')).toBe('n/a');
	});
});

describe('isMachineRead', () => {
	it('is true only when the run carries an extraction block', () => {
		expect(
			isMachineRead({
				extraction: { method: 'ai_extraction', provider: 'mock', confidence: 0.6, line_count: 3 }
			})
		).toBe(true);
		expect(isMachineRead({ extraction: null })).toBe(false);
	});
});

describe('sourceStatementFilename', () => {
	it('composes from the run metadata, not the storage key', () => {
		expect(
			sourceStatementFilename({
				source_format: 'pdf',
				statement_date: '2026-01-31',
				vendor_name: 'Acme Supplies Ltd'
			})
		).toBe('statement-acme-supplies-ltd-2026-01-31.pdf');
	});

	it('uses a .csv extension for every non-PDF intake', () => {
		expect(
			sourceStatementFilename({
				source_format: 'csv',
				statement_date: '2026-02-28',
				vendor_name: 'Globex'
			})
		).toBe('statement-globex-2026-02-28.csv');
	});

	it('survives a missing or punctuation-only vendor name', () => {
		expect(
			sourceStatementFilename({ source_format: 'csv', statement_date: '2026-03-31', vendor_name: null })
		).toBe('statement-vendor-2026-03-31.csv');
		expect(
			sourceStatementFilename({ source_format: 'csv', statement_date: '2026-03-31', vendor_name: '&&&' })
		).toBe('statement-vendor-2026-03-31.csv');
	});
});

describe('RECON_SOURCE_FORMAT_LABEL_KEYS', () => {
	it('covers every source format the backend can stamp', () => {
		// `SOURCE_MANUAL` / `SOURCE_CSV` / `SOURCE_PDF` in
		// backend/app/models/vendor_statement_recon.py. An unlabelled format would
		// render a blank pill.
		expect(Object.keys(RECON_SOURCE_FORMAT_LABEL_KEYS).sort()).toEqual(['csv', 'manual', 'pdf']);
	});

	it('names a real catalogue key for every source format', () => {
		// `Record<ReconSourceFormat, MessageKey>` is the compile-time half; a key
		// naming nothing in the catalogue typechecks fine and renders the raw key
		// string in the provenance pill, so check the catalogue too.
		for (const key of Object.values(RECON_SOURCE_FORMAT_LABEL_KEYS)) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
		}
		expect(reconSourceFormatLabelKey('pdf')).toBe('vendorStatements.sourceFormat.pdf');
		expect(reconSourceFormatLabelKey('edi')).toBeNull();
	});
});

describe('RECON_STATUS_LABEL_KEYS', () => {
	it('names a real catalogue key for every run status', () => {
		for (const status of RECON_STATUSES) {
			const key = RECON_STATUS_LABEL_KEYS[status];
			expect(key, `${status} has no label key`).toBeTruthy();
			expect(Object.keys(en), `${status} → "${key}" is not in the catalogue`).toContain(key);
			expect(en[key]).not.toBe(status);
		}
	});

	it('resolves a known status and returns null otherwise', () => {
		for (const status of RECON_STATUSES) {
			expect(reconStatusLabelKey(status)).toBe(RECON_STATUS_LABEL_KEYS[status]);
		}
		expect(reconStatusLabelKey('archived')).toBeNull();
	});
});

describe('the per-line label maps', () => {
	it('name real catalogue keys for every classification and resolution', () => {
		// Both render as cells of the modal's diff table, whose every header and
		// action is translated — an English literal there was the hybrid this
		// conversion removes.
		for (const key of [
			...Object.values(RECON_CLASSIFICATION_LABEL_KEYS),
			...Object.values(RECON_RESOLUTION_LABEL_KEYS)
		]) {
			expect(Object.keys(en), `"${key}" is not in the catalogue`).toContain(key);
		}
	});
});

describe('ambiguousSkipCount', () => {
	const meta = (skipped?: unknown) =>
		({ method: 'ai_extraction', provider: 'mock', confidence: 0.6, line_count: 2, ...(skipped === undefined ? {} : { skipped_ambiguous: skipped }) }) as never;

	it("reports the reader's refused-row count", () => {
		expect(ambiguousSkipCount(meta(3))).toBe(3);
	});

	it('reports zero when the reader read the document fully', () => {
		// The panel must stay silent on a clean statement — a standing
		// "0 rows skipped" line is the noise the split classification avoids.
		expect(ambiguousSkipCount(meta(0))).toBe(0);
	});

	it('reports zero for a run that predates the count', () => {
		// Persisted `meta.extraction` from before the reader classified its
		// skips carries no key; that is "unknown", not "rows were dropped".
		expect(ambiguousSkipCount(meta())).toBe(0);
		expect(ambiguousSkipCount(null)).toBe(0);
		expect(ambiguousSkipCount(undefined)).toBe(0);
	});

	it('never emits a nonsense figure from a bad payload', () => {
		expect(ambiguousSkipCount(meta(Number.NaN))).toBe(0);
		expect(ambiguousSkipCount(meta(Number.POSITIVE_INFINITY))).toBe(0);
		expect(ambiguousSkipCount(meta(-4))).toBe(0);
		expect(ambiguousSkipCount(meta('3'))).toBe(0);
		expect(ambiguousSkipCount(meta(2.6))).toBe(3);
	});
});
