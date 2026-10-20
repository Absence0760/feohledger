/**
 * What an ERP chart / vendor sync tells the person who ran it.
 *
 * `POST /api/gl-accounts/sync-erp` and `POST /api/vendors/sync-erp` return
 * counts plus what they REFUSED to link (`backend/app/services/
 * gl_account_sync.py`, `vendor_sync.py`, docs/decisions.md §284): an ERP code
 * two accounts share, or a vendor name that matches more than one vendor, is
 * never linked to either, and a chart row whose code stopped identifying one
 * ERP account loses its link; a record retired because the ERP made it
 * inactive, or synced with a value too long to store, is reported too. Those
 * are the lines an operator has to act on —
 * recode the invoices, link the vendor by hand — so they are stated as
 * warnings, apart from the success line, rather than folded into the server's
 * English summary.
 *
 * Pure (the caller passes `m`) so vitest's node config reaches it.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';
import { formatList } from '#lib/utils/list.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

export interface SyncReportLine {
	text: string;
	tone: 'success' | 'warning';
}

/** The GL sync response's fields this reads. */
export interface GlSyncReport {
	created: number;
	updated: number;
	conflicts?: ReadonlyArray<{ code: string }>;
	unlinked?: ReadonlyArray<string>;
	deactivated?: ReadonlyArray<string>;
	/** Codes synced without their parent, whose code did not fit the column. */
	parent_code_dropped?: ReadonlyArray<string>;
}

/** The vendor sync response's fields this reads. */
export interface VendorSyncReport {
	created: number;
	updated: number;
	unchanged: number;
	conflicts?: ReadonlyArray<{ name: string }>;
	/** Vendors whose link this pull could not confirm, so dropped. */
	unlinked?: ReadonlyArray<string>;
	/** How many vendors were retired because the ERP made them inactive. */
	deactivated?: number;
	/** Vendors synced with a field too long to store left at its old value. */
	values_too_long?: ReadonlyArray<{ name: string }>;
}

export function glSyncReportLines(result: GlSyncReport, t: Translate): SyncReportLine[] {
	const lines: SyncReportLine[] = [
		{
			text: t('glAccounts.toast.synced', { created: result.created, updated: result.updated }),
			tone: 'success'
		}
	];
	const conflicts = (result.conflicts ?? []).map((c) => c.code);
	if (conflicts.length) {
		lines.push({
			text: t('glAccounts.toast.syncConflicts', { codes: formatList(conflicts) }),
			tone: 'warning'
		});
	}
	const unlinked = result.unlinked ?? [];
	if (unlinked.length) {
		lines.push({
			text: t('glAccounts.toast.syncUnlinked', { codes: formatList([...unlinked]) }),
			tone: 'warning'
		});
	}
	const deactivated = result.deactivated ?? [];
	if (deactivated.length) {
		lines.push({
			text: t('glAccounts.toast.syncDeactivated', { codes: formatList([...deactivated]) }),
			tone: 'warning'
		});
	}
	const parentDropped = result.parent_code_dropped ?? [];
	if (parentDropped.length) {
		lines.push({
			text: t('glAccounts.toast.syncParentDropped', { codes: formatList([...parentDropped]) }),
			tone: 'warning'
		});
	}
	return lines;
}

export function vendorSyncReportLines(result: VendorSyncReport, t: Translate): SyncReportLine[] {
	const lines: SyncReportLine[] = [
		{
			text: t('vendors.toast.synced', {
				created: result.created,
				updated: result.updated,
				unchanged: result.unchanged
			}),
			tone: 'success'
		}
	];
	const names = (result.conflicts ?? []).map((c) => c.name);
	if (names.length) {
		lines.push({
			text: t('vendors.toast.syncConflicts', { names: formatList(names) }),
			tone: 'warning'
		});
	}
	const unlinked = result.unlinked ?? [];
	if (unlinked.length) {
		lines.push({
			text: t('vendors.toast.syncUnlinked', { names: formatList([...unlinked]) }),
			tone: 'warning'
		});
	}
	if (result.deactivated) {
		lines.push({
			text: t('vendors.toast.syncDeactivated', { n: result.deactivated }),
			tone: 'warning'
		});
	}
	const tooLong = (result.values_too_long ?? []).map((v) => v.name);
	if (tooLong.length) {
		lines.push({
			text: t('vendors.toast.syncValuesTooLong', { names: formatList(tooLong) }),
			tone: 'warning'
		});
	}
	return lines;
}
