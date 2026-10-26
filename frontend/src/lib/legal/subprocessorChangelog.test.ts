import { describe, expect, it } from 'vitest';

import { CONTACT } from './operator';
import {
	NOTICE_OBJECTION_CONTACT,
	NOTICE_PERIOD_DAYS,
	SUBPROCESSOR_CHANGES
} from './subprocessorChangelog.generated';

/**
 * The published § 10 change log is generated from the backend's change log
 * (`backend/app/services/subprocessor_changelog.py`, decisions §321), and the
 * notice email the backend sends names the objection address it was generated
 * with. These hold the two halves of that one promise together on this side.
 */
describe('sub-processor change log', () => {
	it('names the same objection address as the published pages', () => {
		// The email says "write to X"; the DPA says "write to CONTACT.privacy".
		// A notice pointing at a different mailbox than the contract would send
		// an objection somewhere nobody committed to read.
		expect(NOTICE_OBJECTION_CONTACT).toBe(CONTACT.privacy);
	});

	it('carries the 30 days the DPA promises', () => {
		expect(NOTICE_PERIOD_DAYS).toBe(30);
	});

	it('is oldest first with unique ids', () => {
		const ids = SUBPROCESSOR_CHANGES.map((row) => row.id);
		expect(new Set(ids).size).toBe(ids.length);
		const dates = SUBPROCESSOR_CHANGES.map((row) => row.publishedOn);
		expect([...dates].sort()).toEqual(dates);
	});

	it('gives every notice row an effective date, and no other row one', () => {
		for (const row of SUBPROCESSOR_CHANGES) {
			expect(row.effectiveLabel !== null).toBe(row.notice);
		}
	});
});
