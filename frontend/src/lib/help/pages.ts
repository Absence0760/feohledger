// One line of help per app page, keyed by the page's href exactly as `NAV`
// (lib/nav.ts) writes it, plus the two pages reached from the sidebar chrome
// (/profile, /notifications). This map drives each page's "How this page
// works" link and the Help centre's page directory. `summary` is one plain
// sentence, English, inline markup allowed; `guide` is the most relevant
// guide id. content.test.ts fails if a NAV href has no entry here.

export interface PageHelp {
	summary: string;
	guide?: string;
}

export const PAGE_HELP: Record<string, PageHelp> = {
	'/': {
		summary: 'Your overview of payables: totals, aging, upcoming payments, top vendors and trends for the selected entity.',
		guide: 'getting-around'
	},
	'/invoices': {
		summary: 'Every invoice in one list. Find, review, approve and track invoices from arrival through to payment.',
		guide: 'review-invoice'
	},
	'/payments': {
		summary: 'Pick approved invoices to pay, build and execute payment runs, and look back through payment history.',
		guide: 'run-payments'
	},
	'/vendors': {
		summary: 'Your supplier records: add, import or sync vendors, verify new ones and check their risk and screening status.',
		guide: 'add-vendor'
	},
	'/vendors/screening': {
		summary: 'Vendors whose sanctions screening needs a human look, with their screening history and what to do next.',
		guide: 'review-screening'
	},
	'/vendors/change-requests': {
		summary: 'Pending changes to vendor bank or tax details. Each one applies only after a second person approves it.',
		guide: 'change-vendor-bank-details'
	},
	'/exceptions': {
		summary: 'The queue of issues holding invoices back, such as duplicates, fraud flags and match failures, to resolve, escalate or dismiss.',
		guide: 'work-exceptions'
	},
	'/purchase-orders': {
		summary: 'Purchase orders from your ERP, the documents invoices are matched against.',
		guide: 'purchase-orders-receipts'
	},
	'/goods-receipts': {
		summary: 'Records of goods received and quality inspections, used to check that what was invoiced actually arrived.',
		guide: 'purchase-orders-receipts'
	},
	'/requisitions': {
		summary: 'Request a purchase, send it for approval, and turn an approved requisition into a purchase order.',
		guide: 'raise-requisition'
	},
	'/intake': {
		summary: 'Request forms for spend that does not start with a purchase order, from draft through approval.',
		guide: 'raise-requisition'
	},
	'/catalogs': {
		summary: 'Supplier catalogs and punch-out sites to buy from when you raise a requisition.',
		guide: 'raise-requisition'
	},
	'/gl-accounts': {
		summary: 'Your chart of accounts: the GL codes invoices, expenses and budgets are coded to, kept in step with your ERP.',
		guide: 'review-invoice'
	},
	'/budgets': {
		summary: 'Spending limits by department, project, cost center or GL account, and how much of each has been used.',
		guide: 'manage-budgets'
	},
	'/contracts': {
		summary: 'Supplier contracts, their terms and documents, and how much has been invoiced against each one.',
		guide: 'manage-contracts'
	},
	'/expenses': {
		summary: 'Record expenses and receipts, group them into reports, submit them for approval, and reconcile card transactions.',
		guide: 'submit-expenses'
	},
	'/credit-memos': {
		summary: 'Credits suppliers owe you: record them and apply them against open invoices from the same vendor.',
		guide: 'credit-memos'
	},
	'/discounts': {
		summary: 'Early-payment discount offers and what each one is worth, so you can pay early where it pays off.',
		guide: 'early-payment-discounts'
	},
	'/recurring': {
		summary: 'Templates for invoices that arrive on a schedule, such as rent or subscriptions, and the invoices they generate.',
		guide: 'recurring-invoices'
	},
	'/vendor-statements': {
		summary: 'Import a supplier statement and reconcile it line by line against what you have on record for that vendor.',
		guide: 'vendor-statements'
	},
	'/positive-pay': {
		summary: 'Create Positive Pay files listing the payments you issued, so your bank can reject anything you did not send.',
		guide: 'positive-pay-files'
	},
	'/bank-reconciliation': {
		summary: 'Import bank statements and confirm that every payment you made actually cleared the bank.',
		guide: 'reconcile-bank'
	},
	'/billing': {
		summary: 'Your organization\'s own FeohLedger subscription: plan, usage and billing.',
		guide: 'configure-organization'
	},
	'/assistant': {
		summary: 'Ask questions about your payables in plain language and get answers drawn from your own data.',
		guide: 'use-assistant'
	},
	'/cfo': {
		summary: 'Cash-flow analytics for finance leaders: forecast outflows, cash position and spend, with exports.',
		guide: 'reports-and-analytics'
	},
	'/cash-flow': {
		summary: 'Plan cash with the copilot: ask what-if questions, review a proposed payment plan and stage it as a draft run.',
		guide: 'use-assistant'
	},
	'/tax': {
		summary: 'Track 1099-reportable payments by vendor and tax year, and see which vendors cross the filing threshold.',
		guide: 'reports-and-analytics'
	},
	'/reports': {
		summary: 'Build, run, save and export your own reports across invoices, payments and vendors.',
		guide: 'reports-and-analytics'
	},
	'/workflows': {
		summary: 'Design the steps an invoice goes through, including who approves it and at what amounts.',
		guide: 'design-workflows'
	},
	'/experiments': {
		summary: 'Compare two workflow-rule setups side by side to see which approves faster with fewer exceptions.',
		guide: 'design-workflows'
	},
	'/adaptive': {
		summary: 'What your approval history says: approver patterns, unusual invoices, routing suggestions and the auto-approve threshold.',
		guide: 'design-workflows'
	},
	'/audit': {
		summary: 'Search the record of who did what and when, verify approvals and export audit evidence.',
		guide: 'audit-and-compliance'
	},
	'/admin/access-review': {
		summary: 'Review who holds elevated roles and flag accounts that have gone dormant, as a periodic access check.',
		guide: 'audit-and-compliance'
	},
	'/admin/retention': {
		summary: 'Set how long each kind of record is kept before it may be deleted.',
		guide: 'audit-and-compliance'
	},
	'/admin/privacy': {
		summary: 'Handle data-subject requests: export the personal data you hold on someone, or erase it.',
		guide: 'audit-and-compliance'
	},
	'/organization': {
		summary: 'Organization-wide settings: company details, invoice defaults, integrations, payments, security and sign-in.',
		guide: 'configure-organization'
	},
	'/admin?tab=users': {
		summary: 'Invite people, change their roles, and deactivate or sign out accounts.',
		guide: 'manage-users-roles'
	},
	'/admin?tab=roles': {
		summary: 'See the built-in roles and create custom roles that grant specific permissions.',
		guide: 'manage-users-roles'
	},
	'/admin/entities': {
		summary: 'Add and manage the legal entities or subsidiaries that invoices, vendors and payments belong to.',
		guide: 'manage-entities'
	},
	'/admin/partner': {
		summary: 'For partners and resellers: create and manage the client organizations you run.',
		guide: 'configure-organization'
	},
	'/admin/api-keys': {
		summary: 'Create and revoke API keys that let other systems use the FeohLedger API.',
		guide: 'api-and-webhooks'
	},
	'/admin/webhooks': {
		summary: 'Subscribe other systems to FeohLedger events and check or resend recent deliveries.',
		guide: 'api-and-webhooks'
	},
	'/admin/health': {
		summary: 'Check that background jobs are running: when each last ran, what happened and whether any has stalled.'
	},
	'/profile': {
		summary: 'Your own account: name, password, language, two-factor sign-in, passkeys, signed-in devices and notifications.',
		guide: 'secure-your-account'
	},
	'/notifications': {
		summary: 'All your notifications in one place. Open one to go to the record it is about, or mark them read.',
		guide: 'getting-around'
	}
};
