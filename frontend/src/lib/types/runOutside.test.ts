import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
	BANK_FILE_DOWNLOAD_HIDDEN,
	BANK_FILE_REGENERATE_HIDDEN,
	BANK_FILE_REJECT_HIDDEN,
	RUN_RECORD_HIDDEN,
	isRunControlOffered
} from './runOutside';

/**
 * The run dialog's record / bank-file / bank-rejected controls read the
 * server's verdicts (`actions.record_outside`, `nacha_export`,
 * `nacha_regenerate`, `nacha_void`). This module only decides which refusals
 * HIDE a control rather than disable it.
 */

const CONTROLS_PY = readFileSync(
	join(__dirname, '../../../../backend/app/services/payment_controls.py'),
	'utf8'
);

const HIDDEN_SETS: [string, readonly string[]][] = [
	['download', BANK_FILE_DOWNLOAD_HIDDEN],
	['regenerate', BANK_FILE_REGENERATE_HIDDEN],
	['reject', BANK_FILE_REJECT_HIDDEN],
	['record', RUN_RECORD_HIDDEN]
];

describe('isRunControlOffered', () => {
	it.each(HIDDEN_SETS)('%s fails closed until the verdict arrives', (_name, hidden) => {
		expect(isRunControlOffered(undefined, hidden)).toBe(false);
	});

	it.each(HIDDEN_SETS)('%s is offered when the endpoint would accept', (_name, hidden) => {
		expect(isRunControlOffered(null, hidden)).toBe(true);
	});

	it.each(HIDDEN_SETS)('%s is hidden for a refusal about the reader’s role', (_name, hidden) => {
		expect(isRunControlOffered({ code: 'role_not_permitted', message: 'x' }, hidden)).toBe(false);
	});

	it.each(HIDDEN_SETS)('%s is hidden on its wrong-state refusals', (_name, hidden) => {
		for (const code of hidden) {
			expect(isRunControlOffered({ code, message: 'x' }, hidden)).toBe(false);
		}
	});

	it.each([
		'payment_run_segregation',
		'payment_run_awaiting_cfo',
		'nacha_not_configured',
		'nacha_nothing_to_export',
		'payment_run_nothing_to_record'
	])('is shown (disabled, with the reason) for %s — the reader can act on it', (code) => {
		for (const [, hidden] of HIDDEN_SETS) {
			expect(isRunControlOffered({ code, message: 'x' }, hidden)).toBe(true);
		}
	});

	it('the first-file download is hidden once the run’s file is out', () => {
		expect(
			isRunControlOffered({ code: 'nacha_already_exported', message: 'x' }, BANK_FILE_DOWNLOAD_HIDDEN)
		).toBe(false);
	});

	it.each(HIDDEN_SETS)('%s names only codes the backend gates actually emit', (_name, hidden) => {
		for (const code of hidden) expect(CONTROLS_PY).toContain(`"${code}"`);
	});
});
