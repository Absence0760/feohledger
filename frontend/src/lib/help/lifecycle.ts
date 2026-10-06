// The stages of the invoice-lifecycle walkthrough (InvoiceLifecycle.svelte),
// on the help centre's landing page and in the "life of an invoice" guide.
//
// Each stage names the real workflow statuses it covers by their catalogue
// keys, so the walkthrough's badges read exactly what /invoices shows, in the
// reader's language. The order and the side branches follow the authoritative
// graph (backend/app/services/workflow_engine.py::VALID_TRANSITIONS, mirrored in
// #lib/types/invoice.ts); lib/help/content.test.ts pins every status to exactly
// one stage, so a new status can't go missing from help.
//
// `text` and `who` are help prose (English, inline markup — see ./types.ts).

import type { InvoiceStatus } from '#lib/types/invoice.ts';

export interface LifecycleStage {
	id: string;
	/** The statuses this stage covers; its name is the first one's label. */
	statuses: InvoiceStatus[];
	/** Statuses an invoice can leave this stage for, off the main path. */
	branches?: InvoiceStatus[];
	text: string;
	who: string;
	/** The guide that covers this stage. */
	guide: string;
}

export const LIFECYCLE: LifecycleStage[] = [
	{
		id: 'received',
		statuses: ['new'],
		text: 'An invoice arrives: uploaded by your team, forwarded to your intake email address, imported from a CSV file, received as a structured [[e-invoice]], or submitted by the vendor through the [[supplier-portal]]. Nothing has been checked yet.',
		who: 'Anyone who captures invoices; some channels need nobody at all.',
		guide: 'capture-invoices'
	},
	{
		id: 'extracting',
		statuses: ['pending'],
		branches: ['failed'],
		text: 'FeohLedger reads the document and fills in the vendor, amounts, dates and line items, with a [[confidence-score]] for each field. If extraction fails, the invoice shows {ui:invoices.status.failed} and can be retried.',
		who: 'Automatic.',
		guide: 'capture-invoices'
	},
	{
		id: 'review',
		statuses: ['ready_for_review'],
		branches: ['rejected'],
		text: 'A person checks what was extracted, fixes anything wrong, codes it to the right GL accounts and looks at the warnings: a possible [[duplicate-invoice]], a [[three-way-match]] that doesn’t agree, line items that don’t add up. A rejected invoice goes back to be corrected and comes round again.',
		who: 'AP clerks and AP managers.',
		guide: 'review-invoice'
	},
	{
		id: 'approved',
		statuses: ['approved'],
		text: 'The [[approval-chain]] signs it off. Larger invoices can need more levels, or the CFO, depending on your organization’s [[approval-threshold|thresholds]]. Nobody can approve an invoice they uploaded or shaped themselves: that is [[segregation-of-duties]].',
		who: 'The approvers your workflow names.',
		guide: 'approve-invoices'
	},
	{
		id: 'erp',
		statuses: ['sending_to_erp', 'sent_to_erp', 'posted_in_erp'],
		branches: ['failed'],
		text: 'If your organization has an ERP connected, the approved invoice is posted to it, and FeohLedger follows it until the ERP confirms. Without an ERP, an approved invoice goes straight on to payment.',
		who: 'Automatic.',
		guide: 'connect-erp'
	},
	{
		id: 'scheduled',
		statuses: ['payment_scheduled'],
		text: 'The invoice is in a [[payment-run]]. The run is approved (by the CFO, above your threshold), then executed on a [[payment-rail]] such as ACH, wire or virtual card.',
		who: 'AP managers build runs; approvers and the CFO sign them off.',
		guide: 'run-payments'
	},
	{
		id: 'paid',
		statuses: ['paid', 'done'],
		text: 'The payment has gone out and the invoice is {ui:invoices.status.paid}, then {ui:invoices.status.done} once nothing is left to do. A payment that has to be reversed is [[void|voided]], which sends the invoice back to {ui:invoices.status.approved} so it can be paid again correctly.',
		who: 'Automatic, from the payment provider’s confirmation.',
		guide: 'how-payments-work'
	}
];
