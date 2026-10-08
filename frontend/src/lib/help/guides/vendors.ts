// Vendors & procurement how-tos. English prose (rendered lang="en" under any
// UI locale); UI labels are catalogue references so they follow the reader's
// language and any rename. Grammar: ../types.ts.

import type { Guide } from '../types.ts';

export const VENDOR_GUIDES: Guide[] = [
	{
		id: 'add-vendor',
		title: 'Add and verify a vendor',
		summary: 'Create a vendor, bring vendors in from your ERP or a CSV, verify the ones found on invoices, and invite a supplier to the portal.',
		kind: 'howto',
		roles: ['admin', 'ap_manager'],
		route: '/vendors',
		sections: [
			{
				heading: 'Where vendors come from',
				blocks: [
					{
						type: 'p',
						text: 'A [[vendor]] is the payee every invoice and payment points at, so the vendor list is worth keeping clean. Vendors reach [[page:/vendors]] in four ways:'
					},
					{
						type: 'list',
						items: [
							'You create one by hand with {ui:vendors.action.newVendor}. It starts as {ui:vendors.status.active}.',
							'{ui:vendors.action.syncErp} pulls the vendor master from your connected ERP, if your organization has one.',
							'{ui:vendors.action.importCsv} loads a list from a spreadsheet, which is the quickest way to start a new organization.',
							'When an invoice arrives from a supplier FeohLedger can\'t match to an existing vendor, it creates the vendor for you as {ui:vendors.status.unverified}. The {ui:vendors.col.source} column shows {ui:vendors.source.aiExtracted} for these.'
						]
					}
				]
			},
			{
				heading: 'Create a vendor',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/vendors]] and select {ui:vendors.action.newVendor}.',
							'Fill in {ui:vendors.createModal.field.name}, and add {ui:vendors.createModal.field.code}, {ui:vendors.createModal.field.taxId}, {ui:vendors.createModal.field.email}, {ui:vendors.createModal.field.phone} and {ui:vendors.createModal.field.address} if you have them.',
							'Select {ui:vendors.createModal.create}. The vendor is screened against sanctions lists straight away, and the result shows in the {ui:vendors.col.screening} column.',
							'Add the vendor\'s bank details separately with the row\'s {ui:vendors.row.bank} action. That change waits for a second approver before it applies; see [[guide:change-vendor-bank-details|Change a vendor\'s bank details]].'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If the same supplier ends up on the list twice, **Merge duplicates** folds them into one record, so their spend and invoices aren\'t split across two.'
					}
				]
			},
			{
				heading: 'Verify vendors found on invoices',
				blocks: [
					{
						type: 'p',
						text: 'An unverified vendor was created from what was printed on an invoice, and nobody has confirmed it is a real supplier you do business with. Invoices from it carry a {ui:invoices.warning.unverifiedVendor} warning and an exception in the queue, and it can\'t be paid, in a payment run or by card, until someone verifies it.'
					},
					{
						type: 'steps',
						items: [
							'On [[page:/vendors]], select the {ui:vendors.filter.unverified} filter.',
							'Open each vendor and check it against what you know: the name, tax ID and address on file, and the invoices linked to it.',
							'Select {ui:vendors.row.verify} if it is genuine, or {ui:vendors.row.reject} if it is a duplicate or not a real supplier. To act on several at once, tick their rows and use the same buttons in the bar that appears.'
						]
					}
				]
			},
			{
				heading: 'Invite a supplier to the portal',
				blocks: [
					{
						type: 'p',
						text: 'The [[supplier-portal]] lets a supplier submit invoices, follow their payments and keep their own details up to date, which cuts down the "where is my payment?" email.'
					},
					{
						type: 'steps',
						items: [
							'On the vendor\'s row, select {ui:vendors.row.invite}.',
							'Enter the supplier contact\'s {ui:vendors.invite.field.fullName} and {ui:vendors.invite.field.email}, then select {ui:vendors.invite.send}.',
							'The supplier is emailed a temporary password. It is never shown to you, so no one in AP ever knows a supplier\'s password.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers can create, verify, reject and invite vendors. CFOs can view the vendor list; AP clerks don\'t see it.'
					}
				]
			}
		],
		terms: ['vendor', 'supplier-portal', 'sanctions-screening', 'erp-sync', 'bank-change-request', 'form-w9', 'tin-verification'],
		related: ['change-vendor-bank-details', 'review-screening', 'connect-erp', 'capture-invoices']
	},
	{
		id: 'change-vendor-bank-details',
		title: 'Change a vendor\'s bank details',
		summary: 'Request a bank-detail change and have a second person approve it before any payment goes to the new account.',
		kind: 'howto',
		roles: ['admin', 'ap_manager'],
		route: '/vendors/change-requests',
		sections: [
			{
				heading: 'Why a bank change needs two people',
				blocks: [
					{
						type: 'p',
						text: 'The most common way AP departments lose money is a fake "we\'ve changed banks" email. A fraudster poses as a real supplier, or takes over their mailbox, and asks you to send future payments to a new account. This is known as business email compromise, or invoice-redirection fraud. The invoices are genuine, so nothing else looks wrong.'
					},
					{
						type: 'p',
						text: 'So in FeohLedger a bank-detail change never applies when it is entered. It becomes a [[bank-change-request]] that waits in [[page:/vendors/change-requests]] until a *second* person approves it. This is [[segregation-of-duties]] applied to the riskiest edit in AP.'
					},
					{
						type: 'diagram',
						id: 'bank-change-dual-control',
						caption:
							'The vendor keeps its current bank details until a second person approves. Approval re-screens the vendor and holds its unpaid invoices for one more look, so a forged change cannot pay out unnoticed.'
					}
				]
			},
			{
				heading: 'Request the change',
				blocks: [
					{
						type: 'steps',
						items: [
							'On [[page:/vendors]], select the vendor\'s {ui:vendors.row.bank} action.',
							'Enter the new details: {ui:vendors.bank.counterpartyId}, {ui:vendors.bank.bankName}, {ui:vendors.bank.destinationCountry}, {ui:vendors.bank.accountLast4} and the routing digits the form asks for. Add a {ui:vendors.bank.mailingAddressSection} if you pay this vendor by check.',
							'Select {ui:common.save}. The change is submitted for approval, and the vendor\'s current details stay in force until it is approved.'
						]
					},
					{
						type: 'p',
						text: 'Suppliers who use the [[supplier-portal]] can request a bank or tax ID change themselves. Theirs land in the same queue, marked {ui:vendors.changeRequests.requester.supplier} in the {ui:vendors.changeRequests.col.requestedBy} column, and need the same approval.'
					}
				]
			},
			{
				heading: 'Approve or reject a change',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/vendors/change-requests]]. It shows {ui:vendors.changeRequests.status.pending} requests first.',
							'Open a request to see the full proposed value.',
							'Before you approve, call the supplier on a phone number you *already had on file*. Never use a number from the email or letter that asked for the change. Note what you did in {ui:vendors.changeRequests.modal.reviewNote}.',
							'Select {ui:vendors.changeRequests.row.approve}, then {ui:vendors.changeRequests.row.confirmApprove}. If anything doesn\'t check out, select {ui:vendors.changeRequests.row.reject} and then {ui:vendors.changeRequests.row.confirmReject}. Rejecting leaves the vendor unchanged.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'You can\'t approve a change you requested yourself; your row shows {ui:vendors.changeRequests.row.youRequested}. You also can\'t approve a portal request from a supplier login that you invited or whose password you reset, because you could have made that request yourself. Another approver has to sign it off. This is deliberate: one person alone should never be able to redirect where money goes.'
					}
				]
			},
			{
				heading: 'What happens after approval',
				blocks: [
					{
						type: 'list',
						items: [
							'The new details apply to the vendor, and the approval is recorded in the [[audit-trail]] with only the last four digits.',
							'The vendor is screened against sanctions lists again, using the new details.',
							'Every invoice from that vendor that is already waiting to be paid gets a {ui:exceptions.type.fraudFlag} exception. That keeps those invoices out of payment runs until someone has looked at them in [[page:/exceptions]].'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers can request changes and open the approval queue. Approving needs the "Approve vendor bank / tax changes" permission, which admins and AP managers have by default. Your admin can give it to fewer people. Without it you can still review and reject.'
					}
				]
			}
		],
		terms: ['bank-change-request', 'segregation-of-duties', 'supplier-portal', 'payment-blocking-exception', 'audit-trail'],
		related: ['add-vendor', 'review-screening', 'work-exceptions', 'fraud-controls']
	},
	{
		id: 'review-screening',
		title: 'Review sanctions screening',
		summary: 'Work the screening review queue, understand each screening status, and block or unblock payments to a vendor.',
		kind: 'howto',
		route: '/vendors/screening',
		sections: [
			{
				heading: 'When vendors are screened',
				blocks: [
					{
						type: 'p',
						text: '[[sanctions-screening]] checks a vendor against sanctions lists, politically exposed person lists and negative news, so you don\'t pay a party you\'re legally barred from paying. FeohLedger screens a vendor:'
					},
					{
						type: 'list',
						items: [
							'when it is created;',
							'when its name, tax ID or bank country changes, including after an approved bank or tax ID change;',
							'before each payment to it;',
							'when someone selects {ui:vendors.screening.queue.action.rescreen}, or {ui:vendors.bulk.screen} for several vendors on [[page:/vendors]];',
							'on a regular schedule, if your organization has turned that on.'
						]
					}
				]
			},
			{
				heading: 'What each status means',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:vendors.screening.status.clear}: no hit. Nothing to do.',
							'{ui:vendors.screening.status.review}: a possible hit that a person needs to judge. Payments to the vendor aren\'t blocked outright, but a payment is held for compliance review instead of being sent.',
							'{ui:vendors.screening.status.match}: the vendor matched a sanctions list. Its payments are blocked automatically, and stay blocked until someone deliberately unblocks them.',
							'{ui:vendors.screening.status.unscreened}: the vendor hasn\'t been screened yet.',
							'{ui:vendors.screening.adverseMedia}: press coverage of fraud or corruption that isn\'t on a formal list. Treat it as a reason to review the relationship, not as an automatic stop.'
						]
					},
					{
						type: 'p',
						text: 'A {ui:vendors.screening.blocked} pill means payments to that vendor are refused, whatever its screening status.'
					}
				]
			},
			{
				heading: 'Work the review queue',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/vendors/screening]]. It lists every vendor whose status is {ui:vendors.screening.status.match} or {ui:vendors.screening.status.review}. The cards at the top count {ui:vendors.screening.queue.kpi.matches}, {ui:vendors.screening.queue.kpi.needsReview} and {ui:vendors.screening.queue.kpi.blocked} across all vendors.',
							'Select {ui:vendors.screening.queue.row.review} on a vendor to see its {ui:vendors.screening.queue.modal.categories}, risk level and full {ui:vendors.screening.queue.history.title}.',
							'If a hit is a false positive, for example a different company with a similar name, record your reasoning in your compliance file. Then select {ui:vendors.screening.queue.action.rescreen} if the vendor\'s details have been corrected.',
							'If you shouldn\'t pay the vendor, select {ui:vendors.screening.queue.action.block} and give a reason. To lift a block once compliance has cleared it, select {ui:vendors.screening.queue.action.unblock}.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'A block stops every payment to the vendor, including payments already in a run, until someone unblocks it. That is the point: a sanctions hit has to be cleared by a person, never quietly bypassed.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can see the queue. Admins and AP managers can re-screen. Blocking and unblocking need the "block vendor" permission, which admins and AP managers have by default. AP clerks and CFOs see the queue read-only.'
					}
				]
			}
		],
		terms: ['sanctions-screening', 'vendor-risk', 'vendor', 'audit-trail', 'compliance-hold'],
		related: ['add-vendor', 'change-vendor-bank-details', 'fraud-controls', 'run-payments']
	},
	{
		id: 'raise-requisition',
		title: 'Request a purchase',
		summary: 'Ask for non-PO spend with an intake form, raise a purchase requisition, get it approved and turn it into a purchase order, or shop a supplier\'s catalog.',
		kind: 'howto',
		route: '/requisitions',
		sections: [
			{
				heading: 'Pick the right starting point',
				blocks: [
					{
						type: 'list',
						items: [
							'**Intake** is for an ask that isn\'t ready to be a purchase yet, such as new software or a services engagement, where reviewers first decide whether to buy at all. An approved intake becomes a requisition.',
							'A [[requisition]] is a request to buy specific items, with quantities and prices. An approved requisition becomes a [[purchase-order]].',
							'**Catalogs** show what to buy and who to buy it from. A [[punch-out]] catalog lets you shop on a supplier\'s own site and bring the cart back as a requisition.'
						]
					}
				]
			},
			{
				heading: 'Submit an intake request',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/intake]] and select {ui:intake.action.new}.',
							'Choose the {ui:intake.modal.field.type}: {ui:intake.type.software}, {ui:intake.type.services}, {ui:intake.type.hardware} or {ui:intake.type.other}. Then fill in the title, estimated amount, justification and the questions for that type, and select {ui:intake.modal.create}.',
							'Select {ui:intake.row.submit}. The request moves to {ui:intake.status.inReview}, and its answers can no longer be edited.',
							'A reviewer selects {ui:intake.row.approve} or {ui:intake.row.reject}. If it is rejected, select {ui:intake.row.reopen} to take it back to {ui:intake.status.open}, fix it using the reviewer\'s reason, and submit it again.',
							'Once it is approved, a reviewer selects {ui:intake.row.convertToRequisition}. This creates a draft requisition carrying your amount, vendor and justification, with you as its requester.'
						]
					}
				]
			},
			{
				heading: 'Raise and approve a requisition',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/requisitions]] and select {ui:requisitions.action.new}.',
							'Give it a title, then add {ui:requisitions.modal.lineItems} with quantities, unit prices and GL accounts. The total is always calculated from the lines. Select {ui:requisitions.modal.create}.',
							'Select {ui:requisitions.row.submit}. The requisition moves to {ui:requisitions.status.pendingApproval} and is locked, so nobody can change the amount while it is being approved.',
							'An approver selects {ui:requisitions.row.approve} or {ui:requisitions.row.reject}. If it is rejected, {ui:requisitions.row.reopen} takes it back to {ui:requisitions.status.draft} so you can rework it.',
							'Once it is approved, an admin or AP manager selects {ui:requisitions.row.convertToPo}. The purchase order is numbered after the requisition, and the requisition moves to {ui:requisitions.status.converted}.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'You can\'t approve a requisition you raised, or one where you changed the lines, vendor, currency or budget. Someone who had no hand in shaping the spend has to approve it. Converting twice never creates a second purchase order; the second attempt just shows you the existing one.'
					}
				]
			},
			{
				heading: 'Buy from a catalog',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/catalogs]]. To find the preferred source for something, select {ui:catalogs.action.showGuided}, search for the item or category and select {ui:catalogs.guided.find}. You\'ll see {ui:catalogs.guided.preferredVendors}, {ui:catalogs.guided.inContractVendors} and {ui:catalogs.guided.matchingItems}.',
							'To shop a supplier\'s site, select {ui:catalogs.row.punchout} on a {ui:catalogs.filter.punchout} catalog, then {ui:catalogs.punchout.start}. The supplier\'s catalog opens in a new tab.',
							'Check out on the supplier\'s site. Back in FeohLedger, select {ui:catalogs.punchout.refresh} until the cart appears.',
							'Select {ui:catalogs.punchout.convert}. Your cart becomes a draft requisition, which you then submit like any other.'
						]
					},
					{
						type: 'p',
						text: 'Punch-out works only with suppliers your organization has set up for it.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Anyone can raise an intake request. Admins, AP managers and AP clerks raise requisitions and can punch out. Admins, AP managers and CFOs approve requisitions. Admins and AP managers review intake, convert requisitions to purchase orders and manage catalogs.'
					}
				]
			}
		],
		terms: ['intake-request', 'requisition', 'purchase-order', 'punch-out', 'segregation-of-duties', 'budget'],
		related: ['purchase-orders-receipts', 'manage-budgets', 'manage-contracts', 'segregation-of-duties']
	},
	{
		id: 'purchase-orders-receipts',
		title: 'Work with purchase orders and goods receipts',
		summary: 'Find purchase orders and the invoices billed against them, record and check what was delivered, and record the quality inspections behind 4-way matching.',
		kind: 'howto',
		route: '/purchase-orders',
		sections: [
			{
				heading: 'Where purchase orders come from',
				blocks: [
					{
						type: 'p',
						text: 'A [[purchase-order]] is the commitment an invoice is matched against. Purchase orders reach [[page:/purchase-orders]] in three ways: {ui:purchaseOrders.action.syncErp} pulls them from your connected ERP; converting an approved requisition creates one; and {ui:contracts.modal.lifecycle.createPo} creates one from a contract.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/purchase-orders]]. Filter by {ui:purchaseOrders.filter.open}, {ui:purchaseOrders.filter.closed} or {ui:purchaseOrders.filter.cancelled}, or search by PO number.',
							'Open a purchase order to see its {ui:purchaseOrders.modal.lineItems} and its linked invoices, which are the invoices that quote this PO number.',
							'If a PO your vendor quotes isn\'t listed yet, ask an admin or AP manager to select {ui:purchaseOrders.action.syncErp}.'
						]
					}
				]
			},
			{
				heading: 'Record and check goods receipts',
				blocks: [
					{
						type: 'p',
						text: 'A [[goods-receipt]] records what was actually delivered against a purchase order. When an invoice\'s PO has receipts, matching compares the invoiced quantities with everything received, across every delivery. That is a [[three-way-match]]. Receipts can be recorded here, or arrive from another system; the receipt shows which.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/goods-receipts]] and select {ui:goodsReceipts.record.action}.',
							'Search for the purchase order and pick it. Each line shows what was {ui:goodsReceipts.record.ordered}, {ui:goodsReceipts.record.alreadyReceived} and what is {ui:goodsReceipts.record.outstanding}.',
							'Enter {ui:goodsReceipts.record.thisDelivery} for each line that arrived. It starts at the outstanding quantity. Leave a line blank if it wasn\'t in this delivery, or enter 0 if it was expected but didn\'t come.',
							'Set the date it arrived and, if the supplier sent one, the {ui:goodsReceipts.record.number}. Then select {ui:goodsReceipts.record.submit}.'
						]
					},
					{
						type: 'p',
						text: 'Invoices that quote the PO are matched again straight away. An invoice that billed for more than had arrived is released once the rest is received, unless you are the person who entered that invoice: a receipt you record yourself doesn\'t release your own invoice, and someone else has to. Receiving more than is outstanding is allowed, but the match flags it as an over-receipt.'
					},
					{
						type: 'steps',
						items: [
							'To see a delivery, open it from the {ui:goodsReceipts.tabs.receipts} tab: the {ui:goodsReceipts.modal.lineItemsReceived} and any {ui:goodsReceipts.modal.inspections} recorded for it.',
							'To undo a receipt that was entered by mistake, open it and select {ui:goodsReceipts.modal.cancelReceipt}, then confirm. Matching stops counting it. Only receipts recorded in FeohLedger can be cancelled here.'
						]
					}
				]
			},
			{
				heading: 'Record a quality inspection',
				blocks: [
					{
						type: 'p',
						text: 'For regulated or high-spec goods, receiving them isn\'t enough: they also have to pass inspection. A [[quality-inspection]] is the fourth leg of a [[four-way-match]]. Whether an invoice needs one depends on your organization\'s matching rules, checked by vendor, then by commodity (GL account), then the organization default.'
					},
					{
						type: 'steps',
						items: [
							'Open the receipt and select {ui:goodsReceipts.modal.recordInspection}. Or, on the {ui:goodsReceipts.tabs.inspections} tab, select {ui:goodsReceipts.inspections.action.record} and choose the {ui:goodsReceipts.inspections.record.receipt}.',
							'Choose the {ui:goodsReceipts.inspections.record.result}: {ui:goodsReceipts.inspections.result.pass}, {ui:goodsReceipts.inspections.result.fail} or {ui:goodsReceipts.inspections.result.partial}. A partial acceptance needs the {ui:goodsReceipts.inspections.record.acceptedQuantity}.',
							'Add the inspector, the date and any {ui:goodsReceipts.inspections.record.notes}. On a failure, these notes are quoted on the invoice, so whoever works the hold can see what went wrong.',
							'Select {ui:goodsReceipts.inspections.record.submit}.'
						]
					},
					{
						type: 'p',
						text: 'A pass leaves the match alone. A fail puts the invoice on a quality hold, which holds payment until a later inspection passes or someone resolves it. A partial acceptance marks the match as partial and shows how much was accepted. If your organization uses a quality management system, {ui:goodsReceipts.inspections.action.sync} pulls inspections from it. A synced inspection marked {ui:goodsReceipts.inspections.unlinked} names no receipt or PO that FeohLedger holds, so matching never reads it.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can view purchase orders, receipts and inspections. Only admins and AP managers can sync from the ERP or a quality system, or record an inspection. For AP clerks and CFOs these pages are read-only, which is enough to chase a mismatch.'
					}
				]
			}
		],
		terms: ['purchase-order', 'goods-receipt', 'quality-inspection', 'three-way-match', 'four-way-match', 'erp-sync'],
		related: ['match-invoices', 'how-matching-works', 'raise-requisition', 'connect-erp']
	},
	{
		id: 'submit-expenses',
		title: 'Submit and approve expenses',
		summary: 'Log expenses with receipts, group them into a report, get pre-approval where policy needs it, and approve or reject other people\'s reports.',
		kind: 'howto',
		route: '/expenses',
		sections: [
			{
				heading: 'Log your expenses',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/expenses]] and, on the {ui:expenses.tab.expenses} tab, select {ui:expenses.action.newExpense}.',
							'Fill in the {ui:expenseModal.field.date}, {ui:expenseModal.field.merchant}, {ui:expenseModal.field.category}, {ui:expenseModal.field.amount} and {ui:expenseModal.field.currency}, and choose the {ui:expenseModal.field.paymentMethod}. For a car trip, enter the {ui:expenseModal.field.mileageMiles}; the amount is checked against your policy\'s mileage rate.',
							'Under {ui:expenseModal.receipt.title}, select {ui:expenseModal.receipt.attach} and add a photo or PDF of the receipt.',
							'Select {ui:expenseModal.create}.'
						]
					}
				]
			},
			{
				heading: 'Get pre-approval when policy needs it',
				blocks: [
					{
						type: 'p',
						text: 'Your organization\'s expense policies (the {ui:expenses.tab.policies} tab) can require a receipt above one amount and a pre-approval above another. Ask before you spend: on the {ui:expenses.tab.preapprovals} tab, select {ui:expenses.action.newRequest}, then describe the spend and its estimated amount. An admin or AP manager other than you approves or rejects it.'
					}
				]
			},
			{
				heading: 'Group them into a report and submit',
				blocks: [
					{
						type: 'steps',
						items: [
							'On the {ui:expenses.tab.reports} tab, select {ui:expenses.action.newReport}, give it a title and select {ui:expenses.newReport.create}.',
							'Open the report and attach your expenses to it with {ui:expenses.reports.attach}. The total adds up the lines, converting any foreign-currency line into the report\'s currency.',
							'Select {ui:expenses.reports.submit}.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Submission is refused while a line is missing a receipt your policy requires, or needs a pre-approval you don\'t have. You\'ll see {ui:expenses.reports.submitBlocked} followed by the problems. Once submitted, the report and its lines are locked, so the total the approver signs off is the total that gets paid.'
					},
					{
						type: 'p',
						text: 'Only you can add lines to, edit or submit a report you created, whatever your role.'
					}
				]
			},
			{
				heading: 'Approve or reject a report',
				blocks: [
					{
						type: 'steps',
						items: [
							'On the {ui:expenses.tab.reports} tab, open a report in {ui:expenses.reports.status.submitted}.',
							'Check each line, its receipt and any policy warnings.',
							'Select {ui:expenses.reports.approve}, or {ui:expenses.reports.reject} with a reason. Rejecting sends the lines back to {ui:expenses.status.draft} so the owner can fix them and report them again.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'You can\'t approve your own report. A report above your organization\'s [[cfo-gate|CFO threshold]] can only be approved by a CFO or admin. The threshold is compared in your reporting currency, so filing in a different currency doesn\'t avoid it.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins, AP managers and AP clerks log expenses and submit reports. Admins, AP managers and CFOs approve reports; admins and AP managers reject them, decide pre-approvals and manage policies. CFOs can view everything here but not create expenses.'
					}
				]
			}
		],
		terms: ['expense-report', 'expense-policy', 'spend-preapproval', 'cfo-gate', 'segregation-of-duties', 'reporting-currency', 'fx-rate'],
		related: ['currencies-and-entities', 'segregation-of-duties', 'virtual-cards', 'reports-and-analytics']
	},
	{
		id: 'manage-contracts',
		title: 'Manage vendor contracts',
		summary: 'Store vendor contracts, track spend against them, get warned when an invoice breaks the terms, and renew or end them.',
		kind: 'howto',
		roles: ['admin', 'ap_manager'],
		route: '/contracts',
		sections: [
			{
				heading: 'Add a contract',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/contracts]] and select {ui:contracts.action.new}.',
							'Fill in the {ui:contracts.modal.field.contractNumber}, {ui:contracts.modal.field.title}, {ui:contracts.modal.field.type} and {ui:contracts.modal.field.vendor}, plus the {ui:contracts.modal.field.startDate} and {ui:contracts.modal.field.endDate}.',
							'Set the {ui:contracts.modal.field.totalValue} and, to cap cumulative spend, a {ui:contracts.modal.field.spendLimit}. Tick {ui:contracts.modal.field.notToExceed} if going over must be treated as a hard breach rather than a warning.',
							'For renewals, set {ui:contracts.modal.field.autoRenew}, the {ui:contracts.modal.field.renewalTerm} and the {ui:contracts.modal.field.renewalNotice}. Add line items if you\'ll create purchase orders from the contract.',
							'Select {ui:contracts.modal.create}. The contract starts in {ui:contracts.status.draft}. Open it to upload the signed {ui:contracts.modal.document.title}, then select {ui:contracts.modal.lifecycle.activate} once it is in force.'
						]
					}
				]
			},
			{
				heading: 'Track spend and compliance',
				blocks: [
					{
						type: 'p',
						text: 'Spend counts against a contract once an invoice is linked to it. Link one from the invoice\'s detail, under {ui:invoices.modal.contract.label}, with {ui:invoices.modal.contract.link}. The contract\'s {ui:contracts.modal.spend.title} then shows the {ui:contracts.modal.spend.invoiced} amount, the {ui:contracts.modal.spend.limit} and what is {ui:contracts.modal.spend.remaining}. Rejected invoices don\'t count.'
					},
					{
						type: 'p',
						text: 'A linked invoice raises an [[exception]] when it falls outside the contract:'
					},
					{
						type: 'list',
						items: [
							'it is dated before the contract starts or after it ends;',
							'the contract has been terminated or cancelled;',
							'it comes from a different vendor than the contract\'s;',
							'it is coded to a GL account the contract doesn\'t allow;',
							'cumulative spend has gone over the spend limit. With not-to-exceed set, this is an error rather than a warning.'
						]
					}
				]
			},
			{
				heading: 'Renew, end or buy against a contract',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:contracts.modal.lifecycle.renew} sets a {ui:contracts.modal.renew.newEndDate} and makes the contract active again.',
							'{ui:contracts.modal.lifecycle.terminate} ends an active or expired contract early. {ui:contracts.modal.lifecycle.cancel} voids a draft or active one.',
							'{ui:contracts.modal.lifecycle.createPo} creates a [[purchase-order]] from a draft or active contract, copying its vendor, line items and currency.',
							'A contract moves to {ui:contracts.status.expired} on its own once its end date passes.'
						]
					},
					{
						type: 'p',
						text: 'If your organization has renewal alerts turned on, the contract\'s owner and the AP managers are notified once a contract enters its renewal notice window.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers create, edit and change the status of contracts. AP clerks and CFOs can view them. Linking an invoice to a contract is open to admins, AP managers and CFOs.'
					}
				]
			}
		],
		terms: ['contract', 'purchase-order', 'exception', 'vendor'],
		related: ['raise-requisition', 'purchase-orders-receipts', 'work-exceptions', 'review-invoice']
	},
	{
		id: 'manage-budgets',
		title: 'Track spend against budgets',
		summary: 'Set a budget for a department, project, cost center or GL account, and see how much of it is committed, spent and left.',
		kind: 'howto',
		roles: ['admin', 'cfo', 'ap_manager'],
		route: '/budgets',
		sections: [
			{
				heading: 'Create a budget',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/budgets]] and select {ui:budgets.action.new}.',
							'Give it a {ui:budgets.modal.field.name} and choose the {ui:budgets.modal.field.dimension} it tracks: **Department**, **Project**, **Cost Center** or **GL Account**.',
							'Enter the {ui:budgets.modal.field.dimensionValue} exactly as it is coded on invoices, for example the department name or the GL account number.',
							'Set the {ui:budgets.modal.field.period}, and a {ui:budgets.modal.field.periodStart} and {ui:budgets.modal.field.periodEnd} if spend should only count inside those dates. Then set the {ui:budgets.modal.field.amount} and {ui:budgets.modal.field.currency}, and select {ui:budgets.modal.create}.'
						]
					}
				]
			},
			{
				heading: 'Read a budget',
				blocks: [
					{
						type: 'p',
						text: 'Open a [[budget]] to see four figures. They are worked out fresh each time you look, so they can\'t drift from the underlying invoices and orders.'
					},
					{
						type: 'list',
						items: [
							'{ui:budgets.modal.detail.allocated}: the amount you set.',
							'{ui:budgets.modal.detail.committed}: spend that is promised but not yet invoiced. That means requisitions linked to this budget that are awaiting approval or approved, plus the uninvoiced part of the purchase orders they became.',
							'{ui:budgets.modal.detail.actual}: invoices coded to this department, project, cost center or GL account that have been approved or gone further. When the budget has start and end dates, only invoices dated inside them count.',
							'{ui:budgets.modal.detail.remaining}: allocated minus committed minus actual. A negative figure means the budget is overspent.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'A budget only counts spend in its own currency; nothing is converted. A budget set up for one subsidiary only counts that subsidiary\'s invoices.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'A budget tracks spend; it doesn\'t stop it. A requisition or invoice that takes a budget over is not refused, so check {ui:budgets.modal.detail.remaining} before you approve.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and CFOs create, edit and delete budgets. AP managers can view them. AP clerks don\'t see this page.'
					}
				]
			}
		],
		terms: ['budget', 'requisition', 'purchase-order', 'gl-coding', 'entity'],
		related: ['raise-requisition', 'reports-and-analytics', 'start-cfo', 'currencies-and-entities']
	}
];
