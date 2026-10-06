import type { Guide } from '../types.ts';

// Payments & insights how-tos. English prose; UI labels go through {ui:…} so
// they render in the reader's language (see ../types.ts).
export const PAYMENT_GUIDES: Guide[] = [
	// ---------------------------------------------------------------------------
	{
		id: 'run-payments',
		title: 'Run a payment run',
		summary:
			'Batch approved invoices into a draft payment run, get it signed off, execute it, and follow each payment through to Paid.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'cfo'],
		route: '/payments',
		sections: [
			{
				heading: 'Build a draft run from the queue',
				blocks: [
					{
						type: 'p',
						text: 'Every invoice that is approved (or already posted to your ERP) and not yet paid appears on the {ui:payments.tab.queue} tab of [[page:/payments]], soonest due date first. Overdue invoices are highlighted, and invoices with an early-payment discount show what you save by paying before the discount date.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/payments]] and stay on the {ui:payments.tab.queue} tab.',
							'Tick the invoices you want to pay. The bar at the bottom shows how many you picked and their total.',
							'Click {ui:payments.queue.reviewAndPay}.',
							'Choose a payment method for each invoice: ACH, Wire, Check or Virtual Card.',
							'Click **Create Draft Run**. The run opens as a {ui:paymentRuns.status.draft}. No money has moved yet.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'One run pays one currency. If your selection mixes currencies, {ui:payments.queue.reviewAndPay} is disabled. A run has a single total, and that total is what the CFO sign-off threshold is checked against. Adding a dollar total to a euro total would make that check meaningless.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins, AP managers and CFOs can create a run. AP clerks don\'t see the Payments page.'
					}
				]
			},
			{
				heading: 'Get the run signed off and execute it',
				blocks: [
					{
						type: 'p',
						text: 'A draft run waits for a second person. FeohLedger applies two controls before money moves:'
					},
					{
						type: 'list',
						items: [
							'**Maker-checker.** The person who created the run cannot execute it or give CFO sign-off on it. Someone else has to. This is on by default; an admin can turn it off for a single-operator organization.',
							'**CFO sign-off above a threshold.** If your organization has set a CFO approval amount and the run total is above it, the run shows {ui:paymentRuns.runDetail.awaitingCfo} until a user with the CFO role clicks {ui:paymentRuns.runDetail.approveAsCfo}. The threshold is in your organization\'s [[reporting-currency]], so a foreign-currency run is converted at the rate locked on its invoices before the comparison. If that can\'t be done, the run is treated as over the threshold.'
						]
					},
					{
						type: 'steps',
						items: [
							'Open the run from the {ui:payments.tab.runs} tab.',
							'If it needs CFO sign-off, a CFO (not the run\'s creator) clicks {ui:paymentRuns.runDetail.approveAsCfo}.',
							'A user allowed to execute payments clicks **Execute**, then clicks again to confirm. The second click sends the payments.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only a user who actually holds the CFO role can give CFO sign-off. Admins can\'t, because a sign-off you can grant yourself isn\'t a control. By default admins, AP managers and CFOs can execute.'
					},
					{
						type: 'p',
						text: 'Changed your mind before executing? {ui:paymentRuns.runDetail.cancelRun} discards the draft and returns its invoices to the queue.'
					}
				]
			},
			{
				heading: 'What can stop a payment',
				blocks: [
					{
						type: 'p',
						text: 'Some invoices appear in the queue with a reason instead of a checkbox. Others clear the queue but are stopped when the run executes.'
					},
					{
						type: 'diagram',
						id: 'payment-run',
						caption:
							'The path a run takes, and the three gates that can stop a payment on it. Both sign-off and execution need someone other than the run\'s creator, and CFO sign-off is only needed above your organization\'s threshold.'
					},
					{
						type: 'list',
						items: [
							'**Unresolved exceptions.** An open [[payment-blocking-exception]], such as a possible duplicate, a fraud flag, a line-total mismatch or an earlier payment that never confirmed, keeps the invoice out of every run until someone resolves or dismisses the exception on [[page:/exceptions]]. An escalated exception still blocks, because someone is still working on it. The check runs again at execution, so a flag raised while the draft was waiting also stops the payment.',
							'**Sanctions screening at payment time.** Each payment is screened just before it is sent. If screening needs a human review, the payment stops at {ui:payments.status.pendingCompliance} and a compliance exception opens. On the {ui:payments.tab.history} tab, {ui:payments.history.complianceRelease} runs the same checks again (it never skips them), and {ui:payments.history.complianceDismiss} gives up on the payment without sending anything.',
							'**A blocked vendor or a sanctions match.** Payments to a vendor blocked on [[page:/vendors/screening]], or a vendor that matches a sanctions list, are refused and marked {ui:payments.status.failed}.',
							'**Credit memos.** Applied [[credit-memo|credit memos]] are subtracted from the payment. An invoice they fully cover has nothing left to pay and is blocked.',
							'**A live virtual card.** If a card has already been issued for the invoice, the invoice can only be paid by that card.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Approving an invoice doesn\'t clear these flags. The payment run is the last check before money moves, so the flag has to be resolved on its own. Someone involved in creating the invoice can\'t clear a payment-blocking flag on it.'
					}
				]
			},
			{
				heading: 'From executed to Paid',
				blocks: [
					{
						type: 'p',
						text: 'After execution each payment moves through {ui:payments.status.submitted} and {ui:payments.status.processing} to {ui:payments.status.completed}. A payment is only marked {ui:payments.status.completed} when the payment processor confirms it. FeohLedger never assumes money moved. The invoice shows {ui:invoices.status.paymentScheduled} and moves to {ui:invoices.status.paid} when its payment settles. If you have an ERP connected, the payment is also sent to the ERP at that point.'
					},
					{
						type: 'list',
						items: [
							'**Some payments failed.** The run shows {ui:paymentRuns.status.partial} or {ui:paymentRuns.status.failed}, and each failed payment shows why. {ui:payments.runs.retry} on the {ui:payments.tab.runs} tab sends again only the payments that provably never reached the processor. A payment that might have gone through is skipped for a person to check, so a retry can\'t pay twice.',
							'**The processor settled less than you authorized.** The invoice stays at {ui:invoices.status.paymentScheduled}. Once you have agreed the shortfall with the vendor, {ui:payments.history.settlementAccept} on the {ui:payments.tab.history} tab releases it to {ui:invoices.status.paid}. A settled amount that differs from what you authorized also raises a fraud flag.',
							'**An invoice is stuck at Payment Scheduled after the money moved.** {ui:payments.runs.erpSync} on the {ui:payments.tab.runs} tab runs the sync-back again. It doesn\'t move any money, and it\'s safe to repeat.'
						]
					}
				]
			}
		],
		terms: ['payment-run', 'payment-rail', 'cfo-gate', 'segregation-of-duties', 'payment-blocking-exception', 'settlement', 'compliance-hold'],
		related: ['void-payment', 'how-payments-work', 'work-exceptions', 'virtual-cards']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'void-payment',
		title: 'Void a payment',
		summary:
			'Reverse a payment that should not stand: what voiding does to the payment, the invoice and any virtual card, and who is allowed to do it.',
		kind: 'howto',
		roles: ['admin', 'cfo'],
		route: '/payments',
		sections: [
			{
				heading: 'When to void',
				blocks: [
					{
						type: 'p',
						text: 'Void a payment when it went to the wrong place, for the wrong amount, or should never have gone out, and you want the invoice back in the queue. You can void a payment that is {ui:payments.status.submitted}, {ui:payments.status.processing} or {ui:payments.status.completed}.'
					},
					{
						type: 'list',
						items: [
							'A {ui:payments.status.failed} payment never moved money, so there is nothing to void.',
							'A payment on {ui:payments.status.pendingCompliance} never reached the processor. Use {ui:payments.history.complianceDismiss} instead, which also closes its compliance exception.',
							'A draft run that hasn\'t been executed is canceled from the run itself with {ui:paymentRuns.runDetail.cancelRun}.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Voiding needs the payment-void [[permission]]. Admins and CFOs have it by default. AP managers don\'t: they can send money but not reverse it, so those two duties stay with different people. An admin can give the permission to a custom role.'
					}
				]
			},
			{
				heading: 'Void the payment',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/payments]] and go to the {ui:payments.tab.history} tab.',
							'Find the payment (search or the status filters help) and click {ui:payments.history.void}.',
							'Enter a {ui:payments.void.reason}. It is recorded with the void on the [[audit-trail]].',
							'Click {ui:payments.void.confirm}.'
						]
					}
				]
			},
			{
				heading: 'What happens next',
				blocks: [
					{
						type: 'list',
						items: [
							'The payment shows {ui:payments.status.voided}. If it had settled, the original settlement date is kept. The void is recorded separately.',
							'The invoice returns to {ui:invoices.status.approved} and reappears in the payment queue, ready for a new run.',
							'If the payment processor supports reversals, FeohLedger asks it to reverse the payment. If it doesn\'t, the void is recorded in FeohLedger only, and someone has to recover the money from the bank or the vendor.',
							'If an early-payment discount was recorded as captured by this payment, it goes back to {ui:discounts.status.accepted}. Nothing was paid, so nothing was saved.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Voiding a virtual-card payment also tries to close the card. If the card couldn\'t be closed, the void dialog stays open and says so, because the card can still be spent. Use {ui:payments.void.card.retry} to try again. If the vendor has already charged the card, it can\'t be closed and you have to recover the money from the vendor or the card provider.'
					}
				]
			}
		],
		terms: ['void', 'permission', 'segregation-of-duties', 'virtual-card', 'audit-trail'],
		related: ['run-payments', 'how-payments-work', 'virtual-cards']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'reconcile-bank',
		title: 'Reconcile payments against your bank statement',
		summary:
			'Import a bank statement and check that every payment you made cleared, and that every debit on the account is one of yours.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'],
		route: '/bank-reconciliation',
		sections: [
			{
				heading: 'Import a statement',
				blocks: [
					{
						type: 'steps',
						items: [
							'Download a CSV statement for the account from your bank.',
							'Open [[page:/bank-reconciliation]] and click {ui:bankRecon.action.import}.',
							'Fill in the {ui:bankRecon.import.account} (a label for the account, such as its last four digits), {ui:bankRecon.import.periodStart}, {ui:bankRecon.import.periodEnd} and {ui:bankRecon.import.currency}, and choose the {ui:bankRecon.import.file}.',
							'Click {ui:bankRecon.import.submit}. FeohLedger reads the debits and matches them to your payments straight away.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Importing the same file twice opens the statement you already imported instead of creating a duplicate. Common bank column names (Date, Amount or Debit/Credit, Reference, Payee) are recognized automatically.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can see reconciliation results. Only admins and AP managers can import, match, clear or delete.'
					}
				]
			},
			{
				heading: 'How lines are matched',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:bankRecon.method.provider_id}: the bank line carries the payment\'s own reference (trace, check or wire number). This match is certain.',
							'{ui:bankRecon.method.amount_date}: exactly one payment has that amount around that date.',
							'{ui:bankRecon.method.fuzzy_vendor}: only the payee name looks similar. This is marked {ui:bankRecon.state.suggested}, which is a guess, not a fact, until someone clicks {ui:bankRecon.action.confirmMatch} or {ui:bankRecon.action.clearMatch}.'
						]
					},
					{
						type: 'p',
						text: 'Knowing which payment a line belongs to isn\'t the same as the line reconciling. A line is a discrepancy, and is not counted as reconciled, when the bank took a different amount ({ui:bankRecon.method.amount_mismatch}), a different currency ({ui:bankRecon.method.currency_mismatch}), or took money for a payment FeohLedger shows as failed, voided or never sent ({ui:bankRecon.method.status_conflict}).'
					}
				]
			},
			{
				heading: 'Work the Outstanding tab',
				blocks: [
					{
						type: 'p',
						text: 'The {ui:bankRecon.tab.outstanding} tab is your month-end worksheet, across every statement you have imported. Each payment appears in exactly one list:'
					},
					{
						type: 'list',
						items: [
							'{ui:bankRecon.section.uncleared}: you sent it, but no bank line shows it yet. Payments in transit belong here. Use the age filter to find the old ones.',
							'{ui:bankRecon.section.unmatched}: money left the account with no payment of yours behind it. Investigate these first.',
							'{ui:bankRecon.section.discrepancies}: the payment was identified, but the bank line doesn\'t reconcile with it.'
						]
					},
					{
						type: 'steps',
						items: [
							'To match a line yourself, open its statement from the {ui:bankRecon.tab.statements} tab.',
							'Click {ui:bankRecon.action.match} on the line and pick the payment from the uncleared list.',
							'If the amounts disagree, the line stays linked and listed as a discrepancy, so the difference isn\'t lost.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Bank reconciliation covers the whole organization and is not split by [[entity]], because a bank account is shared across your entities.'
					}
				]
			}
		],
		terms: ['bank-reconciliation', 'settlement', 'payment-run', 'entity'],
		related: ['run-payments', 'positive-pay-files', 'vendor-statements', 'how-payments-work']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'positive-pay-files',
		title: 'Send Positive Pay files to your bank',
		summary:
			'Generate the file of checks you issued so your bank refuses altered or forged checks, then process the bank\'s return to flag fraud.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'cfo'],
		route: '/positive-pay',
		sections: [
			{
				heading: 'Why Positive Pay',
				blocks: [
					{
						type: 'p',
						text: '[[positive-pay|Positive Pay]] is a control at your bank. You send the bank a list of the checks you actually issued, and the bank refuses to clear any check that doesn\'t match it: an altered amount or a check you never wrote. For ACH, a separate file lists the vendors allowed to debit your account.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and AP managers generate files and process returns. CFOs can view and download files. AP clerks don\'t have access.'
					}
				]
			},
			{
				heading: 'Generate and send a file',
				blocks: [
					{
						type: 'steps',
						items: [
							'Execute the payment run that issues the checks. A draft run hasn\'t issued anything, so only executed runs can be picked.',
							'Open [[page:/positive-pay]] and click {ui:positivePay.action.generate}.',
							'Choose the file type: {ui:positivePay.modal.fileType.checkIssue} or {ui:positivePay.modal.fileType.achAuthorization}.',
							'For a check-issue file, pick the {ui:positivePay.modal.paymentRun}. Choose the {ui:positivePay.modal.bankFormat} your bank expects.',
							'Click {ui:positivePay.modal.generate}, open the file, and use {ui:positivePay.modal.download} to save it for upload to your bank.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Generating the same run in the same format again returns the existing file instead of a second copy.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'The file contains full bank account numbers, because the bank needs them. Store and send it as carefully as you would any banking document. FeohLedger deletes the stored file after a retention period (one month by default) but keeps the record that it existed.'
					}
				]
			},
			{
				heading: 'Process the bank\'s return',
				blocks: [
					{
						type: 'p',
						text: 'When the bank reports which checks were presented, record them so FeohLedger can compare them with what you issued.'
					},
					{
						type: 'steps',
						items: [
							'Open the check-issue file on [[page:/positive-pay]].',
							'Under {ui:positivePay.modal.processReturn}, paste the presented items, one per line as check number, amount.',
							'Click {ui:positivePay.modal.processReturnAction}.'
						]
					},
					{
						type: 'p',
						text: 'An altered check (right number, wrong amount) raises a fraud-flag [[exception]] on its invoice. A check you never issued raises a fraud flag of its own on [[page:/exceptions]]. A fraud flag on an invoice blocks it from further payment runs until someone resolves it.'
					}
				]
			}
		],
		terms: ['positive-pay', 'payment-rail', 'payment-blocking-exception', 'exception'],
		related: ['run-payments', 'reconcile-bank', 'fraud-controls', 'work-exceptions']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'vendor-statements',
		title: 'Reconcile a vendor statement',
		summary:
			'Check a supplier\'s statement of open items against your own ledger, so you agree on what is owed before you close the period.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'],
		route: '/vendor-statements',
		sections: [
			{
				heading: 'Start a reconciliation',
				blocks: [
					{
						type: 'p',
						text: 'Vendor statement reconciliation happens before money moves: does the supplier agree with you on which invoices are still open? (Bank reconciliation is the check after money moves.) Your side is every invoice from that vendor that isn\'t yet paid.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/vendor-statements]] and click {ui:vendorStatements.new}.',
							'Choose the {ui:vendorStatements.modal.vendor} and enter the {ui:vendorStatements.modal.statementDate}, plus the supplier\'s {ui:vendorStatements.modal.statementReference} if it has one, and the {ui:vendorStatements.modal.currency}.',
							'Choose how the statement is arriving: {ui:vendorStatements.modal.intakeModePaste} (invoice number, date and amount per line) or {ui:vendorStatements.modal.intakeModeFile} as a CSV or PDF.',
							'Click {ui:vendorStatements.modal.reconcile}.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'A PDF statement is read by AI and only works if your organization has set this up. If the reader can\'t read a row without guessing, it skips the row and tells you how many it skipped. Upload a CSV if you need every line.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can view reconciliations. Only admins and AP managers can create them and resolve lines.'
					}
				]
			},
			{
				heading: 'Read the results',
				blocks: [
					{
						type: 'p',
						text: 'Each statement line is matched to one of your invoices by invoice number first (spacing, dashes and capitals are ignored). If that fails, it is matched by an exact amount within a few days of the invoice date. Every line gets one of four results:'
					},
					{
						type: 'list',
						items: [
							'{ui:vendorStatements.classification.matched}: same invoice, amounts agree to the cent.',
							'{ui:vendorStatements.classification.amountMismatch}: same invoice, different amount. You need to act on this.',
							'{ui:vendorStatements.classification.missingOurSide}: the supplier billed it and you have no invoice. You need to act on this, because it is a payable you aren\'t tracking.',
							'{ui:vendorStatements.classification.missingTheirSide}: you have an open invoice the supplier didn\'t list, usually because of timing. This is for information only.'
						]
					}
				]
			},
			{
				heading: 'Clear the differences and check close readiness',
				blocks: [
					{
						type: 'steps',
						items: [
							'Chase each difference with the supplier or fix your own records, for example by capturing a missing invoice.',
							'Mark the line {ui:vendorStatements.modal.resolve} once it is sorted, or {ui:vendorStatements.modal.ignore} if it needs no action.',
							'When every line you need to act on is cleared, the reconciliation shows as resolved.'
						]
					},
					{
						type: 'p',
						text: 'The {ui:vendorStatements.kpi.closeReadiness} card looks at each vendor\'s latest reconciliation. A vendor whose unresolved differences add up to more than the materiality threshold blocks the period close until they are cleared.'
					}
				]
			}
		],
		terms: ['statement-reconciliation', 'vendor', 'bank-reconciliation', 'invoice'],
		related: ['reconcile-bank', 'capture-invoices', 'credit-memos']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'early-payment-discounts',
		title: 'Capture early-payment discounts',
		summary:
			'Review supplier discount offers, accept the ones worth more than your cost of capital, and pay at the discounted amount.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'ap_clerk', 'cfo'],
		route: '/discounts',
		sections: [
			{
				heading: 'Find the offers worth taking',
				blocks: [
					{
						type: 'p',
						text: '[[dynamic-discounting|Dynamic discounting]] tracks offers like "pay within 5 days for 3% off", from suppliers or from the invoice\'s own payment terms. [[page:/discounts]] shows what you have {ui:discounts.status.captured}, what you {ui:discounts.chip.missed}, and what is still open.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/discounts]] and filter to {ui:discounts.status.offered}.',
							'In the {ui:discounts.opt.heading} panel, optionally enter a {ui:discounts.opt.budgetLabel} and click {ui:discounts.opt.optimize}.',
							'The optimizer ranks open offers by annualized return compared with your organization\'s cost of capital and picks the best set that fits your budget. An offer with no net due date can\'t be ranked and is listed separately.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'A 2% discount for paying 20 days early is worth about 37% a year. That is usually far more than your cash earns elsewhere, which is why discounts are worth acting on.'
					}
				]
			},
			{
				heading: 'Accept, decline or propose',
				blocks: [
					{
						type: 'steps',
						items: [
							'Click {ui:discounts.row.accept} on an offer, choose the tier (pay by this date, save this much) and click {ui:discounts.modal.acceptOffer}.',
							'Or click {ui:discounts.row.decline} if you won\'t pay early.',
							'To ask a supplier for a discount across all their open invoices, click {ui:discounts.bulk.open}. The offer is created as {ui:discounts.status.offered} and the supplier responds in the supplier portal.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can see offers and run the optimizer. Admins, AP managers and CFOs can accept or decline. Only admins and AP managers can propose an offer to a vendor.'
					}
				]
			},
			{
				heading: 'Pay at the discounted amount',
				blocks: [
					{
						type: 'note',
						tone: 'caution',
						text: 'Accepting an offer doesn\'t reduce the payment by itself. A payment run pays the invoice amount minus applied credit memos. To pay the discounted amount, record the discount as a [[guide:credit-memos|credit memo]] on the invoice before the run. The offer is marked {ui:discounts.status.captured} only when a settled payment exactly matches the discounted amount.'
					},
					{
						type: 'list',
						items: [
							'Pay before the tier\'s pay-by date. After that the offer shows {ui:discounts.status.expired}.',
							'You can\'t accept a discount on an invoice that is already paid.',
							'If the payment is later voided, the discount goes back from {ui:discounts.status.captured} to {ui:discounts.status.accepted}.'
						]
					}
				]
			}
		],
		terms: ['dynamic-discounting', 'credit-memo', 'payment-run', 'supplier-portal'],
		related: ['run-payments', 'credit-memos', 'use-assistant']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'virtual-cards',
		title: 'Pay vendors by virtual card',
		summary:
			'Pay an invoice with a single-use virtual card, see card details when you need them, and track the rebates cards earn.',
		kind: 'howto',
		roles: ['admin', 'ap_manager', 'cfo'],
		route: '/payments',
		sections: [
			{
				heading: 'Pay an invoice by card',
				blocks: [
					{
						type: 'p',
						text: 'A [[virtual-card]] is a card number issued for one invoice, with a spending limit equal to the amount being paid. The vendor charges it like any card payment. Cards can earn your organization a [[card-rebate]].'
					},
					{
						type: 'steps',
						items: [
							'Make sure your admin has turned on virtual cards for your organization. Without that, card payments fail and can be retried once cards are set up.',
							'Build a payment run as usual and choose Virtual Card as the method for the invoice (see [[guide:run-payments|Run a payment run]]).',
							'When the run executes, the card is issued and the vendor is emailed a link to view the card details. The link expires after 7 days and works once. The vendor needs an email address on their vendor record.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'An invoice can have only one live card. If one already exists, the invoice can only be paid by that card, so the vendor isn\'t paid twice. A card that has already been charged can\'t pay a second payment.'
					}
				]
			},
			{
				heading: 'Track cards and see card details',
				blocks: [
					{
						type: 'p',
						text: 'Card payments appear on the {ui:payments.tab.history} tab of [[page:/payments]] like any other payment. The {ui:payments.tab.cards} tab lists every card with its limit and what has been charged.'
					},
					{
						type: 'steps',
						items: [
							'On the {ui:payments.tab.cards} tab, find the card and click {ui:payments.cards.reveal}.',
							'The full number, expiry and CVV are fetched from the card provider at that moment. FeohLedger never stores them.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins, AP managers and CFOs can see card details. Every time someone views them, it is recorded on the [[audit-trail]].'
					},
					{
						type: 'p',
						text: 'To cancel a card, void its payment (see [[guide:void-payment|Void a payment]]). That closes the card and reverses the payment together, so your books and the card provider stay in agreement.'
					}
				]
			},
			{
				heading: 'Record rebates',
				blocks: [
					{
						type: 'p',
						text: '{ui:payments.cards.rebatesThisMonth} and {ui:payments.cards.rebatesYtd} only count rebates that have been confirmed or paid out. Rebates still waiting for confirmation are shown separately so they don\'t inflate the total.'
					},
					{
						type: 'steps',
						items: [
							'When the card provider\'s statement confirms a rebate, click {ui:payments.rebates.confirm} on it in the Rebates table.',
							'When the rebate money arrives, click {ui:payments.rebates.markPaid}.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Neither button moves money. They record what the card provider has already done.'
					}
				]
			}
		],
		terms: ['virtual-card', 'card-rebate', 'payment-rail', 'void'],
		related: ['run-payments', 'void-payment', 'how-payments-work']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'reports-and-analytics',
		title: 'Reports, analytics and exports',
		summary:
			'Find the figures you need: the dashboard, the Cash Flow page, your own reports in the Report Builder, scheduled email reports and 1099 reporting.',
		kind: 'howto',
		route: '/',
		sections: [
			{
				heading: 'The dashboard',
				blocks: [
					{
						type: 'p',
						text: 'The [[page:/]] shows the state of your payables: {ui:dashboard.kpi.invoices}, {ui:dashboard.kpi.paid}, {ui:dashboard.kpi.pending}, {ui:dashboard.kpi.touchlessRate}, open {ui:dashboard.kpi.exceptions} and {ui:dashboard.kpi.staleApprovals}, plus charts for the {ui:dashboard.chart.pipeline}, {ui:dashboard.chart.aging}, {ui:dashboard.chart.topVendors} and early-payment discounts.'
					},
					{
						type: 'list',
						items: [
							'All totals are shown in your organization\'s [[reporting-currency]]. Amounts in other currencies are converted at the rate locked on each invoice. Any that can\'t be converted are left out and counted, not added at face value.',
							'If your organization has several [[entity|entities]], the figures follow the entity selected in the sidebar.'
						]
					}
				]
			},
			{
				heading: 'Cash Flow (for CFOs and admins)',
				blocks: [
					{
						type: 'p',
						text: '[[page:/cfo]] looks ahead at what you will pay out:'
					},
					{
						type: 'list',
						items: [
							'Projected outflows by day, week or month, split into {ui:cfo.kpi.committed} (approved and later) and {ui:cfo.kpi.pipeline}.',
							'{ui:cfo.position.title}: enter an {ui:cfo.control.openingBalance} and an optional {ui:cfo.control.minBalance} to see your running balance and any period that drops below it.',
							'{ui:cfo.whatif.title}: what paying early, on time or late does to outflow and discounts.',
							'{ui:cfo.budgets.title} across the organization, and {ui:cfo.forecastVariance.title} for months you have entered a forecast for.',
							'{ui:cfo.exportCsv} downloads the forecast.'
						]
					}
				]
			},
			{
				heading: 'Build your own report',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/reports]] and pick a **Data source**: Invoices, Payments, Vendors or Expenses.',
							'Add **Group by** dimensions, at least one measure (a sum, count or average), and any filters.',
							'Click **Run report**.',
							'Click **Save report** to keep it. A saved report can be run again or exported with **Export CSV** or **Export PDF**.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Money measures are always grouped by currency, so a report never adds dollars and euros together.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can run reports and open saved ones. Admins, AP managers and CFOs can save, change and delete them.'
					}
				]
			},
			{
				heading: 'Scheduled reports and 1099s',
				blocks: [
					{
						type: 'p',
						text: '{ui:scheduledReports.heading}, at the bottom of [[page:/cfo]], emails a report (AP aging snapshot, cash-flow forecast, invoice, payment or expense register, or vendor spend) daily, weekly or monthly to up to 20 recipients. A schedule always covers the whole organization, not just one entity. A schedule that keeps failing stops itself and says why. CFOs can see schedules; only admins can create or change them.'
					},
					{
						type: 'p',
						text: '[[page:/tax]] lists each vendor\'s payments for the tax year and flags the ones that need a [[form-1099]]. Filters highlight vendors that are {ui:tax.filter.missingW9} or {ui:tax.filter.tinUnverified}. Card payments are left out, because the card processor reports those on a 1099-K.'
					},
					{
						type: 'steps',
						items: [
							'Pick the {ui:tax.taxYear} and work through the vendors that need attention.',
							'Use {ui:tax.action.manage} on a vendor to record their W-9, verify their TIN or download a working-copy 1099 PDF.',
							'When the year is complete, click {ui:tax.fileButton}, choose the form, and confirm.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'E-filing sends the forms to the IRS and can\'t be undone, which is why it asks you to confirm twice. Admins and AP managers can manage vendors and file. CFOs can view the page. E-filing only reaches the IRS if your organization has set up an e-filing provider.'
					}
				]
			}
		],
		terms: ['reporting-currency', 'form-1099', 'budget', 'entity', 'fx-rate', 'touchless-rate', 'invoice-aging', 'days-payable-outstanding'],
		related: ['use-assistant', 'start-cfo', 'currencies-and-entities', 'manage-budgets']
	},

	// ---------------------------------------------------------------------------
	{
		id: 'use-assistant',
		title: 'Ask the AI assistant',
		summary:
			'Ask questions about your payables in plain English, understand what the assistant can and can\'t see, and use the Cash-Flow Copilot to plan payments.',
		kind: 'howto',
		route: '/assistant',
		sections: [
			{
				heading: 'Ask a question',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/assistant]].',
							'Type a question and click {ui:assistant.send}, or click one of the example questions.',
							'Answers stream in, often with a table or chart beside the text. Start a fresh thread with {ui:assistant.newChat}. Earlier threads are listed under {ui:assistant.recent}.'
						]
					},
					{
						type: 'p',
						text: 'Questions it handles well:'
					},
					{
						type: 'list',
						items: [
							'Finding invoices by status, vendor, amount or date, or by describing them ("the cloud hosting invoice from March").',
							'Which approvals are waiting, including the ones waiting on you.',
							'Which vendors you are spending the most with.',
							'What is due to go out in the coming weeks.'
						]
					}
				]
			},
			{
				heading: 'What the assistant can see',
				blocks: [
					{
						type: 'list',
						items: [
							'**Read-only.** The assistant can only run a fixed set of lookups. It can\'t approve, edit, pay or change anything.',
							'**Your organization only,** and only the [[entity]] selected in the sidebar, if you have chosen one.',
							'**Your conversations are private.** Other users can\'t open your threads.',
							'Every lookup it runs is recorded on the [[audit-trail]]. The record names the lookup but doesn\'t include the text of your question.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Answers are summaries of your data, written by AI. Before you act on a figure, check it on the page that owns it, such as the invoice or the payment.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Everyone can use the assistant. Its cash-planning questions (cash position, payment what-ifs, discount and payment plans) are limited to admins, AP managers and CFOs, and it declines them for anyone else.'
					}
				]
			},
			{
				heading: 'The monthly AI budget',
				blocks: [
					{
						type: 'p',
						text: 'Your organization has a monthly AI usage allowance, shared by everyone using the assistant and the Cash-Flow Copilot. The usage meter on both pages shows how much has been used this period. Once it runs out, new questions are refused until the next period starts. Nothing you already have is affected.'
					}
				]
			},
			{
				heading: 'Plan payments with the Cash-Flow Copilot',
				blocks: [
					{
						type: 'p',
						text: '[[page:/cash-flow]] is a version of the assistant for treasury questions: "When are we going to run low on cash?", "Which discounts should I capture?", "Propose a payment plan for the next quarter." It charts your projected cash position. Switch on {ui:cashFlow.consolidated.label} to answer for the whole group rather than the selected entity.'
					},
					{
						type: 'steps',
						items: [
							'Ask for a payment plan. The copilot shows a {ui:cashFlow.plan.title}: what to pay in each period, which discounts to take, and the resulting cash curve.',
							'{ui:cashFlow.plan.actions.savePlan} keeps a snapshot, so you can later compare the plan with what was actually paid.',
							'{ui:cashFlow.plan.actions.createDraftRun} turns the plan\'s payable invoices into a draft payment run. It still needs the usual review, CFO sign-off and execution.',
							'The capture-discounts button accepts the plan\'s chosen discount offers. You click it twice to confirm.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'The copilot never moves money. The most it can do is create a draft run, and only when you click the button. The AI itself can\'t press it.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'The Cash-Flow Copilot is for admins, AP managers and CFOs.'
					}
				]
			}
		],
		terms: ['ai-assistant', 'audit-trail', 'entity', 'payment-run', 'dynamic-discounting'],
		related: ['ai-in-feohledger', 'reports-and-analytics', 'run-payments', 'early-payment-discounts']
	}
];
