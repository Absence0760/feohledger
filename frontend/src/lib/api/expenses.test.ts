import { beforeEach, describe, expect, it, vi } from 'vitest';
import { messages as de } from '#lib/i18n/locales/de.ts';
import { interpolate } from '#lib/i18n/interpolate.ts';

// `submitReport` hand-rolls its fetch (to keep the 422 violation list intact),
// so it does not pass through `api.ts`'s error path on its own. These pin that
// a coded refusal on that path is still stated in the reader's language
// (decisions §361): the not-draft 422 of a report submitted from another tab,
// and the owner 403 — with the English `message` only for a code this build
// cannot state. The REAL `api.ts` is driven with its SvelteKit-only seams
// replaced (the `cardGenerate.test.ts` arrangement), with German as the
// reader's language.
vi.mock('#lib/tenant.ts', () => ({
	getApiBase: () => 'http://api.test',
	getTenantSlug: () => 'acme'
}));
vi.mock('#lib/entity.ts', () => ({ getSelectedEntityId: () => null }));
vi.mock('#lib/i18n/store.svelte.ts', async () => {
	const { messages } = await import('#lib/i18n/locales/de.ts');
	const { interpolate: fill } = await import('#lib/i18n/interpolate.ts');
	return {
		m: (key: string, params?: Record<string, string | number>) =>
			(messages as Record<string, string>)[key] === undefined
				? key
				: fill((messages as Record<string, string>)[key], params, 'de')
	};
});

const { submitReport } = await import('#lib/api/expenses.ts');

const fetchMock = vi.fn();

function respond(body: unknown, status: number) {
	fetchMock.mockResolvedValueOnce(
		new Response(JSON.stringify(body), {
			status,
			headers: { 'Content-Type': 'application/json' }
		})
	);
}

beforeEach(() => {
	fetchMock.mockReset();
	vi.stubGlobal('fetch', fetchMock);
	vi.stubGlobal('window', {});
	vi.stubGlobal('localStorage', {
		getItem: () => 'jwt-1',
		setItem: () => {},
		removeItem: () => {}
	});
});

describe('submitReport states a coded refusal in the reader’s language', () => {
	it('localizes the not-draft 422 instead of returning the server English', async () => {
		respond(
			{
				detail: {
					code: 'expense_report_not_draft',
					message: "Cannot submit a report in 'submitted' state",
					params: { status: 'submitted' }
				}
			},
			422
		);
		const result = await submitReport('r1');
		expect(result.ok).toBe(false);
		if (result.ok) return;
		expect(result.violations).toEqual([]);
		expect(result.message).toBe(
			interpolate(de['refusal.expenseReportNotDraft'], { status: de['expenses.reports.status.submitted'] }, 'de')
		);
	});

	it('keeps the server English for a code this build cannot state', async () => {
		respond(
			{ detail: { code: 'from_a_newer_backend', message: 'server English', params: {} } },
			422
		);
		const result = await submitReport('r1');
		expect(result.ok).toBe(false);
		if (!result.ok) expect(result.message).toBe('server English');
	});

	it('localizes the coded policy-blocked 422 and keeps its coded violation list (§378)', async () => {
		const violations = [
			{
				code: 'receipt_required',
				message: 'A receipt is required for amounts above 5.00 EUR.',
				params: {
					amount: '50.00',
					amount_currency: 'EUR',
					limit: '5.00',
					currency: 'EUR',
					comparison: 'resolved'
				},
				expense_id: 'e1'
			}
		];
		respond(
			{
				detail: {
					code: 'expense_report_policy_blocked',
					message: 'Report has blocking policy violations and cannot be submitted.',
					params: { count: 1 },
					violations
				}
			},
			422
		);
		const result = await submitReport('r1');
		expect(result.ok).toBe(false);
		if (result.ok) return;
		expect(result.violations).toEqual(violations);
		expect(result.message).toBe(interpolate(de['refusal.expenseReportPolicyBlocked'], { count: 1 }, 'de'));
		expect(result.message).not.toContain('blocking policy violations');
	});

	it('localizes the coded no-locked-rate 422', async () => {
		respond(
			{
				detail: {
					code: 'expense_report_lines_unconverted',
					message:
						'2 expense(s) have no exchange rate locked into the report currency USD and cannot be totalled. Re-attach them to lock a rate.',
					params: { count: 2, currency: 'USD' },
					expense_ids: ['e1', 'e2']
				}
			},
			422
		);
		const result = await submitReport('r1');
		expect(result.ok).toBe(false);
		if (result.ok) return;
		expect(result.violations).toEqual([]);
		expect(result.message).toBe(
			interpolate(de['refusal.expenseReportLinesUnconverted'], { count: 2, currency: 'USD' }, 'de')
		);
	});

	it('still returns an uncoded (older backend) policy-violation list intact', async () => {
		const violations = [{ code: 'receipt_required', message: 'Receipt required' }];
		respond({ detail: { message: 'Report has blocking policy violations', violations } }, 422);
		const result = await submitReport('r1');
		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.violations).toEqual(violations);
			expect(result.message).toBe('Report has blocking policy violations');
		}
	});

	it('throws the owner 403 localized, carrying its code', async () => {
		respond(
			{
				detail: {
					code: 'expense_report_not_owner',
					message: "Only the report's owner can change its lines or submit it.",
					params: {}
				}
			},
			403
		);
		await expect(submitReport('r1')).rejects.toMatchObject({
			status: 403,
			code: 'expense_report_not_owner',
			message: de['refusal.expenseReportNotOwner']
		});
	});
});
