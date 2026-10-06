// The glossary's long-form entries. Each term's NAME and one-line definition
// are not here: they are ordinary UI copy in the message catalogues, as
// `help.term.<id>` and `help.term.<id>.short`, in all six locales — see the
// split described at the top of ./types.ts. What lives here is the English
// explanation the glossary page shows, plus aliases (for search) and related
// entries. Order is the order the glossary page lists each category's terms.

import type { GlossaryEntry } from './types.ts';

export const GLOSSARY: GlossaryEntry[] = [
	// -------------------------------------------------------------------------
	// Invoices
	// -------------------------------------------------------------------------
	{
		id: 'invoice',
		category: 'invoices',
		long: "A supplier's request for payment for goods or services. In FeohLedger an invoice is the record everything else hangs off: its vendor, amount, currency, due date, [[line-item|line items]] and [[gl-coding|GL coding]], plus the approvals, matches, exceptions and payments that follow it.\n\nEvery invoice moves through a fixed lifecycle, from {ui:invoices.status.new} through {ui:invoices.status.readyForReview} and {ui:invoices.status.approved} to {ui:invoices.status.paid} and {ui:invoices.status.done}. Each status change is written to the [[audit-trail]]. The [[guide:invoice-lifecycle|invoice lifecycle guide]] walks through every step.\n\nInvoices arrive by upload, [[email-intake]], [[peppol]], the [[supplier-portal]], CSV import, or a [[recurring-invoice]] template.",
		aliases: ['bill', 'payable', 'AP invoice', 'supplier invoice', 'vendor bill'],
		related: ['extraction', 'workflow', 'payment-run', 'line-item']
	},
	{
		id: 'extraction',
		category: 'invoices',
		long: 'Reading the data off an invoice document automatically. When you upload a PDF or image, FeohLedger uses AI and OCR to pull out the vendor, invoice number, dates, amounts, currency and line items, while the invoice shows {ui:invoices.status.pending}. It then matches the vendor to your vendor list and suggests [[gl-coding|GL codes]].\n\nEvery extracted field carries a [[confidence-score]], so you can see which values to check before you approve. A structured [[e-invoice]] skips the AI entirely: its data is read exactly as the supplier sent it.\n\nIf extraction fails, the invoice goes to {ui:invoices.status.failed} and an {ui:exceptions.type.extractionFailed} exception opens, so nothing drops out of the queue unnoticed.',
		aliases: ['OCR', 'data capture', 'invoice scanning', 'AI extraction', 'IDP'],
		related: ['confidence-score', 'e-invoice', 'email-intake', 'gl-coding']
	},
	{
		id: 'confidence-score',
		category: 'invoices',
		long: "How sure the [[extraction]] is about each value it read, from 0 to 1. A high score is applied as-is; a middling score is applied but flagged for you to check; a low score is only offered as a suggestion. AI [[gl-coding|GL codes]] are only filled in automatically when the AI is reasonably confident.\n\nIf your organization's workflow allows it, an invoice whose overall confidence clears a set bar can be approved without a human review. That shortcut never overrides a money control: an invoice that would need the [[cfo-gate|CFO's sign-off]], or is over the maximum amount, still goes to a person.",
		aliases: ['extraction confidence', 'accuracy score'],
		related: ['extraction', 'approval-threshold', 'workflow']
	},
	{
		id: 'line-item',
		category: 'invoices',
		long: "One row on an invoice: a description, quantity, unit price and amount, often with its own [[gl-coding|GL account]]. FeohLedger extracts line items from the document and uses their quantities in [[three-way-match|three-way matching]] against a [[goods-receipt]].\n\nThe invoice total is never recalculated from the lines. Instead, FeohLedger checks that the lines add up to the total, and raises a {ui:exceptions.type.lineTotalMismatch} exception when they don't. That exception blocks payment until someone resolves it, because a gap between lines and total is how padded invoices slip through.",
		aliases: ['invoice line', 'line', 'invoice detail'],
		related: ['invoice', 'gl-coding', 'three-way-match', 'payment-blocking-exception', 'line-total-mismatch']
	},
	{
		id: 'gl-coding',
		category: 'invoices',
		long: 'Assigning each invoice, or each of its [[line-item|line items]], to a general-ledger account so the spend is booked to the right place in your accounts. FeohLedger suggests GL codes during [[extraction]] and fills them in automatically only when it is reasonably confident. You can correct the coding while you review, or re-code invoices in bulk.\n\nThe accounts come from your {ui:nav.glAccounts}, which is usually synced from your ERP. If your organization has several [[entity|entities]], each one sees the shared accounts plus its own.',
		aliases: ['GL code', 'account coding', 'general ledger account', 'cost coding', 'nominal code'],
		related: ['line-item', 'erp-sync', 'entity', 'extraction']
	},
	{
		id: 'e-invoice',
		category: 'invoices',
		long: 'An invoice sent as structured data rather than as a picture of a page. FeohLedger recognizes UBL 2.1 (the PEPPOL format), UN/CEFACT CII, and the hybrid Factur-X and ZUGFeRD PDFs that carry the data inside them. These are read exactly as written, with no AI involved, so every field arrives at full confidence.\n\nAn ordinary scanned PDF goes through normal [[extraction]] instead. FeohLedger can also produce an invoice as UBL and in several national formats.',
		aliases: ['electronic invoice', 'structured invoice', 'UBL', 'CII', 'Factur-X', 'ZUGFeRD', 'XML invoice'],
		related: ['peppol', 'extraction', 'email-intake']
	},
	{
		id: 'peppol',
		category: 'invoices',
		long: 'An international network for exchanging [[e-invoice|e-invoices]] between businesses, used widely in Europe and beyond and required for public-sector invoicing in many countries. Each business is reached through an access point, much like email through a mail server.\n\nIf your organization is connected to PEPPOL, invoices that suppliers send over the network land in FeohLedger as invoices automatically, already structured. FeohLedger can also send an approved invoice over PEPPOL.',
		aliases: ['Peppol', 'PEPPOL BIS', 'PEPPOL network', 'access point'],
		related: ['e-invoice', 'email-intake', 'invoice']
	},
	{
		id: 'email-intake',
		category: 'invoices',
		long: "Your organization's own email address for invoices. Suppliers email their invoices to it, and each PDF, image or e-invoice attachment becomes an invoice in your queue, with [[extraction]] started automatically. Nobody needs to download and upload anything.\n\nAn admin finds or changes the address in the organization settings. Invoices that arrive this way have no employee as their uploader, so anyone with approval rights may approve them.",
		aliases: ['email to invoice', 'AP inbox', 'invoice mailbox', 'inbound email'],
		related: ['extraction', 'e-invoice', 'invoice']
	},
	{
		id: 'recurring-invoice',
		category: 'invoices',
		long: 'A template for a bill that comes on a fixed schedule, such as rent, insurance or a software subscription. You set the vendor, amount, GL coding and cadence (monthly, quarterly or annually), and FeohLedger creates each invoice on schedule, already coded, so it goes straight to approval with no upload.\n\nThe person who set up the template, and anyone who later changed its vendor, amount, currency, coding or schedule, cannot approve the invoices it generates. That keeps a template from becoming a way to approve your own spend. See [[segregation-of-duties]].',
		aliases: ['recurring bill', 'subscription invoice', 'scheduled invoice', 'standing invoice'],
		related: ['invoice', 'segregation-of-duties', 'gl-coding']
	},
	{
		id: 'credit-memo',
		category: 'invoices',
		long: "A document from a supplier that reduces what you owe them, for example after a return, a pricing error or a rebate. In FeohLedger you record a credit memo and apply it to one of that supplier's open invoices, and the credit is netted off when that invoice is paid.\n\nA credit can only be applied to an invoice from the same vendor, in the same [[entity]] and the same currency, and never to one that is already {ui:invoices.status.paid} or {ui:invoices.status.done}. A credit memo can be edited only while it is open and has never been applied, and can be voided.",
		aliases: ['credit note', 'vendor credit', 'debit memo', 'credit'],
		related: ['invoice', 'payment-run', 'vendor']
	},
	{
		id: 'duplicate-invoice',
		category: 'invoices',
		long: 'The same bill entered twice, whether by accident or because a supplier resent it. Paying it twice is one of the most common losses in accounts payable. FeohLedger flags an invoice whose number already exists for that vendor, and also one that looks very similar to an existing invoice, showing how close the match is.\n\nA suspected duplicate opens a {ui:exceptions.type.duplicate} exception. It is a [[payment-blocking-exception]]: the invoice cannot be paid until someone resolves or dismisses the flag.',
		aliases: ['duplicate', 'double payment', 'duplicate detection', 'dupe'],
		related: ['exception', 'payment-blocking-exception', 'invoice']
	},
	{
		id: 'line-total-mismatch',
		category: 'invoices',
		long: "An invoice whose [[line-item|line items]] don't add up to its total. FeohLedger checks whenever the lines or the header change. The lines reconcile if they match, to within a cent, the invoice total, its subtotal, or the total less tax and shipping plus any discount. Anything else opens a {ui:exceptions.type.lineTotalMismatch} exception.\n\nFeohLedger never changes the total to fit the lines: it can't know which of the two is wrong, and a quiet change would move money with no approval behind it. You can still review and approve the invoice, but the exception is a [[payment-blocking-exception]], so the invoice can't be paid until someone resolves or dismisses it.",
		aliases: ['line total mismatch', 'line-item mismatch', 'invoice total mismatch', 'lines do not add up'],
		related: ['line-item', 'payment-blocking-exception', 'exception', 'fraud-flag']
	},
	{
		id: 'invoice-aging',
		category: 'invoices',
		long: "Your approved, unpaid invoices grouped by how far past their due date they are: {ui:dashboard.aging.current} (not yet due), {ui:dashboard.aging.days30}, {ui:dashboard.aging.days60}, {ui:dashboard.aging.days90} and {ui:dashboard.aging.days90plus}. It shows where late payments, and the supplier friction and missed discounts that come with them, are building up.\n\nThe {ui:dashboard.chart.aging} chart on the {ui:nav.dashboard} covers invoices from {ui:invoices.status.approved} until they are paid. An invoice with no due date counts as current, since it can't be shown to be overdue. Amounts are converted to your [[reporting-currency]]; an invoice with no exchange rate is counted at face value and the chart says so. The same bands appear in the aging snapshot export and scheduled report.",
		aliases: ['AP aging', 'aging report', 'aged payables', 'aged creditors', 'overdue invoices', 'ageing'],
		related: ['days-payable-outstanding', 'invoice', 'reporting-currency', 'touchless-rate']
	},
	{
		id: 'touchless-rate',
		category: 'invoices',
		long: "A measure of how smoothly invoices get through review. {ui:dashboard.kpi.touchlessRate} on the {ui:nav.dashboard} is the share of invoices that cleared review, out of all invoices that finished review: an invoice approved and moving on toward payment counts for it, and a rejected one counts against it.\n\nOnly invoices reviewed in FeohLedger count. Invoices still waiting for review, ones that skipped approval, and history brought in by CSV import are left out, so the figure isn't flattered by records the workflow never handled. A [[workflow-experiment]] readout uses a stricter measure: there, an invoice counts as touchless only if it was approved automatically with no fields changed.",
		aliases: ['STP rate', 'straight-through processing', 'automation rate', 'no-touch rate', 'first-pass rate'],
		related: ['workflow', 'workflow-experiment', 'confidence-score', 'invoice-aging']
	},

	// -------------------------------------------------------------------------
	// Matching
	// -------------------------------------------------------------------------
	{
		id: 'purchase-order',
		category: 'matching',
		long: "Your organization's formal order to a supplier, stating what you are buying, how many and at what price. A PO can come from an approved [[requisition]], from a [[contract]], or be synced from your ERP.\n\nWhen an invoice quotes a PO number, FeohLedger looks that PO up and compares the two, which is [[two-way-match|two-way matching]]. If your organization has several [[entity|entities]], the lookup stays inside the invoice's own entity, so two subsidiaries using the same PO numbers can never match each other's orders.",
		aliases: ['PO', 'order', 'purchase order number'],
		related: ['two-way-match', 'goods-receipt', 'requisition', 'three-way-match']
	},
	{
		id: 'goods-receipt',
		category: 'matching',
		long: 'A record that the goods on a [[purchase-order]] actually arrived, and how many. It is what lets FeohLedger check that you are only paying for what you received.\n\nA PO can have several receipts, for example when an order ships in parts; FeohLedger adds them up, so an order received in two deliveries still matches in full.',
		aliases: ['GR', 'GRN', 'goods received note', 'receiving report', 'receipt'],
		related: ['three-way-match', 'purchase-order', 'quality-inspection']
	},
	{
		id: 'two-way-match',
		category: 'matching',
		long: "Checking an invoice against its [[purchase-order]]: is it from the same vendor, and is the amount within your [[match-tolerance]] of the PO total? This is FeohLedger's standard check for any invoice that quotes a PO number.\n\nThe two must be in the same currency. A EUR invoice is never treated as matching a USD order just because the numbers agree. An invoice outside tolerance, or in a different currency, opens a {ui:exceptions.type.poMismatch} exception for review.",
		aliases: ['2-way match', '2-way', 'PO match'],
		related: ['three-way-match', 'match-tolerance', 'purchase-order']
	},
	{
		id: 'three-way-match',
		category: 'matching',
		long: "A [[two-way-match]] plus a check against the [[goods-receipt]]: did the quantities you are being billed for actually arrive? FeohLedger runs it automatically whenever a receipt exists for the invoice's PO.\n\nIf everything was received the match passes; if only part of the order has arrived, the match shows as partial so you don't pay ahead of delivery.",
		aliases: ['3-way match', '3-way', 'three way matching'],
		related: ['two-way-match', 'four-way-match', 'goods-receipt', 'match-tolerance']
	},
	{
		id: 'four-way-match',
		category: 'matching',
		long: "A [[three-way-match]] plus a [[quality-inspection]]: the goods arrived *and* passed inspection. It is used where receiving the goods isn't enough, such as pharmaceuticals, aerospace parts or food.\n\nA passed inspection leaves the match as it is. A partial acceptance shows the match as partial, and a failed inspection marks it as a mismatch and puts a {ui:exceptions.type.qualityHold} on the invoice. If your organization requires inspection for a vendor or category and none has been recorded, the invoice is flagged too.",
		aliases: ['4-way match', '4-way', 'four way matching'],
		related: ['three-way-match', 'quality-inspection', 'goods-receipt']
	},
	{
		id: 'match-tolerance',
		category: 'matching',
		long: "How far an invoice amount may differ from its [[purchase-order]] and still count as matched, as a percentage. FeohLedger's default is 5%.\n\nAn admin can set a different tolerance for the whole organization, for a particular vendor, or for a particular GL account, so a tightly priced commodity can have a stricter limit than general spend. The most specific rule wins.",
		aliases: ['tolerance', 'variance tolerance', 'price tolerance', 'match threshold'],
		related: ['two-way-match', 'three-way-match', 'exception']
	},
	{
		id: 'quality-inspection',
		category: 'matching',
		long: 'A recorded check of received goods with a result of pass, partial acceptance or fail. It is the extra leg in a [[four-way-match]].\n\nInspections can be recorded in FeohLedger or come from a connected quality-management system. Whether an inspection is required can be set per vendor or per GL account.',
		aliases: ['QI', 'inspection', 'quality check', 'QA inspection'],
		related: ['four-way-match', 'goods-receipt', 'exception']
	},

	// -------------------------------------------------------------------------
	// Approvals
	// -------------------------------------------------------------------------
	{
		id: 'workflow',
		category: 'approvals',
		long: 'The route an invoice takes from arrival to done: [[extraction]], approval, export to your ERP, and completion. An admin designs workflows in {ui:nav.workflows}, including who approves (a picked approver, named approvers, an [[approval-chain]], or automatic approval) and the [[approval-threshold|approval thresholds]].\n\nEach invoice keeps a snapshot of the workflow as it was when the invoice arrived. Editing a workflow changes how *new* invoices are handled, never invoices already on their way. With several [[entity|entities]], each can have its own default workflow.',
		aliases: ['approval workflow', 'routing', 'invoice workflow', 'workflow definition'],
		related: ['approval-chain', 'approval-threshold', 'adaptive-workflow', 'workflow-experiment']
	},
	{
		id: 'approval-chain',
		category: 'approvals',
		long: 'A multi-level approval: level one must finish before level two starts, and so on. Each level can cover an amount band, name its approvers, and require more than one approval.\n\nNobody can approve at two levels of the same chain, so a chain always means more than one person. If a level waits too long it can [[escalation|escalate]] to extra approvers. An approver who is away can have a delegate act for them.',
		aliases: ['multi-level approval', 'approval hierarchy', 'approval levels', 'sequential approval'],
		related: ['workflow', 'escalation', 'segregation-of-duties', 'approval-threshold']
	},
	{
		id: 'approval-threshold',
		category: 'approvals',
		long: "An amount in the [[workflow]] that changes how an invoice is approved. There are three: below the auto-approve amount an invoice can skip human review; above the CFO amount only a CFO can approve it (the [[cfo-gate]]); above the maximum it is refused outright. Approval-chain levels use amount bands the same way.\n\nThresholds are in your organization's [[reporting-currency]]. A foreign-currency invoice is converted at the [[fx-rate]] locked on it, so a GBP invoice can't slip under a USD threshold. If the amount can't be converted, FeohLedger errs on the side of more review.",
		aliases: ['approval limit', 'approval matrix', 'authority limit', 'DOA', 'delegation of authority'],
		related: ['cfo-gate', 'approval-chain', 'reporting-currency', 'workflow']
	},
	{
		id: 'cfo-gate',
		category: 'approvals',
		long: "The rule that above a set amount, a CFO has to sign off. FeohLedger applies it in three places: invoice approval above your workflow's CFO threshold, [[payment-run|payment runs]] whose total is above the payment CFO threshold, and expense reports above theirs.\n\nAll three are measured in your [[reporting-currency]]. If a threshold is set to something FeohLedger can't read, the gate stays on rather than switching off, so a typo can never remove the control.",
		aliases: ['CFO approval', 'CFO sign-off', 'executive approval'],
		related: ['approval-threshold', 'payment-run', 'segregation-of-duties']
	},
	{
		id: 'segregation-of-duties',
		category: 'approvals',
		long: "The principle that no one person should control a payment from start to finish. FeohLedger enforces it throughout. Whoever created or materially edited an invoice cannot approve it. Whoever created a [[payment-run]] cannot execute it or give its CFO approval. A vendor's [[bank-change-request|bank change]] needs a second person to approve it. Nobody can clear a [[payment-blocking-exception]] on a payable they were involved in.\n\nWhen FeohLedger refuses you for this reason it is working as designed: ask a colleague to take the next step. An admin can split sensitive duties further with custom [[role|roles]] and [[permission|permissions]]. The [[guide:segregation-of-duties|segregation of duties guide]] explains every rule.",
		aliases: ['SoD', 'separation of duties', 'maker-checker', 'four-eyes principle', 'dual control'],
		related: ['permission', 'approval-chain', 'bank-change-request', 'payment-run']
	},
	{
		id: 'step-up',
		category: 'approvals',
		long: "Confirming it's really you, again, just before a sensitive action, even though you are already signed in. It protects against someone using an unattended or stolen session.\n\nFeohLedger asks for it when you change your own sign-in factors, and before an admin exports a person's unmasked banking details in a [[dsar|data request]]. For that export only your authenticator code or passkey will do, not your password, so you need two-factor authentication set up first.",
		aliases: ['step-up authentication', 're-authentication', 'MFA prompt', 'verify identity'],
		related: ['dsar', 'permission', 'audit-trail', 'mfa']
	},
	{
		id: 'escalation',
		category: 'approvals',
		long: "Passing something to more senior or additional people when it can't or shouldn't be decided where it is. In an [[approval-chain]], a level that waits longer than its set time escalates automatically: the named escalation approvers become able to approve, alongside the original ones, never instead of them.\n\nIn the {ui:nav.exceptions} queue you can escalate an exception yourself, for example when [[segregation-of-duties]] stops you deciding it. An escalated exception that blocks payment still blocks it.",
		aliases: ['escalate', 'approval escalation', 'reminder', 'SLA escalation'],
		related: ['approval-chain', 'exception', 'exception-agent']
	},
	{
		id: 'workflow-experiment',
		category: 'approvals',
		long: "A controlled A/B test of two versions of a [[workflow]]'s rules, such as two different auto-approve amounts, run to see which works better before you adopt it. New invoices on that workflow are split between version A and version B in the proportion you set, and each invoice keeps its version for life.\n\nThe results compare time to approval, [[touchless-rate|touchless rate]], exception rate and rejection rate, and name a winner on the metric you chose once both versions have enough completed invoices. No statistical-significance test is claimed. Once invoices have been assigned, the two versions can't be edited, so the results always describe what actually ran. An experiment changes how invoices are routed, never how they are paid. Admins create and run experiments in {ui:nav.experiments}; AP managers and CFOs can read the results.",
		aliases: ['A/B test', 'experiment', 'split test', 'workflow A/B test'],
		related: ['workflow', 'adaptive-workflow', 'touchless-rate', 'approval-threshold']
	},

	// -------------------------------------------------------------------------
	// Payments
	// -------------------------------------------------------------------------
	{
		id: 'payment-run',
		category: 'payments',
		long: "A batch of approved invoices paid together, such as a weekly pay run. In {ui:nav.payments} you pick invoices from the queue, review the batch, and execute it; each invoice is then paid on its [[payment-rail]].\n\nSeveral controls apply. A run holds one currency. The person who created it can't execute it or give its [[cfo-gate|CFO approval]]. A run above the CFO threshold needs a CFO's sign-off first. An invoice with an open [[payment-blocking-exception]], or a vendor that is blocked or unverified, can't be paid, and each payment is [[sanctions-screening|screened]] before it is sent.",
		aliases: ['pay run', 'payment batch', 'check run', 'payment proposal', 'AP run'],
		related: ['payment-rail', 'settlement', 'cfo-gate', 'segregation-of-duties']
	},
	{
		id: 'payment-rail',
		category: 'payments',
		long: 'The method and network a payment travels on: ACH, wire, check, real-time payments, SEPA, BACS, Faster Payments, CHAPS, international wire or ACH, or a [[virtual-card]]. Which rails you can use depends on the payment provider your organization has connected.\n\nThe rail matters beyond speed. Card payments are left out of [[form-1099|1099]] totals because the card network reports them, and [[positive-pay]] files cover checks.',
		aliases: ['payment method', 'ACH', 'wire', 'check', 'cheque', 'SEPA', 'BACS', 'RTP'],
		related: ['payment-run', 'virtual-card', 'settlement', 'positive-pay']
	},
	{
		id: 'settlement',
		category: 'payments',
		long: "The point at which the money has actually moved and the bank or processor confirms it. FeohLedger never marks a payment {ui:payments.status.completed} just because it was sent: it waits for the provider's confirmation.\n\nOn confirmation FeohLedger checks that the amount settled is the amount you authorized. If less arrived than you authorized, the invoice is held short of {ui:invoices.status.paid}. A payment the processor never confirms is eventually raised as a {ui:exceptions.type.paymentReconciliation} exception.",
		aliases: ['settled', 'cleared', 'payment confirmation', 'value date'],
		related: ['payment-run', 'bank-reconciliation', 'void']
	},
	{
		id: 'compliance-hold',
		category: 'payments',
		long: "A payment stopped just before it is sent because its compliance check needs a person. When a [[payment-run]] executes, each payment is [[sanctions-screening|screened]] again. If the vendor is a possible match or needs review, or the invoice has no vendor that can be screened, the payment shows {ui:payments.status.pendingCompliance} and a {ui:exceptions.type.paymentComplianceHold} exception opens. No money moves while it is held.\n\nOnce the underlying issue is dealt with, for example the vendor's screening has been reviewed, find the payment on the {ui:payments.tab.history} tab of {ui:nav.payments}. {ui:payments.history.complianceRelease} runs the same checks again and sends the payment only if they now pass, so it is never a way around them. {ui:payments.history.complianceDismiss} gives up on the payment with a reason: it shows {ui:payments.status.failed} and never reaches the processor. By default admins, AP managers and CFOs can release a held payment, and admins and CFOs can dismiss one. Both are recorded in the [[audit-trail]].",
		aliases: ['sanctions hold', 'KYC hold', 'held payment', 'AML hold'],
		related: ['sanctions-screening', 'payment-run', 'exception', 'audit-trail']
	},
	{
		id: 'void',
		category: 'payments',
		long: 'Cancelling a payment so the invoice can be paid again correctly, for example after a lost check or a payment to the wrong account. A voided payment shows as {ui:payments.status.voided} and its invoice returns to {ui:invoices.status.approved}, ready for a new [[payment-run]].\n\nVoiding a [[virtual-card]] payment also closes the card. If an early-payment discount was captured, it is reversed, since nothing was saved. By default only admins and CFOs can void, and every void is written to the [[audit-trail]].',
		aliases: ['void payment', 'reverse payment', 'cancel payment', 'stop payment'],
		related: ['payment-run', 'settlement', 'audit-trail', 'virtual-card']
	},
	{
		id: 'positive-pay',
		category: 'payments',
		long: "A fraud control run with your bank. You send the bank a file listing the checks you actually issued, with their numbers and amounts. When a check is presented, the bank compares it to the file and refuses anything that doesn't match, such as an altered amount or a check you never wrote. A second file type lists the companies allowed to debit your account by ACH.\n\nFeohLedger generates the check-issue file from a [[payment-run]], and can raise a fraud exception when the bank reports a check that isn't on file. Because the file holds full account numbers, it is deleted after a short retention period.",
		aliases: ['positive pay file', 'check fraud file', 'ACH debit block', 'issue file'],
		related: ['payment-run', 'payment-rail', 'retention-policy']
	},
	{
		id: 'virtual-card',
		category: 'payments',
		long: "A single-use card number created to pay one invoice, with a spending limit equal to the invoice amount. The supplier charges the card instead of receiving a bank transfer. Each card can be charged once, for no more than its limit, and it expires.\n\nSuppliers see their card details in the [[supplier-portal]]. An invoice can have only one live card at a time, so it can't be paid twice by card or by card and transfer. Card payments earn a [[card-rebate]].",
		aliases: ['VCN', 'virtual card number', 'single-use card', 'card payment'],
		related: ['card-rebate', 'payment-rail', 'supplier-portal']
	},
	{
		id: 'card-rebate',
		category: 'payments',
		long: "Cash back your organization earns when a supplier is paid by [[virtual-card]], a share of the card network's fees. FeohLedger records one rebate per card, by period, so the total can be tracked and reported.",
		aliases: ['cashback', 'interchange rebate', 'card cashback'],
		related: ['virtual-card', 'payment-rail']
	},
	{
		id: 'dynamic-discounting',
		category: 'payments',
		long: 'Paying a supplier early in return for a discount, with the terms agreed per offer rather than fixed on the invoice. An offer can slide with timing, such as 3% off if paid in five days or 2% in ten. Offers come from suppliers through the [[supplier-portal]] or from standard terms like 2/10 net 30.\n\nFeohLedger ranks open offers by their annualized return so you can see which ones beat your other uses of cash. Each offer moves from offered to accepted to captured, or ends declined or expired, and the savings are tracked in {ui:nav.discounts}.',
		aliases: ['early payment discount', 'early-pay discount', '2/10 net 30', 'prompt payment discount', 'supply chain finance'],
		related: ['payment-run', 'supplier-portal', 'void']
	},
	{
		id: 'bank-reconciliation',
		category: 'payments',
		long: "Checking that the payments you made match what left your bank account. You import a bank statement, and FeohLedger matches each debit to a payment, showing how each was matched. Anything it can't match stays visible for you to resolve by hand.\n\nAn outstanding view lists payments that haven't cleared and bank debits with no payment behind them, for month-end close. This is the after-the-money check; [[statement-reconciliation]] is the before.",
		aliases: ['bank rec', 'cash reconciliation', 'statement matching'],
		related: ['statement-reconciliation', 'settlement', 'payment-run']
	},
	{
		id: 'statement-reconciliation',
		category: 'payments',
		long: "Comparing a supplier's statement of what they think you owe with the open invoices in FeohLedger. You paste or upload the statement, and FeohLedger matches each line to your invoices and sorts every difference into a review list: amount differences, invoices the supplier billed that you don't have, and open invoices missing from their statement.\n\nIt answers *do we and the supplier agree on the balance?* before you pay. [[bank-reconciliation]] answers the question after.",
		aliases: ['vendor statement reconciliation', 'supplier statement', 'statement of account', 'AP reconciliation'],
		related: ['bank-reconciliation', 'vendor', 'invoice']
	},
	{
		id: 'reporting-currency',
		category: 'payments',
		long: 'The one currency your organization reports in. Dashboards and analytics convert every invoice into it so totals in different currencies can be added up honestly, and [[approval-threshold|approval thresholds]] and the [[cfo-gate]] are measured in it.\n\nAn admin sets it in the organization settings. Each invoice keeps its own currency; the converted figure sits beside it, at the [[fx-rate]] locked on that invoice. An invoice with no locked rate is left out of a total and counted, rather than added at face value.',
		aliases: ['base currency', 'home currency', 'functional currency', 'presentation currency'],
		related: ['fx-rate', 'approval-threshold', 'entity']
	},
	{
		id: 'fx-rate',
		category: 'payments',
		long: "The rate used to convert one currency into another. FeohLedger locks a rate onto an invoice when it is recorded and onto a payment when it is sent, and keeps it. A later market move never changes a decision already made, such as which approval threshold an invoice fell under.\n\nThe difference between the invoice's rate and the payment's rate is the realized exchange gain or loss. Open foreign invoices show an unrealized gain or loss against today's rate.",
		aliases: ['exchange rate', 'FX', 'currency conversion', 'foreign exchange'],
		related: ['reporting-currency', 'payment-run', 'approval-threshold']
	},
	{
		id: 'cash-position',
		category: 'payments',
		long: "How much cash you are projected to have, period by period, as your payables go out. On the {ui:nav.cashFlow} page, FeohLedger starts from an opening bank balance and subtracts the invoices falling due in each day, week or month of the horizon you pick, carrying each period's closing balance forward to the next. Approved invoices and ones still in review both count, by their due date or their scheduled payment date where one is set.\n\nThe opening balance is the {ui:cfo.control.openingBalance} you enter; failing that, the balance from your connected bank where that is available; failing that, a balance saved for your organization. Set a {ui:cfo.control.minBalance} and any period that would close below it is marked {ui:cfo.position.belowMinimum}. Figures are in your [[reporting-currency]]. Money coming in from customers is not included, since FeohLedger only sees what you owe. Admins and CFOs can open this page.",
		aliases: ['cash forecast', 'cash projection', 'liquidity', 'running balance', 'cash runway'],
		related: ['days-payable-outstanding', 'reporting-currency', 'payment-run', 'dynamic-discounting']
	},
	{
		id: 'days-payable-outstanding',
		category: 'payments',
		long: "On average, how many days you take to pay your suppliers. A higher figure means you hold on to cash longer; too high, and supplier relationships suffer and early-payment discounts slip away.\n\nFeohLedger shows it as {ui:cfoMetrics.kpi.dpo} on the {ui:nav.cashFlow} page, calculated the classic way: your open payables divided by spend over the period, multiplied by the number of days in the period. FeohLedger doesn't see your cost of goods sold, so it uses your invoice spend in its place; read the figure as an estimate. Open payables are invoices approved but not yet paid; spend is every invoice dated in the period except rejected ones; both are in your [[reporting-currency]]. The {ui:cfoMetrics.dpoTrend.title} chart shows each closed month.",
		aliases: ['DPO', 'days payable', 'payables days', 'creditor days'],
		related: ['cash-position', 'invoice-aging', 'reporting-currency', 'dynamic-discounting']
	},

	// -------------------------------------------------------------------------
	// Vendors
	// -------------------------------------------------------------------------
	{
		id: 'vendor',
		category: 'vendors',
		long: "A supplier you pay. A vendor record holds the name, contact details, tax ID, bank details, payment terms, tax classification and screening history, in {ui:nav.vendors}.\n\nVendors come from your ERP, are added by an admin or AP manager, or are created automatically when [[extraction]] finds an invoice from a supplier you don't have yet. Those automatic ones start unverified and can't be paid until someone reviews them, because a fake vendor is a classic fraud.",
		aliases: ['supplier', 'payee', 'vendor master', 'creditor'],
		related: ['bank-change-request', 'sanctions-screening', 'vendor-risk', 'supplier-portal']
	},
	{
		id: 'bank-change-request',
		category: 'vendors',
		long: "A proposed change to a vendor's bank or tax details, held for approval instead of applied at once. Fraudsters often impersonate a supplier and ask you to update their bank account, a scam known as business email compromise. So in FeohLedger no single person can change where a vendor's money goes.\n\nThe change waits in the bank change approvals queue until a second person, never the one who proposed it, approves it. That applies whether the change came from your team or from the vendor in the [[supplier-portal]]. Account numbers are masked throughout and in the [[audit-trail]].",
		aliases: ['bank detail change', 'BEC', 'vendor bank change', 'change request', 'payment redirection'],
		related: ['segregation-of-duties', 'vendor', 'supplier-portal']
	},
	{
		id: 'sanctions-screening',
		category: 'vendors',
		long: 'Checking vendors against government sanctions lists, politically exposed person lists and adverse news. FeohLedger screens a vendor when it is created or changed, re-screens on a schedule, and screens again before each payment.\n\nA confirmed sanctions match blocks payment. A possible match, or adverse news, holds the payment as {ui:payments.status.pendingCompliance} until a person reviews it. Every screening result is kept as a permanent record.',
		aliases: ['sanctions check', 'OFAC', 'watchlist screening', 'PEP screening', 'KYB', 'AML screening'],
		related: ['vendor-risk', 'vendor', 'payment-run', 'compliance-hold']
	},
	{
		id: 'vendor-risk',
		category: 'vendors',
		long: 'A single score from 0 to 100, with a level from low to critical, that sums up how risky a vendor is. It combines [[sanctions-screening]] results, fraud signals and payment history, so you can see which suppliers deserve a closer look.\n\nThe score is a guide for review; it is the screening and fraud controls themselves that stop a payment.',
		aliases: ['risk score', 'vendor risk score', 'supplier risk', 'third-party risk'],
		related: ['sanctions-screening', 'vendor', 'exception']
	},
	{
		id: 'supplier-portal',
		category: 'vendors',
		long: "A separate sign-in where your suppliers serve themselves. They can submit and resubmit invoices, see their invoices' and payments' status, update company details, upload tax forms, respond to early-payment offers and view [[virtual-card]] details.\n\nSuppliers only ever see their own records. Bank or tax ID changes they make are held as a [[bank-change-request]] for your team to approve.",
		aliases: ['vendor portal', 'supplier self-service', 'vendor self-service'],
		related: ['vendor', 'bank-change-request', 'dynamic-discounting']
	},
	{
		id: 'form-1099',
		category: 'vendors',
		long: "The US tax forms (1099-NEC and 1099-MISC) that report payments to contractors and other non-corporate vendors to the IRS. A vendor must be reported once your payments to them in a calendar year reach the filing threshold.\n\nFeohLedger tracks which vendors are 1099-eligible, their W-9 forms and tax IDs, and totals each vendor's payments for the year in {ui:nav.taxReporting}. Card payments are excluded, because the card network reports those separately.",
		aliases: ['1099', '1099-NEC', '1099-MISC', 'IRS reporting'],
		related: ['vendor', 'form-w9', 'tin-verification', 'payment-rail', 'virtual-card']
	},
	{
		id: 'form-w9',
		category: 'vendors',
		long: "The IRS form a US vendor fills in to give you their legal name, tax classification and taxpayer identification number ([[tin-verification|TIN]]). You need one on file before you report the vendor's payments on a [[form-1099]]. A foreign vendor gives you a W-8 instead.\n\nIn {ui:nav.taxReporting}, an admin or AP manager selects {ui:tax.action.manage} on the vendor and then {ui:tax.vendorModal.uploadW9}, which records the form and the date it was received. Vendors can also upload their own W-9 or W-8 in the [[supplier-portal]]. The {ui:tax.filter.missingW9} filter lists the vendors you still need to chase.",
		aliases: ['W-9', 'W9', 'Form W-9', 'W-8', 'W-8BEN', 'tax form', 'Request for Taxpayer Identification Number'],
		related: ['form-1099', 'tin-verification', 'supplier-portal', 'vendor']
	},
	{
		id: 'tin-verification',
		category: 'vendors',
		long: "Checking that a vendor's taxpayer identification number (TIN), their EIN or SSN, is valid before you report payments to them on a [[form-1099]]. A wrong TIN on a filed 1099 can lead to IRS penalty notices and backup withholding, so it is worth catching early.\n\nIn {ui:nav.taxReporting}, an admin or AP manager selects {ui:tax.action.manage} on a vendor and then {ui:tax.vendorModal.verifyTin}. Depending on how your organization is set up, FeohLedger either checks the number's format or matches it against IRS records through a TIN-matching service. A pass marks the TIN verified; any other result clears the mark. Changing a vendor's TIN, by any route, clears it too, because a match only ever applies to the number that was checked. The {ui:tax.filter.tinUnverified} filter shows who still needs checking.",
		aliases: ['TIN', 'TIN match', 'TIN check', 'EIN', 'SSN', 'taxpayer identification number', 'IRS TIN matching'],
		related: ['form-w9', 'form-1099', 'vendor']
	},

	// -------------------------------------------------------------------------
	// Controls
	// -------------------------------------------------------------------------
	{
		id: 'exception',
		category: 'controls',
		long: "Something about an invoice or payment that needs a person's judgment: a [[duplicate-invoice|possible duplicate]], a PO mismatch, a fraud flag, failed extraction, a compliance hold and so on. Exceptions collect in {ui:nav.exceptions}, where you can resolve, dismiss or [[escalation|escalate]] each one.\n\nEvery decision is recorded in the invoice's [[audit-trail]], so you can always see who cleared a flag and why. Some exceptions are [[payment-blocking-exception|payment-blocking]], and some can be handled by an [[exception-agent]] if your organization has turned them on.",
		aliases: ['flag', 'exception queue', 'issue', 'hold', 'discrepancy'],
		related: ['payment-blocking-exception', 'exception-agent', 'escalation', 'audit-trail']
	},
	{
		id: 'payment-blocking-exception',
		category: 'controls',
		long: "An [[exception]] that stops an invoice from being paid while it is open or escalated. There are four: {ui:exceptions.type.duplicate}, {ui:exceptions.type.fraudFlag}, {ui:exceptions.type.lineTotalMismatch} and {ui:exceptions.type.paymentReconciliation}.\n\nApproving an invoice doesn't clear them, so resolving one is the sign-off that lets the money move. That is why nobody involved in creating or editing the payable, or who raised the flag, may resolve or dismiss it. They can escalate it instead.",
		aliases: ['payment hold', 'blocking exception', 'payment block'],
		related: ['exception', 'segregation-of-duties', 'payment-run', 'duplicate-invoice', 'fraud-flag', 'line-total-mismatch']
	},
	{
		id: 'fraud-flag',
		category: 'controls',
		long: "An [[exception]] raised when something about an invoice or payment looks like fraud. FeohLedger raises a {ui:exceptions.type.fraudFlag} when, for example, an invoice is for a suspiciously round amount, is dated in the future, falls due within days of being issued, comes from a brand-new vendor for a large amount, quotes a remit-to address that differs from earlier approved invoices, comes from a vendor using a personal email provider, or is far above that vendor's usual amount. It also raises one on every invoice awaiting payment when a vendor's bank details change, when a payment settles for a different amount than was sent, and when your bank reports a check that doesn't match your [[positive-pay]] file.\n\nA fraud flag is a [[payment-blocking-exception]]: the invoice can't be paid until someone resolves or dismisses it, and by default nobody involved in creating or editing the invoice may do that. An admin chooses which invoice checks run, and their limits, in {ui:org.section.fraud} in the organization settings.",
		aliases: ['fraud alert', 'suspicious invoice', 'red flag', 'fraud detection', 'fraud rule'],
		related: ['payment-blocking-exception', 'bank-change-request', 'duplicate-invoice', 'positive-pay']
	},
	{
		id: 'audit-trail',
		category: 'controls',
		long: "The permanent, time-stamped record of who did what: every invoice status change, approval, payment, vendor change and exception decision. Entries can't be edited or deleted, and sensitive values such as bank account numbers are masked.\n\nAuditors can review an invoice's full history, export it for a period, and verify the approval signatures in {ui:nav.auditTrail}. It is the evidence behind SOX and SOC 2 controls.",
		aliases: ['audit log', 'activity log', 'history', 'SOX trail'],
		related: ['access-review', 'retention-policy', 'segregation-of-duties']
	},
	{
		id: 'access-review',
		category: 'controls',
		long: "A periodic check that the people with powerful access still need it, which SOX expects. In {ui:nav.accessReview}, FeohLedger lists everyone with elevated access (admin, AP manager, CFO, or a custom role with sensitive permissions) and flags as dormant anyone who hasn't used those rights in 90 days or never has.\n\nAn admin or CFO reviews the list and records their sign-off, which is itself audited. Activity is read straight from the [[audit-trail]], so it can't drift.",
		aliases: ['user access review', 'UAR', 'entitlement review', 'access recertification'],
		related: ['role', 'permission', 'audit-trail']
	},
	{
		id: 'retention-policy',
		category: 'controls',
		long: "How long each kind of record is kept before it is archived. FeohLedger's default is seven years, a common SOX and tax baseline, and an admin can set a different period per record type in {ui:nav.retention}.\n\nArchiving goes through an audited process, never a plain delete. The [[audit-trail]] itself is never deleted. [[positive-pay]] files, which hold full account numbers, default to one month.",
		aliases: ['data retention', 'records retention', 'record keeping'],
		related: ['audit-trail', 'dsar', 'positive-pay']
	},
	{
		id: 'dsar',
		category: 'controls',
		long: 'A data subject access request: a person asking what personal data you hold about them, or asking for it to be erased, under GDPR, CCPA and similar laws. In {ui:nav.privacy} an admin can export everything held about a user, a supplier-portal user or a vendor contact, or erase their personal data.\n\nErasure removes personal details but keeps the financial and audit records your organization is legally required to keep. Exporting unmasked bank details needs a [[step-up]] check first.',
		aliases: ['data subject request', 'subject access request', 'SAR', 'right to erasure', 'GDPR request', 'right to be forgotten'],
		related: ['retention-policy', 'step-up', 'audit-trail', 'data-residency']
	},
	{
		id: 'data-residency',
		category: 'controls',
		long: "Where your organization's data, its database and uploaded files, is required to be stored. Customers subject to GDPR or similar laws often need their data kept in a particular region. FeohLedger supports {ui:org.residency.region.us} (the default), {ui:org.residency.region.eu}, {ui:org.residency.region.uk}, {ui:org.residency.region.ca} and {ui:org.residency.region.au}.\n\nAn admin sets the region under {ui:org.section.dataResidency} in the organization settings, and the change is written to the [[audit-trail]]. Setting it records the requirement; it does not move any data by itself. The {ui:org.residency.alignment.title} panel beside it shows whether the service you are using actually runs in that region, so you can see plainly whether the requirement is met today.",
		aliases: ['data location', 'data sovereignty', 'data localization', 'hosting region', 'GDPR residency'],
		related: ['organization', 'dsar', 'retention-policy']
	},
	{
		id: 'mfa',
		category: 'controls',
		long: "Signing in with something you have as well as something you know, so a stolen password alone can't get into your account. FeohLedger supports an authenticator app, which shows a six-digit code that changes every 30 seconds, and passkeys, such as Touch ID, Face ID, Windows Hello or a security key. A code sent to your email is offered as a backup, so a lost phone doesn't lock you out.\n\nSet it up on your [[page:/profile|profile]] under {ui:profile.mfa.heading} and {ui:profile.passkeys.heading}. An admin can make it compulsory for everyone with {ui:org.security.requireMfa} in the organization's {ui:org.section.security} settings, and you then can't turn it off. Some sensitive actions, such as exporting unmasked bank details in a [[dsar|data request]], need a [[step-up]] check with your authenticator code or passkey, so it is worth setting up early.",
		aliases: ['MFA', '2FA', 'two-step verification', 'multi-factor authentication', 'TOTP', 'authenticator app', 'passkey', 'WebAuthn', 'security key'],
		related: ['step-up', 'organization', 'dsar']
	},

	// -------------------------------------------------------------------------
	// Platform
	// -------------------------------------------------------------------------
	{
		id: 'organization',
		category: 'platform',
		long: "Your company's own FeohLedger account, reached at its own web address. Its data is held in its own separate database, apart from every other customer.\n\nAn admin manages its settings in {ui:nav.organization}: company details, [[reporting-currency]], ERP and payment connections, sign-in and single sign-on, branding and more. One organization can contain several [[entity|entities]].",
		aliases: ['tenant', 'account', 'company', 'workspace'],
		related: ['entity', 'role', 'reporting-currency', 'data-residency']
	},
	{
		id: 'entity',
		category: 'platform',
		long: 'A legal entity or subsidiary inside your [[organization]], each with its own books. Invoices, vendors, POs, payments and workflows belong to an entity, and you can switch between one entity and a consolidated view of all of them.\n\nEach entity can have its own GL accounts on top of the shared chart, and its own default [[workflow]]. Matching and duplicate checks stay inside an entity. When entities bill each other, see [[intercompany]].',
		aliases: ['subsidiary', 'legal entity', 'company code', 'business unit'],
		related: ['organization', 'intercompany', 'reporting-currency']
	},
	{
		id: 'intercompany',
		category: 'platform',
		long: "A transaction between two [[entity|entities]] of the same organization, where one bills the other. FeohLedger can create the matching payable in the receiving entity automatically, linked to the original, so both sets of books reflect it.\n\nThe mirrored invoice carries the original's approval restrictions, so anyone involved in the original can't approve the mirror either. Consolidated reports can show each entity's figures side by side.",
		aliases: ['inter-company', 'intercompany invoice', 'IC', 'related-party transaction'],
		related: ['entity', 'segregation-of-duties', 'invoice']
	},
	{
		id: 'role',
		category: 'platform',
		long: "What a user is allowed to do, based on their job. FeohLedger has four standard roles. Admin sets up the organization and users. AP manager approves invoices and runs payments. AP clerk captures and prepares invoices but never approves. CFO approves, signs off large amounts and sees analytics.\n\nAn admin can also create custom roles that grant specific [[permission|permissions]], for example to split approving a vendor's bank change from executing payments. Roles are managed in {ui:nav.roles}.",
		aliases: ['user role', 'RBAC', 'access level'],
		related: ['permission', 'segregation-of-duties', 'access-review']
	},
	{
		id: 'permission',
		category: 'platform',
		long: "A single sensitive right, such as approving invoices, approving or executing payment runs, voiding payments, approving vendor bank changes, blocking vendors, managing vendors, or managing users. The standard [[role|roles]] come with sensible permissions; custom roles let an admin give each one to exactly the people who should have it.\n\nThis is how an organization separates duties more strictly than the standard roles do. A user's permissions are everything granted by all of their roles together.",
		aliases: ['right', 'entitlement', 'privilege', 'granular permission'],
		related: ['role', 'segregation-of-duties', 'access-review']
	},
	{
		id: 'api-key',
		category: 'platform',
		long: "A secret token that lets another system read your FeohLedger data through the developer API, without a person signing in. Only an admin can create one, in {ui:nav.apiKeys}, and the key is shown once, at creation.\n\nKeys today are read-only, and usage is tracked per key. A key only ever reaches your own organization's data, so revoke any key you no longer use.",
		aliases: ['API token', 'developer API', 'integration key'],
		related: ['webhook', 'erp-sync', 'organization']
	},
	{
		id: 'webhook',
		category: 'platform',
		long: "A message FeohLedger sends to another system the moment something happens, so it doesn't have to keep asking. An admin subscribes a web address to events in {ui:nav.webhooks}: an invoice approved, a payment settled, or an exception raised.\n\nEach message is signed so the receiver can check it really came from FeohLedger. Failed deliveries are retried a limited number of times.",
		aliases: ['outbound webhook', 'event notification', 'callback'],
		related: ['api-key', 'erp-sync']
	},
	{
		id: 'erp-sync',
		category: 'platform',
		long: 'Keeping FeohLedger and your ERP (your main accounting system) in step. Approved invoices are sent to the ERP, showing {ui:invoices.status.sendingToErp}, {ui:invoices.status.sentToErp} and then {ui:invoices.status.postedInErp}. Payments are written back after they are made, and vendors, GL accounts and POs can be brought in from the ERP.\n\nWhich ERP you use, and how, is set by an admin. If a sync step fails, the invoice shows it so it can be retried rather than silently missed.',
		aliases: ['ERP integration', 'ERP export', 'accounting sync', 'posting'],
		related: ['gl-coding', 'purchase-order', 'workflow']
	},
	{
		id: 'ai-assistant',
		category: 'platform',
		long: "A chat in {ui:nav.aiAssistant} where you ask questions about your AP data in plain language, such as what's overdue or how much you spent with a vendor. It looks things up with a fixed set of read-only tools.\n\nIt can't change anything, and it only ever sees your own organization's data. Check important figures against the source pages before acting on them.",
		aliases: ['assistant', 'AP assistant', 'chatbot', 'copilot', 'AI chat'],
		related: ['exception-agent', 'adaptive-workflow']
	},
	{
		id: 'exception-agent',
		category: 'platform',
		long: "An AI helper that works the {ui:nav.exceptions} queue. For each exception it either fixes the issue the same way a person would, such as linking the right PO or correcting a small amount mismatch, or escalates it to a person. Every decision is logged and reviewable.\n\nAgents are off unless your organization turns them on, and they follow the same rules people do: an agent won't resolve something the person who triggered it couldn't. When unsure, for example about currency, it escalates rather than guessing.",
		aliases: ['AI agent', 'autonomous agent', 'exception bot'],
		related: ['exception', 'escalation', 'ai-assistant']
	},
	{
		id: 'adaptive-workflow',
		category: 'platform',
		long: 'Suggestions FeohLedger learns from your approval history, such as which approver is best placed to take an invoice, or whether your auto-approve amount could safely be raised. They are shown in {ui:nav.adaptive}.\n\nThey are advice only until someone applies one. Applying goes through the same audited path as making the change by hand.',
		aliases: ['smart routing', 'AI workflow', 'routing suggestion', 'adaptive approval'],
		related: ['workflow', 'approval-threshold', 'exception-agent', 'workflow-experiment']
	},
	{
		id: 'budget',
		category: 'platform',
		long: "An amount set aside for a department, project, cost center or GL account over a period. FeohLedger tracks spend against it as committed (requisitions and POs) and actual (invoices), always calculated from the live records so it can't drift.\n\nWhen you raise a [[requisition]], FeohLedger tells you if it would take a budget over. Budgets are managed in {ui:nav.budgets}.",
		aliases: ['budget tracking', 'spend budget', 'cost center budget', 'allocation'],
		related: ['requisition', 'purchase-order', 'gl-coding']
	},
	{
		id: 'intake-request',
		category: 'platform',
		long: "A request to buy something outside the purchase-order process, such as new software, a services engagement or hardware, raised before any vendor or PO exists. Anyone can raise one in {ui:nav.intake}, answering a short questionnaire for its type, and send it for review with {ui:intake.row.submit}.\n\nAn admin or AP manager approves or rejects it. A rejected request can be reopened, changed and submitted again. An approved one is turned into a [[requisition]] with {ui:intake.row.convertToRequisition}, and from there follows the normal route to a [[purchase-order]]. Not to be confused with [[email-intake]], which is how invoices arrive by email.",
		aliases: ['intake', 'intake form', 'non-PO request', 'buying request', 'procurement intake'],
		related: ['requisition', 'purchase-order', 'budget']
	},
	{
		id: 'requisition',
		category: 'platform',
		long: "An internal request to buy something, made before any order goes to a supplier. You raise it with its items, it is approved, and it becomes a [[purchase-order]].\n\nThe person who raised or edited a requisition can't approve it. A rejected requisition can be reopened, changed and resubmitted.",
		aliases: ['purchase requisition', 'PR', 'purchase request', 'req'],
		related: ['purchase-order', 'budget', 'punch-out', 'intake-request']
	},
	{
		id: 'punch-out',
		category: 'platform',
		long: "Shopping on a supplier's own website from inside your purchasing process. You jump to the supplier's catalog, fill a cart, and return; the cart comes back to FeohLedger and becomes a [[requisition]] with the agreed prices.\n\nPunch-out catalogs sit alongside internal catalogs in {ui:nav.catalogs}.",
		aliases: ['punchout', 'cXML', 'OCI', 'hosted catalog', 'catalog'],
		related: ['requisition', 'purchase-order', 'contract']
	},
	{
		id: 'contract',
		category: 'platform',
		long: "An agreement with a supplier, stored in {ui:nav.contracts} with its document, dates, value and terms. A contract moves from draft to active, and on to expired, terminated or cancelled.\n\nFeohLedger tracks spend against active contracts, raises a {ui:exceptions.type.contractNoncompliant} exception when an invoice doesn't follow its contract, alerts you before renewal dates, and can create a [[purchase-order]] from a contract.",
		aliases: ['vendor contract', 'CLM', 'agreement', 'master service agreement', 'MSA'],
		related: ['vendor', 'purchase-order', 'exception']
	},
	{
		id: 'expense-report',
		category: 'platform',
		long: "A group of an employee's expenses, whether out of pocket or on a company card, submitted together for approval and reimbursement in {ui:nav.expenses}. Each expense is checked against your organization's expense policy.\n\nExpenses in different currencies are converted into the report's currency at a locked rate. The submitter can't approve their own report, and reports above the set amount need a [[cfo-gate|CFO's approval]].",
		aliases: ['expense claim', 'T&E', 'travel and expense', 'reimbursement'],
		related: ['cfo-gate', 'segregation-of-duties', 'fx-rate', 'expense-policy', 'spend-preapproval']
	},
	{
		id: 'expense-policy',
		category: 'platform',
		long: "Your organization's rules for what employees may claim, checked automatically against every expense. A policy can cover all expenses or a single category, and can set a spending limit, a daily allowance, a mileage rate, the amount above which a receipt is needed, and the amount above which a [[spend-preapproval]] is needed.\n\nMost breaches are flagged on the expense for the approver to see. Two stop the [[expense-report]] from being submitted at all: a missing receipt the policy requires, and a missing pre-approval. Admins and AP managers manage policies on the {ui:expenses.tab.policies} tab of {ui:nav.expenses}. A policy's amounts are in the currency it names, or your [[reporting-currency]] if it names none.",
		aliases: ['T&E policy', 'reimbursement policy', 'spending policy', 'per diem', 'mileage rate'],
		related: ['expense-report', 'spend-preapproval', 'reporting-currency']
	},
	{
		id: 'spend-preapproval',
		category: 'platform',
		long: "Permission to spend, asked for before the expense is incurred, for example ahead of a conference trip. You raise a request with a title, estimated amount, currency, category and justification on the {ui:expenses.tab.preapprovals} tab of {ui:nav.expenses}, and an admin or AP manager approves or rejects it. Nobody can decide their own request.\n\nWhen an [[expense-policy]] requires pre-approval above an amount, an expense over it can't be submitted until an approved pre-approval covers it. A pre-approval covers only its requester's own expenses, and only in its own currency.",
		aliases: ['pre-approval', 'preapproval', 'spend request', 'travel request', 'prior approval'],
		related: ['expense-policy', 'expense-report', 'segregation-of-duties']
	}
];
