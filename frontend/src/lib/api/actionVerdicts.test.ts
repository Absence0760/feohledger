/**
 * The page side of the action verdicts (issue #575): a control is offered from
 * the server's answer alone, an unanswered verdict offers nothing, and a
 * refusal is stated in the reader's language when this build knows its code.
 */
import { describe, expect, it } from 'vitest';
import { en } from '../i18n/locales/en';
import { interpolate } from '../i18n/interpolate';
import type { MessageKey } from '../i18n/messages';
import { ROLE_NOT_PERMITTED, isAllowed, isOffered, refusalText } from './actionVerdicts';

const t = (key: MessageKey, params?: Record<string, string | number>) =>
	interpolate((en as Record<string, string>)[key], params, 'en');

const sod = { code: 'payment_run_segregation', message: 'server English', params: {} };

describe('isAllowed', () => {
	it('is true only for an explicit null — the server said yes', () => {
		expect(isAllowed(null)).toBe(true);
		expect(isAllowed(sod)).toBe(false);
	});

	it('fails closed on a verdict that never arrived', () => {
		expect(isAllowed(undefined)).toBe(false);
	});
});

describe('isOffered', () => {
	it('renders a control the reader may press, or one refused for a reason they can act on', () => {
		expect(isOffered(null)).toBe(true);
		expect(isOffered(sod)).toBe(true);
	});

	it('hides a control the reader’s role can never use, and one with no verdict yet', () => {
		expect(isOffered({ code: ROLE_NOT_PERMITTED, message: 'x' })).toBe(false);
		expect(isOffered(undefined)).toBe(false);
	});
});

describe('refusalText', () => {
	it('localizes a code this build knows', () => {
		expect(refusalText(sod, t)).toBe(en['refusal.paymentRunSegregation']);
		expect(
			refusalText({ code: 'vendor_change_self_provisioned', message: 'x', params: {} }, t)
		).toBe(en['refusal.vendorChangeSelfProvisioned']);
		expect(refusalText({ code: 'invoice_not_in_review', message: 'x', params: {} }, t)).toBe(
			en['refusal.invoiceNotInReview']
		);
	});

	it('falls back to the server sentence for a code it predates', () => {
		expect(refusalText({ code: 'some_future_code', message: 'Server says no.' }, t)).toBe(
			'Server says no.'
		);
	});

	it('is empty for no refusal', () => {
		expect(refusalText(null, t)).toBe('');
	});
});
