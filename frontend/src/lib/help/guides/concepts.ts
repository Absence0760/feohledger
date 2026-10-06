import type { Guide } from '../types.ts';

export const CONCEPT_GUIDES: Guide[] = [
	// ---------------------------------------------------------------------
	{
		id: 'invoice-lifecycle',
		title: 'How an invoice moves through FeohLedger',
		summary:
			'Follow an invoice from capture to done: every status it can hold, what moves it on, and the paths for rejection, failure, voided payments and paying without an ERP.',
		kind: 'concept',
		route: '/invoices',
		sections: [
			{
				heading: 'The lifecycle at a glance',
				blocks: [
					{ type: 'lifecycle' },
					{
						type: 'p',
						text: 'Every [[invoice]] has exactly one status, and it can only move along the paths shown above. A move the diagram does not allow is refused, so an invoice can never skip approval or jump straight to paid. Every status change is written to the [[audit-trail]] with who made it and when.'
					},
					{
						type: 'p',
						text: 'Which stages an invoice actually passes through depends on your organization\'s [[workflow]]. If your admin has turned off a step (for example, there is no ERP export), the invoice simply skips that stage. The workflow is frozen onto each invoice when it is created, so editing the workflow later never changes the rules for invoices already in flight.'
					}
				]
			},
			{
				heading: 'From capture to approval',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:invoices.status.new}: the invoice record exists but nothing has been read from it yet. An invoice entered by hand can go straight to review from here.',
							'{ui:invoices.status.pending}: a file was attached and [[extraction]] is reading it. This usually takes seconds. If it has not finished within a few minutes, the invoice is marked failed so it never sits silently.',
							'{ui:invoices.status.readyForReview}: the data is in and waiting for a person. A reviewer checks and corrects the fields, looks at any warnings and the PO match, then approves or rejects.',
							'{ui:invoices.status.approved}: the invoice has been signed off. Its amount and other financial fields are now locked, because the approval was given for exactly those figures.'
						]
					},
					{
						type: 'p',
						text: 'If your admin has switched on auto-approval, a small invoice, or one read with very high [[confidence-score|confidence]], can go from {ui:invoices.status.pending} straight to {ui:invoices.status.approved}. It still has to pass the same money checks a person would: anything over the [[cfo-gate|CFO threshold]] or the maximum invoice amount falls back to a human reviewer.'
					}
				]
			},
			{
				heading: 'Rejection and rework',
				blocks: [
					{
						type: 'p',
						text: 'A reviewer who finds a problem clicks {ui:invoices.modal.review.reject} and must give a reason. The invoice moves to {ui:invoices.status.rejected}, and the reason is recorded in the audit trail and raised in the exception queue so someone follows it up. If the invoice came from the supplier portal, the supplier sees the reason too.'
					},
					{
						type: 'list',
						items: [
							'Once the problem is fixed, a rejected invoice can go back to {ui:invoices.status.readyForReview} for another round of review.',
							'If it needs to start over (for example, the wrong document was uploaded), it can go back to {ui:invoices.status.new} instead.'
						]
					}
				]
			},
			{
				heading: 'Sending to the ERP, failures and retries',
				blocks: [
					{
						type: 'p',
						text: 'If your organization exports invoices to an ERP, an approved invoice moves to {ui:invoices.status.sendingToErp} while it is being sent, then {ui:invoices.status.sentToErp} once the ERP confirms it arrived, and {ui:invoices.status.postedInErp} once the ERP reports it has been booked. Each send carries a unique reference, so the ERP can recognize a retry instead of booking a second bill.'
					},
					{
						type: 'p',
						text: '{ui:invoices.status.failed} means a stage could not finish. It is never a dead end:'
					},
					{
						type: 'list',
						items: [
							'If extraction failed, open the invoice and click {ui:invoices.modal.reExtract} to try again. You can also correct the fields by hand.',
							'If the ERP send failed on an invoice that was already approved, click {ui:invoices.modal.erp.retrySend}. Short network hiccups are retried automatically first; you only see this when the ERP refused the invoice or kept failing.'
						]
					}
				]
			},
			{
				heading: 'Payment, voids and done',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:invoices.status.paymentScheduled}: the invoice is in a payment run that has been executed, and the money is on its way.',
							'{ui:invoices.status.paid}: the payment processor confirmed the money moved, and the amount it reported covers the invoice. If the processor reports less than was authorized, the invoice stays at {ui:invoices.status.paymentScheduled} until someone looks into it.',
							'{ui:invoices.status.done}: the workflow is closed. This is the final status, and a done invoice can no longer be changed.'
						]
					},
					{
						type: 'p',
						text: 'You do not need an ERP to pay. Without one, an invoice goes straight from {ui:invoices.status.approved} to {ui:invoices.status.paymentScheduled}, and is still marked {ui:invoices.status.paid} when the payment settles.'
					},
					{
						type: 'p',
						text: 'If a payment has to be reversed, [[void|voiding]] it sends the invoice from {ui:invoices.status.paymentScheduled} or {ui:invoices.status.paid} back to {ui:invoices.status.approved}. It re-enters the payment queue, keeps its approval, and can be paid again in a later run. The original payment and the void both stay on record.'
					}
				]
			}
		],
		terms: ['invoice', 'workflow', 'extraction', 'erp-sync', 'void', 'audit-trail'],
		related: ['review-invoice', 'approve-invoices', 'how-payments-work', 'void-payment']
	},

	// ---------------------------------------------------------------------
	{
		id: 'segregation-of-duties',
		title: 'Segregation of duties: who may sign off what',
		summary:
			'Understand why FeohLedger sometimes refuses to let you approve, clear or pay something, which people it bars, and how admins split duties with custom roles.',
		kind: 'concept',
		sections: [
			{
				heading: 'The idea: no one completes a money path alone',
				blocks: [
					{
						type: 'p',
						text: '[[segregation-of-duties|Segregation of duties]] means the person who creates or shapes a payable is never the person who signs it off. It is the classic accounts payable control, and auditors expect it: if one person can both enter an invoice and approve it, a fictitious or inflated invoice needs no accomplice.'
					},
					{
						type: 'p',
						text: 'FeohLedger enforces this at the point of action, not by policy alone. When a control stops you, the message says which rule applied. That is the system working as intended, not a fault, and the fix is to hand the item to a colleague.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'These checks are on by default. An organization with a single operator can switch some of them off, but that is a deliberate admin decision and it is recorded.'
					}
				]
			},
			{
				heading: 'Approving an invoice: the implicated set',
				blocks: [
					{
						type: 'p',
						text: 'You cannot approve an invoice you are *implicated* in. FeohLedger keeps a set of the people who created or shaped each invoice, and refuses approval from anyone in it:'
					},
					{
						type: 'diagram',
						id: 'segregation-of-duties',
						caption:
							'Whoever brought an invoice in or shaped it is refused at approval, with a message naming the rule. A colleague outside that set approves it. The same rule decides who may clear a [[payment-blocking-exception]] on it.'
					},
					{
						type: 'list',
						items: [
							'The person who entered it: whoever uploaded the file, created it by hand, or imported it from a CSV.',
							'For a [[recurring-invoice]], the person who set up the template, plus anyone who later made a material change to it (vendor, amount, currency, GL coding or schedule). One person could otherwise edit a template and approve every invoice it produces.',
							'For an [[intercompany]] mirror payable, everyone implicated in the original invoice. The mirror copies the original\'s terms, so shaping a payable under one entity bars you from approving its mirror under another.'
						]
					},
					{
						type: 'p',
						text: 'In a multi-level [[approval-chain]], one person can also only approve at one level. Signing two levels would turn a two-person check into a one-person one.'
					},
					{
						type: 'p',
						text: 'Some invoices have no employee who created them: ones that arrived by [[email-intake|email]], over [[peppol|PEPPOL]], or from a supplier through the [[supplier-portal]]. Nobody on your team is implicated, so anyone with approval rights may approve them. Suppliers can never reach an approval, so this does not let a supplier approve their own invoice.'
					}
				]
			},
			{
				heading: 'Clearing a payment-blocking exception',
				blocks: [
					{
						type: 'p',
						text: 'Some exceptions stop an invoice from being paid until a person clears them: suspected duplicates, fraud flags, line totals that do not add up to the header, and payments whose outcome is uncertain. These are [[payment-blocking-exception|payment-blocking exceptions]]. Resolving or dismissing one is effectively a sign-off to pay, so the same rule applies: anyone implicated in the invoice cannot clear its blocking exceptions.'
					},
					{
						type: 'p',
						text: 'If you are barred, use {ui:exceptions.resolveModal.escalate} instead. Escalating hands the decision to someone else, and the invoice stays blocked until they decide. AI exception agents follow the same rule: an agent run started by a barred person escalates rather than clearing anything.'
					}
				]
			},
			{
				heading: 'Bank changes and payment runs: two people, every time',
				blocks: [
					{
						type: 'list',
						items: [
							'**Vendor bank details.** A change to where a vendor is paid is staged, not applied. A second person must approve it on [[page:/vendors/change-requests]], and the person who proposed it cannot. If a supplier submitted the change through the portal, anyone who has held that supplier\'s portal password (for example, the colleague who invited them) is also barred. This is the main defence against bank-redirect fraud.',
							'**After a bank change.** Once a change is approved, every unpaid invoice for that vendor gets a fraud flag that blocks payment. The person who approved the change cannot clear those flags, so a third look happens before money goes to the new account.',
							'**Payment runs.** The person who creates a [[payment-run]] cannot execute it or give the CFO sign-off on it. Someone else has to press the button that moves money.'
						]
					}
				]
			},
			{
				heading: 'Splitting duties with custom roles',
				blocks: [
					{
						type: 'p',
						text: 'The four built-in [[role|roles]] bundle duties together. For example, an AP manager can by default both approve vendor bank changes and execute payment runs. If your organization wants those held by different people, an admin can create custom roles on [[page:/admin?tab=roles]] that grant only specific [[permission|permissions]], such as approving bank changes, executing payments or voiding payments.'
					},
					{
						type: 'p',
						text: 'A person\'s access is the combination of all their roles. The CFO sign-off on large payment runs is deliberately kept to the CFO role, so it cannot be handed out through a custom role.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only admins can create and edit roles. Admins and CFOs can review who holds what on [[page:/admin/access-review]].'
					}
				]
			}
		],
		terms: ['segregation-of-duties', 'payment-blocking-exception', 'bank-change-request', 'role', 'permission', 'approval-chain'],
		related: ['approve-invoices', 'manage-users-roles', 'change-vendor-bank-details', 'fraud-controls']
	},

	// ---------------------------------------------------------------------
	{
		id: 'how-matching-works',
		title: 'How PO matching works',
		summary:
			'Learn how FeohLedger compares an invoice with its purchase order, goods receipts and quality inspections, what each match result means, and what happens when they disagree.',
		kind: 'concept',
		route: '/purchase-orders',
		sections: [
			{
				heading: 'Two-, three- and four-way matching',
				blocks: [
					{
						type: 'p',
						text: 'Matching checks that you are only paying for what was ordered and delivered. When an invoice carries a PO number, FeohLedger finds that [[purchase-order]] and compares them automatically, and it re-checks whenever the invoice changes. How far the comparison goes depends on what documents exist:'
					},
					{
						type: 'list',
						items: [
							'[[two-way-match|Two-way]]: the invoice against the PO. Does the amount agree, within tolerance?',
							'[[three-way-match|Three-way]]: adds the [[goods-receipt|goods receipts]] booked against the PO. Have the quantities actually arrived? Several deliveries against one PO are added together.',
							'[[four-way-match|Four-way]]: adds a [[quality-inspection]]. Did the goods that arrived pass inspection? Inspections are recorded on [[page:/goods-receipts]].'
						]
					},
					{
						type: 'diagram',
						id: 'three-way-match',
						caption:
							'Each variant adds one document to the comparison. Whatever the variant, the result is either a match the approver sees, or a mismatch that flags the invoice and opens an [[exception]] for someone to work.'
					},
					{
						type: 'p',
						text: 'Matching only looks at POs and receipts belonging to the invoice\'s own entity. Two subsidiaries that both use a number like PO-1001 can never be matched against each other\'s orders.'
					}
				]
			},
			{
				heading: 'Reading the match result',
				blocks: [
					{
						type: 'p',
						text: 'The invoice shows a {ui:invoices.modal.poMatch.title} panel with one of these results:'
					},
					{
						type: 'list',
						items: [
							'{ui:invoices.modal.poMatch.matched}: the PO was found and the amounts agree within tolerance (and, where receipts exist, everything ordered has arrived).',
							'{ui:invoices.modal.poMatch.mismatch}: the amounts differ by more than the tolerance, the invoice and PO are in different currencies, or the goods failed inspection.',
							'{ui:invoices.modal.poMatch.partial}: only part of the order has been received, or inspection accepted only part of it.',
							'{ui:invoices.modal.poMatch.notFound}: the invoice names a PO number that does not exist in this entity. An invoice with no PO number is simply not matched.'
						]
					},
					{
						type: 'p',
						text: 'The [[match-tolerance]] is 5% unless your organization has changed it, and it can be set tighter for particular vendors or GL accounts. An invoice and a PO in different currencies are never treated as a match, because comparing euros with dollars at face value would mean nothing.'
					}
				]
			},
			{
				heading: 'When something does not match',
				blocks: [
					{
						type: 'p',
						text: 'A mismatch adds a warning to the invoice and opens an [[exception]] for someone to work. Receiving more than was ordered is flagged even when the amount agrees, because extra quantities are how an invoice for goods nobody authorized gets a receipt to support it. A failed inspection, or a missing one when your organization requires inspections, opens a quality hold.'
					},
					{
						type: 'p',
						text: 'Match results inform the reviewer; they do not decide on their own. A reviewer sees the result before approving, and the exception stays in the queue until someone resolves it. Some PO mismatches can be worked by an AI exception agent if your organization has enabled them. See [[guide:ai-in-feohledger|AI in FeohLedger]].'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Most mismatches come from a PO that was changed after the invoice was issued, or a receipt that has not been booked yet. Check [[page:/purchase-orders]] and [[page:/goods-receipts]] before rejecting the invoice.'
					}
				]
			}
		],
		terms: ['purchase-order', 'goods-receipt', 'two-way-match', 'three-way-match', 'four-way-match', 'match-tolerance'],
		related: ['match-invoices', 'purchase-orders-receipts', 'work-exceptions', 'review-invoice']
	},

	// ---------------------------------------------------------------------
	{
		id: 'fraud-controls',
		title: 'The fraud controls that protect your payments',
		summary:
			'See the checks FeohLedger runs between an invoice arriving and money leaving: duplicate and fraud detection, bank-change dual control, sanctions screening, Positive Pay, the audit trail and step-up.',
		kind: 'concept',
		sections: [
			{
				heading: 'Checks on every invoice',
				blocks: [
					{
						type: 'p',
						text: 'Every invoice is checked when it arrives and again whenever it changes. Findings appear as warnings on the invoice, and the serious ones also open an [[exception]].'
					},
					{
						type: 'list',
						items: [
							'**Duplicates.** Another invoice from the same vendor with the same invoice number is flagged, even if the number is written differently (INV-001 and INV 1 count as the same). A separate check spots near-identical documents, such as a resent invoice with one field changed, including across your entities.',
							'**Suspicious patterns.** Round amounts, unusually short payment terms, a large first invoice from a newly added vendor, an amount far above the vendor\'s normal range, a personal email address, and a remit-to address that differs from the vendor\'s last paid invoice. Your admin can tune or switch off the individual rules.',
							'**Split invoices.** Approval limits are checked against the invoice plus the same vendor\'s other recent invoices (the last 7 days unless changed). Splitting one large bill into several small ones does not get it under the CFO threshold.',
							'**Line totals.** If the line items openly disagree with the invoice total, that is flagged. The total is never quietly recalculated from the lines, because the total is what gets paid and what was approved.'
						]
					},
					{
						type: 'p',
						text: 'Suspected duplicates, fraud flags and line-total disagreements are [[payment-blocking-exception|payment-blocking]]. An invoice can still be approved, but no payment run will include it until someone who is not implicated in the invoice resolves or dismisses the exception.'
					}
				]
			},
			{
				heading: 'Protecting vendor bank details',
				blocks: [
					{
						type: 'p',
						text: 'Redirecting a vendor\'s payments to a fraudster\'s account is the most damaging attack on accounts payable, so a [[bank-change-request|bank change]] goes through several layers:'
					},
					{
						type: 'steps',
						items: [
							'The change is staged, not applied. Nothing changes until a second person approves it on [[page:/vendors/change-requests]], and the proposer can never approve their own request.',
							'Once approved, the vendor is re-screened against the new details.',
							'Every unpaid invoice for that vendor gets a fraud flag that blocks payment, and the person who approved the change cannot clear it. Someone else confirms before money goes to the new account.'
						]
					},
					{
						type: 'p',
						text: 'Full bank account numbers never appear in the audit trail or in error messages; only the last four digits do.'
					}
				]
			},
			{
				heading: 'Sanctions screening',
				blocks: [
					{
						type: 'p',
						text: 'Vendors are [[sanctions-screening|screened]] against sanctions and watch lists before every payment. Where screening is switched on for your organization, they are also screened when they are created, when their name, tax ID or bank country changes, and on a regular schedule.'
					},
					{
						type: 'list',
						items: [
							'A confirmed match blocks all payments to the vendor. The block stays until someone with permission to block vendors deliberately lifts it.',
							'A result that needs review puts the payment on {ui:payments.status.pendingCompliance} and opens an exception. Releasing it re-runs the same screen, so a hold cannot be pushed through without the underlying problem being fixed.',
							'If screening cannot run at all, the payment is held rather than sent unscreened.'
						]
					},
					{
						type: 'p',
						text: 'Screening results and the review queue are on [[page:/vendors/screening]].'
					}
				]
			},
			{
				heading: 'Checks on the money itself',
				blocks: [
					{
						type: 'list',
						items: [
							'**Positive Pay.** For check payments, you can send your bank a file of the checks you actually issued from [[page:/positive-pay]]. The bank refuses to clear an altered or unknown check, and any such items the bank reports back become fraud flags. See [[positive-pay]].',
							'**Settlement checks.** When a payment processor confirms a payment, FeohLedger compares the amount and currency it reports with what you authorized. A difference raises a fraud flag, and an invoice that was paid short is held back from {ui:invoices.status.paid}.',
							'**Two people per payment run.** The person who creates a run cannot execute it, and runs above your threshold need a CFO\'s sign-off. See [[guide:how-payments-work|How payments work]].'
						]
					}
				]
			},
			{
				heading: 'The audit trail and step-up',
				blocks: [
					{
						type: 'p',
						text: 'Every status change, approval, payment, void, bank change and settings change writes a row to the [[audit-trail]]. Rows can be added but never edited or deleted, so the record of who did what cannot be rewritten after the fact. Admins and CFOs can search it on [[page:/audit]].'
					},
					{
						type: 'p',
						text: '[[step-up|Step-up]] asks you to prove it is really you, with your authenticator code or passkey, before an especially sensitive action: changing your own sign-in factors, or exporting a privacy request that includes unmasked bank details. A stolen session on its own is not enough to do either.'
					}
				]
			}
		],
		terms: ['duplicate-invoice', 'fraud-flag', 'payment-blocking-exception', 'bank-change-request', 'sanctions-screening', 'positive-pay', 'audit-trail'],
		related: ['segregation-of-duties', 'change-vendor-bank-details', 'review-screening', 'work-exceptions']
	},

	// ---------------------------------------------------------------------
	{
		id: 'how-payments-work',
		title: 'How payments work',
		summary:
			'Understand how approved invoices become a payment run, who must sign off before money moves, what happens at execution, and how settlement, failures and voids are handled.',
		kind: 'concept',
		roles: ['admin', 'ap_manager', 'cfo'],
		route: '/payments',
		sections: [
			{
				heading: 'From approved invoice to payment run',
				blocks: [
					{
						type: 'p',
						text: 'Approved invoices that are not yet paid wait in the {ui:payments.tab.queue} tab on [[page:/payments]], soonest due first. Paying them happens in two deliberate steps, so someone can review exactly what is about to go out before any money moves.'
					},
					{
						type: 'diagram',
						id: 'payment-run',
						caption:
							'From approved invoice to {ui:invoices.status.paid}. Sanctions screening and the blocked-vendor check happen at execution, and blocking exceptions are checked again there, so a flag raised while a draft run waits still stops that payment.'
					},
					{
						type: 'steps',
						items: [
							'Select invoices in the queue and click {ui:payments.queue.reviewAndPay}. Choose a [[payment-rail]] for each one: ACH, wire, check or virtual card.',
							'Create the draft run. This books the payments as pending; no money has moved yet.',
							'If the run is above your organization\'s CFO threshold, a CFO reviews it and clicks {ui:paymentRuns.runDetail.approveAsCfo}.',
							'A different person from the one who created the run clicks **Execute**. Only now are payments sent.'
						]
					},
					{
						type: 'list',
						items: [
							'One run holds one currency. Mixing currencies would make the run total, and the CFO threshold check on it, meaningless.',
							'The CFO threshold is set in your organization\'s [[reporting-currency]]. A run in another currency is converted at the rate already locked on its invoices, and if it cannot be converted it is treated as needing sign-off.',
							'Open [[credit-memo|credit memos]] applied to an invoice are netted off before it is paid.',
							'A queue row that cannot be paid yet (for example, it has a [[payment-blocking-exception]]) says why, so you can clear it or leave it out.'
						]
					}
				]
			},
			{
				heading: 'What happens at execution',
				blocks: [
					{
						type: 'p',
						text: 'Just before each payment is sent, FeohLedger re-checks it: that the invoice is still payable, that no blocking exception has appeared since the run was drafted, and that the vendor passes [[sanctions-screening]]. A payment that fails screening is refused or put on {ui:payments.status.pendingCompliance}, and the rest of the run carries on.'
					},
					{
						type: 'p',
						text: 'Each payment is sent with its own unique reference, so if a request is repeated the processor recognizes it and does not pay twice. Invoices in the run move to {ui:invoices.status.paymentScheduled}.'
					}
				]
			},
			{
				heading: 'Settlement and marking invoices paid',
				blocks: [
					{
						type: 'p',
						text: 'A payment is {ui:payments.status.submitted} or {ui:payments.status.processing} until the processor confirms it, then {ui:payments.status.completed}. On completion FeohLedger checks the [[settlement]] amount and currency against what you authorized, then marks the invoice {ui:invoices.status.paid} and, if you have an ERP connected, records the payment there.'
					},
					{
						type: 'list',
						items: [
							'If the processor reports a different amount or currency, a fraud flag is raised.',
							'If it reports less than the invoice needed, the invoice stays at {ui:invoices.status.paymentScheduled} until someone accepts the settlement or voids the payment.',
							'Without an ERP, invoices are still marked paid; there is just nothing to send.'
						]
					}
				]
			},
			{
				heading: 'Failures, retries and voids',
				blocks: [
					{
						type: 'p',
						text: 'Each payment that fails keeps its reason, which you can see in the run. Retrying failed payments books a new attempt rather than overwriting the old one, so the history of both attempts is kept. A payment is only retried when FeohLedger can tell it never reached the processor.'
					},
					{
						type: 'p',
						text: 'To reverse a payment, use {ui:payments.history.void} in the {ui:payments.tab.history} tab and give a reason. The payment becomes {ui:payments.status.voided} and the invoice returns to {ui:invoices.status.approved} so it can be paid again. Voiding a virtual card payment also closes the card. The original settlement date is preserved on the record.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'By default, admins, AP managers and CFOs can create and execute payment runs, only CFOs can give the CFO sign-off, and only admins and CFOs can void a payment. Your admin may have split these duties differently with custom roles. AP clerks do not see the payments page.'
					}
				]
			}
		],
		terms: ['payment-run', 'payment-rail', 'cfo-gate', 'settlement', 'void', 'reporting-currency'],
		related: ['run-payments', 'void-payment', 'segregation-of-duties', 'currencies-and-entities']
	},

	// ---------------------------------------------------------------------
	{
		id: 'currencies-and-entities',
		title: 'Currencies and entities',
		summary:
			'Learn how FeohLedger labels every amount with its currency, rolls totals into a reporting currency, handles exchange rates, and separates subsidiaries with the entity switcher.',
		kind: 'concept',
		sections: [
			{
				heading: 'Every amount carries its own currency',
				blocks: [
					{
						type: 'p',
						text: 'An invoice, payment, credit memo or PO is always shown in its own currency, with that currency\'s code or symbol. A euro invoice reads in euros everywhere, even if your organization reports in dollars. FeohLedger never assumes a currency it does not know.'
					},
					{
						type: 'list',
						items: [
							'If a figure\'s currency genuinely is not known, it is shown as a plain number with no symbol. A missing symbol is a gap you can ask about; a wrong one would be a wrong number that looks right.',
							'Where amounts in several currencies are listed together, each is totalled separately and shown side by side, never added into one meaningless sum.',
							'Money is stored and calculated exactly, to the cent, with no rounding drift.'
						]
					}
				]
			},
			{
				heading: 'Reporting currency and exchange rates',
				blocks: [
					{
						type: 'p',
						text: 'Your organization has one [[reporting-currency]], set by your admin. Dashboards, analytics and other organization-wide totals are expressed in it, and they say which currency they are in.'
					},
					{
						type: 'list',
						items: [
							'When a foreign-currency invoice is saved, it is converted to the reporting currency and the [[fx-rate|exchange rate]] used is stored with it. Totals reuse that stored rate rather than looking up a new one each time, so the same report gives the same answer tomorrow.',
							'Approval limits and the CFO threshold for payment runs are amounts in the reporting currency. A foreign invoice is compared using its stored converted amount, so a GBP invoice cannot slip under a USD limit just because the number is smaller. If an amount cannot be converted, the control assumes it is over the limit and asks a person.',
							'When a foreign invoice is paid, the rate is fixed at the moment of payment, and the difference from the original rate is recorded as an exchange gain or loss.'
						]
					}
				]
			},
			{
				heading: 'Entities and the switcher',
				blocks: [
					{
						type: 'p',
						text: 'An [[entity]] is a legal entity or subsidiary inside your organization. Invoices, vendors, POs, payments, exceptions and other records each belong to one entity. Your admin adds entities on [[page:/admin/entities]], and each can have its own currency, chart of accounts and approval workflow.'
					},
					{
						type: 'list',
						items: [
							'Once there is more than one entity, a switcher appears in the sidebar. Pick an entity to see and create records for that subsidiary only, or {ui:entity.all} for the consolidated view.',
							'New records are created under the entity you have selected, or the default entity when you are in the consolidated view.',
							'Some general ledger accounts are shared by every entity; others belong to one. An invoice can only be coded to an account in its own entity\'s chart.'
						]
					}
				]
			},
			{
				heading: 'Inter-company invoices',
				blocks: [
					{
						type: 'p',
						text: 'When one of your entities bills another, open the invoice and use {ui:invoices.modal.intercompany.route}. FeohLedger creates a matching payable under the other entity, with the same amount, currency and vendor, so both sets of books show it. This records the [[intercompany]] charge; it does not move any money.'
					},
					{
						type: 'p',
						text: 'The mirror payable goes through its own review and approval. Anyone implicated in the original invoice cannot approve the mirror, so moving a payable between subsidiaries never becomes a way around [[segregation-of-duties]]. Admins and CFOs can compare entities side by side in the by-entity breakdown on [[page:/cfo]].'
					}
				]
			}
		],
		terms: ['reporting-currency', 'fx-rate', 'entity', 'intercompany', 'gl-coding'],
		related: ['manage-entities', 'configure-organization', 'reports-and-analytics', 'how-payments-work']
	},

	// ---------------------------------------------------------------------
	{
		id: 'ai-in-feohledger',
		title: 'AI in FeohLedger: what it does and what it never does',
		summary:
			'Know where AI helps in FeohLedger, how much it is allowed to do on its own, and which decisions always stay with a person.',
		kind: 'concept',
		sections: [
			{
				heading: 'Reading invoices',
				blocks: [
					{
						type: 'p',
						text: '[[extraction|Extraction]] reads an uploaded invoice and fills in the vendor, amounts, dates, line items and a suggested GL code. Each field carries a [[confidence-score]], and fields the AI was unsure of are marked for you to check.'
					},
					{
						type: 'list',
						items: [
							'Structured e-invoices (such as those arriving over PEPPOL) are read exactly from their data, with no AI guesswork.',
							'Before finishing, extraction checks its own work: do the subtotal and tax add up to the total, is the due date after the invoice date, do the lines add up? A failed check lowers confidence and adds a warning.',
							'Once your chart of accounts is set up, a suggested GL code must exist in the invoice\'s own chart. Codes the AI invents are dropped and flagged.',
							'When you correct a field for a vendor, that correction can be reused on the vendor\'s next invoice for fields the AI was unsure of. These appear under {ui:invoices.modal.priors.cacheGroupTitle}.',
							'If the vendor is not recognized, a new vendor record is created as unverified and the invoice is flagged so someone checks it.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Extracted invoices go to a person for review. Your admin can switch on auto-approval for very high-confidence invoices, but it is off by default, and it never applies to an invoice that needs CFO approval or exceeds the maximum amount.'
					}
				]
			},
			{
				heading: 'Answering questions',
				blocks: [
					{
						type: 'p',
						text: 'The [[ai-assistant]] on [[page:/assistant]] answers plain-language questions such as "what is waiting for my approval?" or "what do we owe next month?". It can only look things up: it lists invoices, finds invoices by description, shows vendor spend, shows the approval queue and forecasts upcoming payments. It cannot change anything.'
					},
					{
						type: 'p',
						text: 'It only sees your organization\'s data, and only the entity you have selected. The [[page:/cash-flow]] copilot goes one step further for finance leaders: it can propose a payment plan and create a *draft* payment run from it. A draft moves no money; it still needs the normal sign-offs and a person to execute it.'
					}
				]
			},
			{
				heading: 'Working exceptions',
				blocks: [
					{
						type: 'p',
						text: '[[exception-agent|Exception agents]] can work certain exceptions for you: a small PO amount mismatch, an invoice missing its PO link, or a missing GL code that the vendor\'s history makes obvious. An agent only runs when a person clicks {ui:exceptions.agents.run.action} on the {ui:exceptions.tab.agents} tab of [[page:/exceptions]].'
					},
					{
						type: 'list',
						items: [
							'By default agents are set to the most cautious level, where they never act on their own: they explain what they found and escalate to a person. Your admin can allow them to act when they are highly confident.',
							'When an agent does act, it may correct the invoice and approve it, but only with the authority of the person who started it. Every normal check still applies: segregation of duties, named approvers, the CFO threshold and the maximum amount. If any check would refuse that person, the agent escalates instead.',
							'Agents never clear suspected duplicates or fraud flags. Those always go to a person.',
							'Every agent decision, with its confidence and reasoning, is logged on the {ui:exceptions.tab.agents} tab, and any change it makes is also in the audit trail.'
						]
					}
				]
			},
			{
				heading: 'Suggestions from your history',
				blocks: [
					{
						type: 'p',
						text: 'Several features learn from your own past invoices and approvals. These use plain statistics, not an AI model, and they only suggest:'
					},
					{
						type: 'list',
						items: [
							'**Data enrichment** suggests the GL code, cost center or payment terms a vendor usually has, flags unit prices far from that vendor\'s norm, and points out vendor records that look like duplicates. It never overwrites a value already on the invoice, and duplicate vendors are only merged when a person confirms.',
							'[[adaptive-workflow|Adaptive workflows]] on [[page:/adaptive]] spot unusual invoices, recommend which approver to route an invoice to, and suggest raising the auto-approve limit when vendors\' approval history supports it. Nothing changes until a person clicks to apply a suggestion, and raising the auto-approve limit is admin-only and recorded like any other workflow change.'
						]
					}
				]
			},
			{
				heading: 'What AI never does',
				blocks: [
					{
						type: 'list',
						items: [
							'It never moves money: no AI feature executes a payment run or sends a payment.',
							'It never approves a vendor bank change, blocks or unblocks a vendor, or changes who has access.',
							'It never bypasses segregation of duties, the CFO threshold or any other approval control. Where it acts, it acts as the person who asked, with that person\'s limits.',
							'It never clears a payment-blocking exception.',
							'It never reads another organization\'s data.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If an AI result looks wrong, correct it. Corrections to extracted fields are recorded and help the next invoice from the same vendor.'
					}
				]
			}
		],
		terms: ['extraction', 'confidence-score', 'ai-assistant', 'exception-agent', 'adaptive-workflow'],
		related: ['review-invoice', 'use-assistant', 'work-exceptions', 'design-workflows']
	}
];
