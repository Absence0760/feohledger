// GENERATED FILE — do not edit by hand.
//
// Source of truth: backend/app/services/subprocessor_changelog.py
// Regenerate:      pnpm gen:subprocessor-changelog
// Drift check:     pnpm check:subprocessor-changelog  (runs in CI)
//
// /legal/sub-processors § 10 renders these rows. The same entries drive
// the notice emailed to every customer organisation's administrators
// (backend/app/services/subprocessor_notices.py, docs/decisions.md §321),
// so a row here and the notice a customer received cannot disagree.
// Add a row in the Python module, never here.

/** The advance notice the DPA promises, in days. */
export const NOTICE_PERIOD_DAYS = 30;

/** The objection address the notice email names. Must equal
 *  `CONTACT.privacy` (operator.ts) — a vitest holds the two together. */
export const NOTICE_OBJECTION_CONTACT = 'privacy@feohledger.com';

export interface SubprocessorChange {
	/** Stable entry id — the key the notice ledger is written under. */
	readonly id: string;
	/** ISO date the row was published. */
	readonly publishedOn: string;
	/** `publishedOn` as the page prints it (e.g. 15 September 2026). */
	readonly publishedLabel: string;
	readonly change: string;
	/** Whether the change needed, and was given, the advance notice. */
	readonly notice: boolean;
	/** When a notice change takes effect, as the page prints it; null otherwise. */
	readonly effectiveLabel: string | null;
	/** Why a notice was shorter than NOTICE_PERIOD_DAYS, if it was. */
	readonly urgentReason: string | null;
}

/** Oldest first, exactly as the backend change log lists them. */
export const SUBPROCESSOR_CHANGES = [
	{
		id: '2026-09-15-initial-publication',
		publishedOn: '2026-09-15',
		publishedLabel: '15 September 2026',
		change: 'Initial publication of the sub-processor register.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
	{
		id: '2026-10-07-rds',
		publishedOn: '2026-10-07',
		publishedLabel: '7 October 2026',
		change: 'The databases move from the EC2 instance\'s own disk to Amazon RDS, in the same region. RDS is added to § 2; the EC2 row no longer holds the databases.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
	{
		id: '2026-10-09-woocommerce',
		publishedOn: '2026-10-09',
		publishedLabel: '9 October 2026',
		change: 'Your own WooCommerce store is added to § 4 as a system you control: a goods receipt can push stock quantities to it. It is not a sub-processor.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
	{
		id: '2026-10-09-square',
		publishedOn: '2026-10-09',
		publishedLabel: '9 October 2026',
		change: 'Square (Block) is added to § 4 as a commerce platform you can connect: a goods receipt adds the received quantity to your own Square account\'s stock.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
	{
		id: '2026-10-10-lightspeed',
		publishedOn: '2026-10-10',
		publishedLabel: '10 October 2026',
		change: 'Lightspeed Retail (X-Series) is added to § 4 as a commerce platform connection: your own store, receiving received-stock quantities against its own product identifiers.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
	{
		id: '2026-10-10-cin7',
		publishedOn: '2026-10-10',
		publishedLabel: '10 October 2026',
		change: 'Cin7 Core (Cin7) is added to § 4 as a commerce platform connection: a goods receipt becomes a stock adjustment in your own Cin7 account, valued at Cin7\'s own average cost.',
		notice: false,
		effectiveLabel: null,
		urgentReason: null,
	},
] as const satisfies readonly SubprocessorChange[];
