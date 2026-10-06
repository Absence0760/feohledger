// How-to guides for the invoice side of AP: getting invoices in, reviewing,
// approving, matching, working exceptions, credit memos and recurring bills.
// Prose is English; UI labels go through `{ui:…}` (see ../types.ts).

import type { Guide } from '../types.ts';

export const INVOICE_GUIDES: Guide[] = [
	{
		id: 'capture-invoices',
		title: 'Get invoices into FeohLedger',
		summary:
			'Upload invoice files, give vendors an email address to send to, import open AP from a spreadsheet, and know what happens to each invoice next.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'],
		route: '/invoices',
		sections: [
			{
				heading: 'Upload invoice files',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/invoices]].',
							'Click {ui:invoices.action.upload} and pick one or more files. PDF, PNG, JPG and TIFF files are accepted, and you can select several at once.',
							'Each file becomes its own invoice and starts at {ui:invoices.status.pending} while FeohLedger reads it.',
							'To key in an invoice by hand instead, use {ui:invoices.action.create}.'
						]
					},
					{
						type: 'p',
						text: 'A structured [[e-invoice]] that arrives as a PDF (Factur-X or ZUGFeRD, which carry the invoice data inside the file) is read directly from that data rather than by AI, so every field comes through exactly.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins, AP managers, CFOs and AP clerks can upload and create invoices. An invoice an AP clerk brings in always goes to a person for approval, even if your workflow would otherwise approve it automatically, and the clerk can never approve it themselves.'
					}
				]
			},
			{
				heading: 'Let vendors email invoices to you',
				blocks: [
					{
						type: 'p',
						text: 'With [[email-intake]], your organization gets a private address. Every PDF, image or XML e-invoice attached to an email sent there becomes an invoice and goes through the same reading and approval as an upload. The email body is kept as the invoice description.'
					},
					{
						type: 'steps',
						items: [
							'An admin opens [[page:/organization]] and finds the {ui:org.section.emailIntake} section.',
							'If there is no address yet, click {ui:org.emailIntake.generate}.',
							'Copy the {ui:org.emailIntake.addressLabel} and give it to your vendors.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Treat the address like a password. Anything sent to it lands in your AP queue, and vendors get no reply. If it leaks, an admin clicks {ui:org.emailIntake.rotate}: the old address stops working immediately, so tell your vendors the new one straight away.'
					}
				]
			},
			{
				heading: 'Import open AP from a spreadsheet',
				blocks: [
					{
						type: 'p',
						text: 'When you move to FeohLedger, {ui:invoices.action.importCsv} loads your existing invoices from a CSV file. Each row needs an invoice number, a vendor and an amount; vendors that don\'t exist yet are created as unverified.'
					},
					{
						type: 'list',
						items: [
							'Import invoices that still need paying as {ui:invoices.status.new}, so they go through approval like any other invoice.',
							'Import history that was already settled in your old system as done or paid, so it never re-enters the approval queue.',
							'You can\'t import an invoice straight into an approved or scheduled state — that would skip the second pair of eyes.',
							'The import attaches no files and runs no AI reading. You are recorded as the person who added each invoice, so you can\'t approve them yourself.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins, AP managers and AP clerks can import. An AP clerk can import only open invoices ({ui:invoices.status.new} or {ui:invoices.status.rejected}); rows that record past payments (paid or done, which is also what a blank status means) are refused for them. CFOs can\'t import.'
					}
				]
			},
			{
				heading: 'Other ways invoices arrive',
				blocks: [
					{
						type: 'list',
						items: [
							'**Supplier portal** — invite a vendor from [[page:/vendors]] with {ui:vendors.row.invite}. They can then upload their own invoices through the [[supplier-portal]], and see why one was rejected so they can send a corrected copy.',
							'**PEPPOL** — if your organization is connected to the [[peppol]] network, e-invoices sent to you that way arrive in the queue on their own.',
							'**Recurring templates** — fixed bills like rent can be generated on a schedule. See [[guide:recurring-invoices|Set up recurring invoices]].'
						]
					}
				]
			},
			{
				heading: 'What happens next',
				blocks: [
					{
						type: 'diagram',
						id: 'capture-channels',
						caption:
							'Uploads, emailed invoices and supplier-portal submissions are read by [[extraction]]; a [[peppol|PEPPOL]] e-invoice is read from its own data. A CSV import arrives already typed in, so nothing is read; its [[duplicate-invoice|duplicate]] and fraud checks run as it’s imported. Rows imported as already paid or done are history and aren’t checked.'
					},
					{
						type: 'p',
						text: 'FeohLedger reads the invoice ([[extraction]]): vendor, amounts, dates, line items and a suggested [[gl-coding|GL code]], each with a [[confidence-score]]. It matches the vendor, checks the invoice against its purchase order if it has one, and looks for duplicates and fraud signals. The invoice then moves to {ui:invoices.status.readyForReview}.'
					},
					{
						type: 'p',
						text: 'If reading fails, the invoice shows {ui:invoices.status.failed}. Open it and click {ui:invoices.modal.reExtract}, or fill in the fields yourself. If your organization has switched on auto-approval for high-confidence invoices, some may go straight to {ui:invoices.status.approved} — but never one above the amount that needs a CFO.'
					}
				]
			}
		],
		terms: ['extraction', 'confidence-score', 'email-intake', 'e-invoice', 'supplier-portal', 'peppol'],
		related: ['review-invoice', 'invoice-lifecycle', 'recurring-invoices', 'ai-in-feohledger']
	},
	{
		id: 'review-invoice',
		title: 'Review and correct an invoice',
		summary:
			'Check what was read from an invoice, fix fields and line items, code it to the right GL accounts, and understand the warnings before sending it for approval.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'],
		route: '/invoices',
		sections: [
			{
				heading: 'Check and correct the fields',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/invoices]] and click an invoice. The document shows beside the fields read from it.',
							'Compare the vendor, invoice number, amount, dates and terms with the document. Each field read by AI shows how confident the reading was — look hardest at the low ones.',
							'Correct anything that\'s wrong and click {ui:common.save}.',
							'Vendor, invoice number and amount are required before the invoice can move on.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If a reading looks badly off, you can ask FeohLedger to read the file again with {ui:invoices.modal.extract} (or {ui:invoices.modal.reExtract} after a failed reading) while the invoice is still new or failed — or simply type the right values in.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'AP clerks can correct and code an invoice while it is {ui:invoices.status.new}, {ui:invoices.status.pending}, {ui:invoices.status.failed} or {ui:invoices.status.rejected}, until they submit it. After that, an AP manager, CFO or admin corrects it or rejects it back. A clerk who changes an invoice someone else brought in is recorded on it, so they can never approve it later.'
					}
				]
			},
			{
				heading: 'Code it to the general ledger',
				blocks: [
					{
						type: 'p',
						text: 'Set the {ui:invoices.modal.field.glAccount}, plus cost center, department or project if you use them. If the vendor has approved invoices before, a {ui:invoices.modal.suggestions.title} panel shows what was used last time and how often. Nothing is filled in for you — click {ui:invoices.modal.suggestions.apply} on what you want, then save.'
					},
					{
						type: 'p',
						text: 'Once your chart of accounts is set up, codes are checked against your entity\'s active accounts. A code the AI suggested that isn\'t in your chart is dropped and flagged rather than kept.'
					}
				]
			},
			{
				heading: 'Fix the line items',
				blocks: [
					{
						type: 'steps',
						items: [
							'In {ui:invoices.modal.lineItems.title}, edit a line\'s description, quantity, unit price, tax, total or GL account, or click {ui:invoices.modal.lineItems.addLine}.',
							'Click {ui:invoices.modal.lineItems.save}.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Changing line items never changes the invoice amount — that is the figure that gets paid, and only an explicit edit may move it. If the lines no longer add up to the amount, FeohLedger flags a [[payment-blocking-exception|line-total mismatch]], and the invoice can\'t be paid until the two agree or someone resolves the exception.'
					}
				]
			},
			{
				heading: 'Read the warnings',
				blocks: [
					{
						type: 'p',
						text: 'Warnings appear on the invoice and as an icon on its row in the list. They don\'t stop you saving; they tell you what to check.'
					},
					{
						type: 'list',
						items: [
							'**Duplicates** — the same invoice number from the same vendor, or a [[duplicate-invoice|near-identical invoice]] already on file.',
							'**Fraud signals** — a round amount, a future invoice date, a rush due date, a vendor using a personal email, a new vendor billing a large amount, a changed remit-to address, or an amount far above the vendor\'s usual.',
							'**Matching** — the invoice doesn\'t agree with its purchase order or what was received. See [[guide:match-invoices|Match invoices to purchase orders]].',
							'**Other checks** — prices above the vendor\'s usual, a contract that has expired or been exceeded, amounts that don\'t add up.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'A duplicate, a fraud signal or a line-total mismatch also opens an [[exception]] that keeps the invoice out of any payment run until someone resolves or dismisses it. That gives a person the final say before money leaves.'
					}
				]
			},
			{
				heading: 'See who did what, then send it on',
				blocks: [
					{
						type: 'p',
						text: 'The {ui:invoices.modal.activity.title} section lists every step on the invoice — reading, edits, approvals, rejections — with who did it and when. It is part of the [[audit-trail]] and can\'t be edited.'
					},
					{
						type: 'p',
						text: 'When the invoice is right, click {ui:invoices.modal.submit.forReview} (if your organization asks you to, pick the approver first). See [[guide:approve-invoices|Approve or reject invoices]].'
					}
				]
			}
		],
		terms: ['confidence-score', 'gl-coding', 'line-item', 'line-total-mismatch', 'duplicate-invoice', 'exception', 'audit-trail'],
		related: ['capture-invoices', 'approve-invoices', 'match-invoices', 'fraud-controls']
	},
	{
		id: 'approve-invoices',
		title: 'Approve or reject invoices',
		summary:
			'Work your approval queue, approve or reject with a reason, follow multi-level approval chains, and understand why some approvals are refused.',
		kind: 'howto',
		roles: ['ap_manager', 'cfo', 'admin'],
		route: '/invoices',
		sections: [
			{
				heading: 'Find what is waiting for you',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/invoices]] and click the {ui:invoices.status.readyForReview} filter.',
							'Click {ui:invoices.filter.myApprovals} to narrow the list to invoices assigned to you. Invoices assigned to nobody can be approved by any approver, so check those too.',
							'Click an invoice to open it.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'AP managers, CFOs and admins can approve. AP clerks never see the approve buttons. If an invoice is assigned to someone else, you\'ll see who instead of the buttons.'
					}
				]
			},
			{
				heading: 'Approve or reject',
				blocks: [
					{
						type: 'steps',
						items: [
							'Check the fields, line items, warnings and purchase-order match. You can correct fields and save before deciding.',
							'Click {ui:invoices.modal.review.approve}, or',
							'click {ui:invoices.modal.review.reject}, type the reason, and click {ui:invoices.modal.review.confirmReject}. A reason is required.'
						]
					},
					{
						type: 'p',
						text: 'A rejected invoice goes to {ui:invoices.status.rejected} and opens an exception carrying your reason. If the vendor submitted it through the supplier portal, they see the reason and can send a corrected copy. To send a corrected invoice back for review yourself, fix and save it, then on the list select it and use {ui:invoices.bulk.changeStatus} to move it to {ui:invoices.status.readyForReview}.'
					}
				]
			},
			{
				heading: 'Multi-level approval chains',
				blocks: [
					{
						type: 'p',
						text: 'If your organization uses an [[approval-chain]], an invoice may need several sign-offs, level by level — for example a manager, then a director for larger amounts. The {ui:invoices.modal.chain.title} panel shows each level and how many approvals it still needs. After your approval the invoice stays in {ui:invoices.status.readyForReview} until every level is done, and FeohLedger tells you how many approvals remain.'
					},
					{
						type: 'p',
						text: 'If a level names specific approvers, only they can approve it. You can\'t approve at two different levels of the same invoice.'
					},
					{
						type: 'diagram',
						id: 'approval-chain',
						caption:
							'An example chain. Each level covers a band of amounts, so a larger invoice collects more sign-offs, in order. Above the [[cfo-gate|CFO approval amount]] only a CFO can approve, and above the maximum the invoice is refused. Every amount is compared in your [[reporting-currency]].'
					}
				]
			},
			{
				heading: 'Why an approval can be refused',
				blocks: [
					{
						type: 'note',
						tone: 'caution',
						text: '**You can\'t approve an invoice you brought in.** If you uploaded, created or imported it — or generated it from a recurring template, wrote that template, or changed its vendor, amount, currency, coding or schedule — someone else has to approve it. This is [[segregation-of-duties]]: one person should never be able to both raise a bill and sign it off. It is on by default; only an admin can turn it off, for example for a one-person team.'
					},
					{
						type: 'list',
						items: [
							'**[[cfo-gate|CFO sign-off]]** — above an amount your organization sets, only a CFO can approve.',
							'**Maximum amount** — your organization may set an upper limit above which an invoice is refused outright.',
							'Both thresholds are measured in your organization\'s reporting currency, using the exchange rate fixed on the invoice, so a foreign-currency invoice is judged on its real value.'
						]
					}
				]
			},
			{
				heading: 'Approve several at once',
				blocks: [
					{
						type: 'steps',
						items: [
							'On [[page:/invoices]], tick the invoices you want.',
							'Click {ui:invoices.bulk.changeStatus}, choose {ui:invoices.status.approved} (or {ui:invoices.status.rejected}, which asks for one reason for all of them), and click {ui:common.apply}.'
						]
					},
					{
						type: 'p',
						text: 'Every control above still applies to each invoice. Any that would be refused are skipped and reported, and the rest go through.'
					}
				]
			}
		],
		terms: ['approval-chain', 'approval-threshold', 'cfo-gate', 'segregation-of-duties', 'workflow'],
		related: ['review-invoice', 'segregation-of-duties', 'design-workflows', 'run-payments']
	},
	{
		id: 'match-invoices',
		title: 'Match invoices to purchase orders',
		summary:
			'See how an invoice compares with its purchase order, goods received and quality inspection, and what to do when they don\'t agree.',
		kind: 'howto',
		route: '/invoices',
		sections: [
			{
				heading: 'How an invoice gets matched',
				blocks: [
					{
						type: 'p',
						text: 'When an invoice carries a PO number, FeohLedger finds that [[purchase-order]] in the same entity and compares them. How far the check goes depends on what exists:'
					},
					{
						type: 'list',
						items: [
							'[[two-way-match|Two-way]] — the invoice against the purchase order: vendor and amount.',
							'[[three-way-match|Three-way]] — adds the [[goods-receipt]]: were the quantities billed actually delivered?',
							'[[four-way-match|Four-way]] — adds a [[quality-inspection]]: were the goods delivered actually accepted?'
						]
					},
					{
						type: 'p',
						text: 'There is nothing to start — matching runs on its own when the invoice is read and again whenever it is corrected.'
					}
				]
			},
			{
				heading: 'Read the match on an invoice',
				blocks: [
					{
						type: 'p',
						text: 'Open the invoice and find the {ui:invoices.modal.poMatch.title} panel. It shows the PO number, the PO total, the {ui:invoices.modal.poMatch.variance} and one of these results:'
					},
					{
						type: 'list',
						items: [
							'{ui:invoices.modal.poMatch.matched} — the amounts agree within your [[match-tolerance]].',
							'{ui:invoices.modal.poMatch.mismatch} — the amount is outside tolerance, the currencies differ, or a quality inspection failed.',
							'{ui:invoices.modal.poMatch.partial} — only part of the order has been received or accepted so far.',
							'{ui:invoices.modal.poMatch.notFound} — no purchase order with that number exists in this entity.'
						]
					},
					{
						type: 'p',
						text: 'Tolerance is 5% unless your organization has set its own, and it can be tighter for particular vendors or GL accounts. Receiving more than was ordered is flagged even when the amount matches.'
					}
				]
			},
			{
				heading: 'When it doesn\'t match',
				blocks: [
					{
						type: 'steps',
						items: [
							'Check the PO number on the invoice first. A typo is the most common cause of {ui:invoices.modal.poMatch.notFound}.',
							'For an amount difference, compare the invoice with the order in [[page:/purchase-orders]]. Correct the invoice if it was read wrongly; otherwise query the vendor.',
							'For a partial receipt, check [[page:/goods-receipts]]. If the rest of the goods are on their way, the match updates once they are received.',
							'A mismatch also opens a {ui:exceptions.type.poMismatch} exception. Resolve it with a note once you have settled the difference. See [[guide:work-exceptions|Work the exceptions queue]].'
						]
					}
				]
			},
			{
				heading: 'Record a quality inspection',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/goods-receipts]] and the {ui:goodsReceipts.tabs.inspections} tab.',
							'Click {ui:goodsReceipts.inspections.action.record}, choose the receipt, and record pass, fail or partial. For partial, enter how much was accepted.'
						]
					},
					{
						type: 'p',
						text: 'A pass leaves the match alone. A fail turns it into a mismatch and opens a {ui:exceptions.type.qualityHold} exception; a partial acceptance records the quantity accepted. Whether an inspection is required at all depends on your organization\'s settings, the vendor and the GL account.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers record inspections. Everyone can see them.'
					}
				]
			}
		],
		terms: ['purchase-order', 'goods-receipt', 'three-way-match', 'four-way-match', 'match-tolerance', 'quality-inspection'],
		related: ['how-matching-works', 'purchase-orders-receipts', 'work-exceptions', 'review-invoice']
	},
	{
		id: 'work-exceptions',
		title: 'Work the exceptions queue',
		summary:
			'Resolve, dismiss or escalate flagged problems, clear the ones that block payment, and let AI agents handle routine cases.',
		kind: 'howto',
		roles: ['admin', 'ap_manager'],
		route: '/exceptions',
		sections: [
			{
				heading: 'What lands in the queue',
				blocks: [
					{
						type: 'p',
						text: 'An [[exception]] is a problem FeohLedger found that a person needs to decide on: a likely duplicate, a fraud signal, a purchase-order mismatch, a failed reading, a rejected invoice, a price or contract issue, and so on. Each one links to its invoice and has a severity and a status.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'The exceptions queue is open to admins and AP managers.'
					}
				]
			},
			{
				heading: 'Resolve, dismiss or escalate',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/exceptions]]. The {ui:exceptions.tab.queue} tab shows {ui:exceptions.filter.open} exceptions; use the type and severity filters or search by invoice number or vendor.',
							'Click the invoice link to look at the invoice, and fix the underlying problem if there is one.',
							'Click {ui:exceptions.row.resolve} on the row and write a {ui:exceptions.resolveModal.note} saying what you did.',
							'Choose {ui:exceptions.resolveModal.resolve} when the problem is dealt with, {ui:exceptions.resolveModal.dismiss} when it was a false alarm, or {ui:exceptions.resolveModal.escalate} to hand it to someone more senior.'
						]
					},
					{
						type: 'p',
						text: 'A note is required to resolve or escalate. To handle many at once, tick them and resolve them together — they all get the same note.'
					}
				]
			},
			{
				heading: 'Exceptions that block payment',
				blocks: [
					{
						type: 'p',
						text: 'Four types are [[payment-blocking-exception|payment-blocking]]: {ui:exceptions.type.duplicate}, {ui:exceptions.type.fraudFlag}, {ui:exceptions.type.lineTotalMismatch} and {ui:exceptions.type.paymentReconciliation}. While one is open or escalated, its invoice can\'t go into a payment run, even if it has been approved. Resolving or dismissing it is the sign-off that lets the money move.'
					},
					{
						type: 'diagram',
						id: 'exception-flow',
						caption:
							'Every exception ends resolved or dismissed; escalating only hands it on. A [[payment-blocking-exception|payment-blocking]] one holds its invoice out of every payment run until then.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'You can\'t clear a payment-blocking exception on an invoice you brought in or shaped, or one you raised yourself. Otherwise one person could create a payable and wave its fraud flag through. Escalate it instead, or ask a colleague. When you resolve several at once, any you are involved in are left open and counted in the result.'
					}
				]
			},
			{
				heading: 'Let AI agents take routine cases',
				blocks: [
					{
						type: 'p',
						text: 'The {ui:exceptions.tab.agents} tab shows what the [[exception-agent|exception agents]] have done: how many decisions they made, how often they resolved versus escalated, and a log of {ui:exceptions.agents.log.heading}.'
					},
					{
						type: 'steps',
						items: [
							'Under {ui:exceptions.agents.run.heading}, pick an open or escalated exception and click {ui:exceptions.agents.run.action}.',
							'Confirm. The agent either applies a fix — through the same checks a person faces — or escalates to a human, and records its reasoning either way.'
						]
					},
					{
						type: 'p',
						text: 'Agents act on their own only when your organization has allowed it and they are confident enough. By default every case is escalated to a person. Today agents can fix small purchase-order amount differences, link a missing PO, and fill in missing GL coding; payment-blocking types always go to a person.'
					}
				]
			}
		],
		terms: ['exception', 'payment-blocking-exception', 'fraud-flag', 'line-total-mismatch', 'escalation', 'exception-agent', 'segregation-of-duties'],
		related: ['fraud-controls', 'segregation-of-duties', 'review-invoice', 'run-payments']
	},
	{
		id: 'credit-memos',
		title: 'Record and apply credit memos',
		summary:
			'Record money a vendor owes you back and apply it to one of their unpaid invoices, so the next payment is reduced by the credit.',
		kind: 'howto',
		route: '/credit-memos',
		sections: [
			{
				heading: 'Create a credit memo',
				blocks: [
					{
						type: 'p',
						text: 'A [[credit-memo]] records money a vendor owes you back — a return, an overcharge, a rebate. Once applied to an unpaid invoice, the payment for that invoice is reduced by the credit.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/credit-memos]] and click {ui:creditMemos.new}.',
							'Choose the vendor and enter the memo number, amount, currency and reason.',
							'Optionally pick an invoice under {ui:creditMemos.createModal.invoice} to apply the credit straight away, or leave it as {ui:creditMemos.createModal.noInvoice}.',
							'Click {ui:creditMemos.createModal.create}. An unapplied memo is {ui:creditMemos.status.open}.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers create, apply, edit and void credit memos. Everyone else can view them.'
					}
				]
			},
			{
				heading: 'Apply it to an invoice',
				blocks: [
					{
						type: 'steps',
						items: [
							'Find the open memo and click {ui:creditMemos.row.apply}.',
							'Pick the invoice. The list only offers invoices that can take this credit.',
							'Confirm. The memo becomes {ui:creditMemos.status.applied}.'
						]
					},
					{
						type: 'p',
						text: 'An invoice can take a credit only when it is from the same vendor, in the same entity and the same currency, isn\'t yet paid or closed, and has at least the memo\'s amount still uncredited. Credits can never add up to more than the invoice.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Once a credit is applied, FeohLedger refuses invoice edits that would break it — changing the vendor or currency, or lowering the amount below the credits already applied. Otherwise one vendor\'s credit could end up reducing another vendor\'s payment.'
					}
				]
			},
			{
				heading: 'Correct or void a memo',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:creditMemos.row.edit} — fix an open memo that has never been applied. Every change is recorded.',
							'{ui:creditMemos.row.void} — cancel an open memo you no longer need. Click once, then {ui:creditMemos.row.confirm}.',
							'An applied memo can\'t be edited or voided. It has already changed what the vendor is owed, so it stays as it is for the audit record.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If the list says no invoice can take the credit, the vendor has no unpaid invoice in that currency with enough left — keep the memo open for their next invoice.'
					}
				]
			}
		],
		terms: ['credit-memo', 'vendor', 'entity', 'audit-trail'],
		related: ['run-payments', 'vendor-statements', 'currencies-and-entities']
	},
	{
		id: 'recurring-invoices',
		title: 'Set up recurring invoices',
		summary:
			'Create a template for a fixed bill like rent or a subscription, set its schedule, and generate each period\'s invoice ready for approval.',
		kind: 'howto',
		route: '/recurring',
		sections: [
			{
				heading: 'Create a template',
				blocks: [
					{
						type: 'p',
						text: 'A [[recurring-invoice]] template holds everything about a predictable bill — vendor, amount, currency, coding and schedule — so you don\'t upload and check the same invoice every month.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/recurring]] and click {ui:recurring.action.new}.',
							'Give it a name, choose the vendor and enter the amount and currency.',
							'Add the GL account and any cost center, department, project, PO number or payment terms. Every invoice it generates carries them.',
							'Set the {ui:recurring.modal.field.cadence} ({ui:recurring.cadence.monthly}, {ui:recurring.cadence.quarterly} or {ui:recurring.cadence.annual}), the {ui:recurring.modal.field.dayOfPeriod}, and a {ui:recurring.modal.field.startDate}. Add an {ui:recurring.modal.field.endDate} if the agreement ends.',
							'Click {ui:recurring.modal.create}.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers manage templates. Everyone else can view them.'
					}
				]
			},
			{
				heading: 'How invoices are generated',
				blocks: [
					{
						type: 'p',
						text: 'On each run date FeohLedger generates that period\'s invoice and sends it straight to {ui:invoices.status.readyForReview} — it is already coded, so there is nothing to read. It never generates the same period twice, and it never pays anything: the invoice still needs approval and a payment run like any other.'
					},
					{
						type: 'list',
						items: [
							'Open a template to see its {ui:recurring.modal.upcoming.title} and its {ui:recurring.modal.history.title}.',
							'{ui:recurring.row.generateNow} creates this period\'s invoice immediately, for example when the bill is due early.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Whoever generates the invoice, wrote the template, or later changed its vendor, amount, currency, coding or schedule can\'t approve the invoices it produces. A standing instruction is still a bill someone raised, so a second person signs it off.'
					}
				]
			},
			{
				heading: 'Pause, resume or end',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:recurring.row.pause} stops generation for now; {ui:recurring.row.resume} starts it again.',
							'{ui:recurring.row.end} stops the template for good. It can\'t be restarted.',
							'After the end date, the template generates nothing.'
						]
					},
					{
						type: 'p',
						text: 'If a template is missing its vendor or amount, it can\'t generate, and its row shows {ui:recurring.skip.pill}. Fill in what is missing. After three missed periods in a row it is paused automatically.'
					}
				]
			},
			{
				heading: 'When the real bill differs',
				blocks: [
					{
						type: 'p',
						text: 'If an invoice from that vendor arrives for a different amount than the template — a price rise, an extra seat — it is flagged with a warning for the reviewer. Set how much difference is acceptable with {ui:recurring.modal.field.varianceTolerance}; leave it empty to use your organization\'s default. The flag is advice, not a block: the reviewer decides.'
					}
				]
			}
		],
		terms: ['recurring-invoice', 'gl-coding', 'segregation-of-duties', 'approval-chain'],
		related: ['capture-invoices', 'approve-invoices', 'run-payments']
	}
];
