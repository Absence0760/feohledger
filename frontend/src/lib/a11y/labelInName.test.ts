import { describe, test, expect } from 'vitest';
import { SUPPORTED_LOCALES } from '#lib/i18n/locale.ts';
import { loadAllMessages } from '#lib/i18n/allMessages.ts';

// WCAG 2.5.3 Label in Name: when a control's accessible name is an
// aria-label, that name must contain the words the control SHOWS, or a
// speech-input user who says what they see ("click Resolve") gets nothing.
//
// The pairing convention the catalogues already follow: an aria-label for a
// control whose visible text is message `<key>` is message `<key>Aria`
// (`supplierChat.resolve` → `supplierChat.resolveAria`). For every such pair,
// in every shipped locale, the Aria text must contain the visible text,
// compared case-insensitively, with `{placeholders}` and a trailing ellipsis
// or colon set aside. An Aria text that interpolates `{action}` is handed
// the visible label at the call site, so `{action}` counts as containing it.
//
// Translators reword freely, which is how this broke: the English
// "Resolve chat thread" became "Chat-Thread als gelöst markieren" in German,
// a fine sentence that no longer says "Lösen".

/** `<key>Aria` pairs that are NOT a visible-label / aria-label pair: the
 *  Aria key names a different control than the one showing `<key>`. */
const NOT_A_PAIR: Record<string, string> = {
	'expenses.reports.rejectAria': 'labels the rejection-reason textarea, not the Reject button',
	'expenses.reports.attachAria': 'labels the expense-id input, not the Attach button',
	'invoices.modal.review.rejectAria': 'labels the rejection-reason textarea, not the Reject button',
	'scheduledReports.modal.createAria': 'names the create dialog, not the Create button',
	'workflows.mgmt.ver.fromAria': 'labels the "from version" select beside a "From" caption',
	'workflows.mgmt.ver.toAria': 'labels the "to version" select beside a "To" caption',
	'assistant.thinkingAria': 'names the typing indicator image, which shows no text',
};

function visibleCore(text: string): string {
	return text
		.replace(/\{[^}]*\}/g, '')
		.trim()
		.replace(/[….:]+$/u, '')
		.trim()
		.toLowerCase();
}

function containsVisible(aria: string, visible: string): boolean {
	if (aria.includes('{action}')) return true;
	return aria.toLowerCase().includes(visibleCore(visible));
}

describe('WCAG 2.5.3 Label in Name: every <key>Aria contains <key>', () => {
	test('the exemption list names keys that exist', async () => {
		const en = await loadAllMessages('en');
		for (const key of Object.keys(NOT_A_PAIR)) {
			expect(key in en, `${key} is not a message key`).toBe(true);
			expect(key.slice(0, -4) in en, `${key} has no visible sibling`).toBe(true);
		}
	});

	test('the supplier chat status buttons are covered (not exempted)', async () => {
		const en = await loadAllMessages('en');
		for (const key of ['supplierChat.resolveAria', 'supplierChat.reopenAria']) {
			expect(key in en, key).toBe(true);
			expect(key in NOT_A_PAIR, key).toBe(false);
		}
	});

	for (const locale of SUPPORTED_LOCALES) {
		test(`${locale}: each aria-label contains its visible label`, async () => {
			const dict = await loadAllMessages(locale);
			const failures: string[] = [];
			for (const [key, aria] of Object.entries(dict)) {
				if (!key.endsWith('Aria') || key in NOT_A_PAIR) continue;
				const visible = (dict as Record<string, string>)[key.slice(0, -4)];
				if (visible === undefined) continue;
				if (!containsVisible(aria, visible)) {
					failures.push(`${key}: "${aria}" does not contain "${visible}"`);
				}
			}
			expect(failures).toEqual([]);
		});
	}
});
