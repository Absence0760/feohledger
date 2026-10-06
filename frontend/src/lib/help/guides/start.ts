import type { Guide } from '../types.ts';

export const START_GUIDES: Guide[] = [
	{
		id: 'getting-around',
		title: 'Getting around FeohLedger',
		summary:
			'Find your way around the sidebar, the entity switcher, notifications and your profile menu, and learn which links you can bookmark or share.',
		kind: 'start',
		route: '/',
		sections: [
			{
				heading: 'Use the sidebar',
				blocks: [
					{
						type: 'p',
						text: 'The sidebar on the left is how you move between pages. It only shows what your role can open, so two colleagues can see different sidebars. That is deliberate: a page you cannot use is hidden rather than left as a dead end.'
					},
					{
						type: 'diagram',
						id: 'roles-overview',
						caption:
							'The four built-in [[role|roles]]. Your sidebar shows the pages your roles can open, so it follows from which of these you hold.'
					},
					{
						type: 'list',
						items: [
							'The busiest pages have their own row: [[page:/]], [[page:/invoices]], [[page:/payments]], [[page:/vendors]], [[page:/vendors/screening]], [[page:/vendors/change-requests]] and [[page:/exceptions]].',
							'Everything else is folded into a group row: {ui:nav.group.procurement}, {ui:nav.group.billing}, {ui:nav.group.insights}, {ui:nav.group.automation}, {ui:nav.group.governance} and {ui:nav.group.settings}. Clicking a group opens its first page you can see.',
							'Inside a group, the pages appear as section tabs across the top of the page. When the tabs do not fit, the rest sit behind {ui:shell.sectionMore}.',
							'Click {ui:shell.collapse} at the bottom of the sidebar to shrink it to icons and give the page more room. Hover an icon to see its name; click the arrow again to expand it.'
						]
					}
				]
			},
			{
				heading: 'Pick an entity, if your organization has several',
				blocks: [
					{
						type: 'p',
						text: 'If your organization runs more than one legal [[entity]] (for example, subsidiaries in different countries), an entity switcher appears under the logo. With a single entity it stays hidden, because there is nothing to choose.'
					},
					{
						type: 'list',
						items: [
							'{ui:entity.all} shows the consolidated view across every entity.',
							'Choosing one entity reloads the page and narrows lists, totals and reports to that entity. New records you create are filed under it.',
							'Your choice is remembered in this browser, so check the switcher if numbers look smaller than you expect.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Only an admin can add or retire entities, on [[page:/admin/entities]]. A retired entity disappears from the switcher.'
					}
				]
			},
			{
				heading: 'Keep up with notifications',
				blocks: [
					{
						type: 'p',
						text: 'The bell next to the logo shows a badge with your unread count. Click it to see your six most recent notifications.'
					},
					{
						type: 'list',
						items: [
							'Click a notification about an invoice to open that invoice directly.',
							'{ui:notifications.markAllRead} clears the badge.',
							'**View all** opens the full [[page:/notifications|notifications page]], where you can page back through older items.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'You choose which events reach you, in the app and by email, under {ui:profile.notifications.heading} on [[page:/profile|Profile & Security]].'
					}
				]
			},
			{
				heading: 'Your profile menu',
				blocks: [
					{
						type: 'p',
						text: 'Your name sits at the bottom of the sidebar. Click it to open the profile menu.'
					},
					{
						type: 'list',
						items: [
							'{ui:shell.profileAndSecurity} is where you change your name, password and display {ui:profile.language.heading}, set up {ui:profile.mfa.heading} and {ui:profile.passkeys.heading}, see your {ui:profile.sessions.heading}, and choose your notifications. See [[guide:secure-your-account|Secure your account]].',
							'{ui:shell.legalAndPrivacy} opens the published privacy policy, terms and related documents.',
							'**Help** opens this help centre.',
							'{ui:shell.logOut} ends your session on this device.'
						]
					}
				]
			},
			{
				heading: 'Bookmark and share links, and work on the go',
				blocks: [
					{
						type: 'p',
						text: 'Most lists keep their filters, sort order and selected record in the page address. Bookmark a filtered view you use every day, or paste the link to a colleague so they land on exactly the same invoice or list. They still only see what their own role allows.'
					},
					{
						type: 'p',
						text: 'The FeohLedger mobile app is a focused subset for work away from your desk: approving invoices, checking invoices and payments, working exceptions and reading notifications. Setup, configuration and most reporting stay on the web.'
					}
				]
			}
		],
		terms: ['entity', 'role', 'organization', 'permission', 'mfa'],
		related: ['secure-your-account', 'start-ap-clerk', 'start-ap-manager', 'start-cfo']
	},
	{
		id: 'start-ap-clerk',
		title: 'Your first week as an AP clerk',
		summary:
			'What an AP clerk does in FeohLedger, which pages you will use, and what is reserved for other roles and why.',
		kind: 'start',
		roles: ['ap_clerk'],
		route: '/invoices',
		sections: [
			{
				heading: 'What your role covers',
				blocks: [
					{
						type: 'p',
						text: 'As an AP clerk you have a look-up-and-prepare role. You can see every [[invoice]] and the documents around it, track where each one is in its lifecycle, prepare purchasing requests and expenses, and answer questions from suppliers. Creating, editing, approving and paying invoices belongs to AP managers, CFOs and admins.'
					},
					{
						type: 'p',
						text: 'The pages you will use most are [[page:/invoices]], [[page:/purchase-orders]], [[page:/goods-receipts]], [[page:/gl-accounts]], [[page:/requisitions]], [[page:/expenses]] and the [[page:/assistant]].'
					}
				]
			},
			{
				heading: 'A path through your first week',
				blocks: [
					{
						type: 'steps',
						items: [
							'Read [[guide:getting-around|Getting around FeohLedger]] and set up two-factor sign-in with [[guide:secure-your-account|Secure your account]].',
							'Learn the stages an invoice moves through in [[guide:invoice-lifecycle|The invoice lifecycle]], so a status like {ui:invoices.status.readyForReview} or {ui:invoices.status.approved} tells you who acts next.',
							'Open [[page:/invoices]] and practise finding an invoice by vendor, status or amount. Save the views you use as bookmarks.',
							'When an invoice is held on a match, look up its order and delivery on [[page:/purchase-orders]] and [[page:/goods-receipts]]. [[guide:how-matching-works|How matching works]] explains what is being compared.',
							'Look up GL codes on [[page:/gl-accounts]] when someone asks how a cost should be coded.',
							'Raise a purchase request with [[guide:raise-requisition|Raise a requisition]], and claim your own spending with [[guide:submit-expenses|Submit expenses]].',
							'Try the [[page:/assistant]] for quick questions such as which invoices are due this week. See [[guide:use-assistant|Use the AI assistant]].'
						]
					}
				]
			},
			{
				heading: 'What you can see but not change',
				blocks: [
					{
						type: 'p',
						text: 'Several pages open for you in read-only form, because seeing the data helps you answer questions while changing it is a controlled step: [[page:/purchase-orders]], [[page:/gl-accounts]], [[page:/credit-memos]], [[page:/recurring]], [[page:/vendor-statements]], [[page:/bank-reconciliation]] and the [[page:/vendors/screening]] queue.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'You cannot approve invoices, create payment runs or change vendor bank details, and [[page:/payments]], [[page:/vendors]] and [[page:/exceptions]] do not appear in your sidebar. This is [[segregation-of-duties]]: the people who prepare and look up AP work are kept apart from the people who sign it off and send money, so no single person can push a payment through alone.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'If your job needs one of these abilities, ask your admin. They can give you a different role, or a custom role that grants one specific [[permission]].'
					}
				]
			}
		],
		terms: ['invoice', 'purchase-order', 'gl-coding', 'requisition', 'segregation-of-duties'],
		related: ['getting-around', 'invoice-lifecycle', 'raise-requisition', 'submit-expenses']
	},
	{
		id: 'start-ap-manager',
		title: 'Your first week as an AP manager',
		summary:
			'What an AP manager owns in FeohLedger, the pages you will live in, and a path through capturing, approving and paying invoices.',
		kind: 'start',
		roles: ['ap_manager'],
		route: '/invoices',
		sections: [
			{
				heading: 'What your role covers',
				blocks: [
					{
						type: 'p',
						text: 'As an AP manager you run the payables process day to day. You bring invoices in, review and approve them, clear the [[exception|exceptions]] that hold them up, look after vendors, and prepare [[payment-run|payment runs]].'
					},
					{
						type: 'p',
						text: 'You will live in [[page:/invoices]], [[page:/exceptions]], [[page:/payments]] and [[page:/vendors]], with [[page:/vendors/change-requests]], [[page:/credit-memos]] and [[page:/vendor-statements]] close behind.'
					}
				]
			},
			{
				heading: 'A path through your first week',
				blocks: [
					{
						type: 'steps',
						items: [
							'Read [[guide:getting-around|Getting around FeohLedger]] and [[guide:invoice-lifecycle|The invoice lifecycle]].',
							'Bring invoices in by uploading, creating or importing them: [[guide:capture-invoices|Capture invoices]].',
							'Check what was read from each document and correct it before it moves on: [[guide:review-invoice|Review an invoice]].',
							'Turn on {ui:invoices.filter.myApprovals} on [[page:/invoices]] to see the invoices waiting on you, then work through [[guide:approve-invoices|Approve invoices]].',
							'Clear what is holding invoices back on [[page:/exceptions]]: [[guide:work-exceptions|Work exceptions]].',
							'Select approved invoices on the {ui:payments.tab.queue} tab of [[page:/payments]] and create a draft run: [[guide:run-payments|Run payments]].',
							'Keep supplier records clean, and route any bank-detail change through a second approver: [[guide:add-vendor|Add a vendor]] and [[guide:change-vendor-bank-details|Change vendor bank details]].'
						]
					}
				]
			},
			{
				heading: 'Controls that will stop you, and why',
				blocks: [
					{
						type: 'note',
						tone: 'caution',
						text: 'If your organization requires [[segregation-of-duties]], you cannot approve an invoice you uploaded or helped shape, and you cannot clear a payment-blocking exception on an invoice you were involved in. A second person has to look, which is what stops one person creating and approving a fake invoice.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'By default, whoever creates a payment run cannot also execute it, and a run above your organization\'s CFO sign-off threshold waits for a CFO. You cannot approve a vendor bank-detail change you requested. Each of these keeps a second human between a request and money leaving the bank.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Voiding a payment is reserved for CFOs and admins, and invoices above your [[approval-threshold]] may need a CFO\'s approval. Workflows, users and organization settings belong to the admin.'
					}
				]
			}
		],
		terms: ['approval-chain', 'exception', 'payment-run', 'bank-change-request', 'segregation-of-duties', 'cfo-gate'],
		related: ['approve-invoices', 'work-exceptions', 'run-payments', 'segregation-of-duties']
	},
	{
		id: 'start-cfo',
		title: 'Your first week as a CFO',
		summary:
			'What a CFO signs off in FeohLedger, where to watch cash and spend, and the controls that keep your approval meaningful.',
		kind: 'start',
		roles: ['cfo'],
		route: '/cfo',
		sections: [
			{
				heading: 'What your role covers',
				blocks: [
					{
						type: 'p',
						text: 'As CFO you are the senior sign-off. You approve high-value invoices and payment runs that cross your organization\'s thresholds, can void a payment when something has gone wrong, own budgets, and watch cash, spend and liabilities.'
					},
					{
						type: 'p',
						text: 'You will spend most of your time in [[page:/]], [[page:/cfo]], [[page:/payments]], [[page:/budgets]], [[page:/reports]] and the [[page:/audit]].'
					}
				]
			},
			{
				heading: 'A path through your first week',
				blocks: [
					{
						type: 'steps',
						items: [
							'Read [[guide:getting-around|Getting around FeohLedger]] and set up two-factor sign-in with [[guide:secure-your-account|Secure your account]].',
							'Read [[guide:segregation-of-duties|Segregation of duties]] to see which sign-offs land with you and why.',
							'Approve the invoices that need you on [[page:/invoices]]. If your workflow sets a CFO approval amount, invoices above it can only be approved by someone with the CFO role. {ui:invoices.filter.myApprovals} narrows the list to invoices assigned to you. See [[guide:approve-invoices|Approve invoices]].',
							'On the {ui:payments.tab.runs} tab of [[page:/payments]], open any run marked {ui:paymentRuns.runDetail.awaitingCfo} and use {ui:paymentRuns.runDetail.approveAsCfo}. Approving moves no money; execution is a separate step. See [[guide:run-payments|Run payments]].',
							'Learn when and how to reverse a payment: [[guide:void-payment|Void a payment]].',
							'Review cash, spend and aging on [[page:/cfo]] and build the views you need: [[guide:reports-and-analytics|Reports and analytics]].',
							'Set spending limits on [[page:/budgets]]: [[guide:manage-budgets|Manage budgets]].',
							'Use [[page:/audit]] and [[page:/admin/access-review]] for audit evidence: [[guide:audit-and-compliance|Audit and compliance]].'
						]
					}
				]
			},
			{
				heading: 'Controls that apply to you too',
				blocks: [
					{
						type: 'note',
						tone: 'caution',
						text: 'You cannot CFO-approve or execute a payment run you created yourself, and segregation rules on invoices apply to you as to anyone else. A sign-off you can give yourself is not a sign-off.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Exceptions, vendor bank-detail approvals and vendor maintenance are handled by AP managers and admins, and [[page:/exceptions]] and [[page:/vendors/change-requests]] do not appear in your sidebar. Workflows, users and organization settings belong to the admin.'
					}
				]
			}
		],
		terms: ['cfo-gate', 'approval-threshold', 'payment-run', 'void', 'budget', 'audit-trail', 'cash-position', 'days-payable-outstanding'],
		related: ['approve-invoices', 'run-payments', 'void-payment', 'reports-and-analytics']
	},
	{
		id: 'start-admin',
		title: 'Setting up your organization',
		summary:
			'Set up a new FeohLedger organization in a sensible order, from company details and entities to users, sign-in security, ERP, workflows and vendors.',
		kind: 'start',
		roles: ['admin'],
		route: '/organization',
		sections: [
			{
				heading: 'What your role covers',
				blocks: [
					{
						type: 'p',
						text: 'As an admin you configure the [[organization]] for everyone else: who it is, who works in it, how invoices are approved and which systems it connects to. Admins can also do the day-to-day AP work, but the setup below comes first.'
					},
					{
						type: 'p',
						text: 'Most settings live under {ui:nav.group.settings} in the sidebar. [[page:/organization]] opens on {ui:org.gettingStarted.title}, which links to the panels most organizations fill in first.'
					}
				]
			},
			{
				heading: 'Set up the basics',
				blocks: [
					{
						type: 'steps',
						items: [
							'On [[page:/organization]], fill in {ui:org.section.company} and {ui:org.section.defaults} (currency, payment terms, default GL account). Add {ui:org.section.branding} if you want your own look. See [[guide:configure-organization|Configure your organization]].',
							'If you run more than one legal entity, add them on [[page:/admin/entities]] with {ui:admin.entities.createEntity}. The entity switcher appears for everyone once there are two. See [[guide:manage-entities|Manage entities]].',
							'Invite your team on [[page:/admin?tab=users]] with {ui:admin.usersRoles.inviteUser}, and if you need to split sensitive duties, define custom roles on [[page:/admin?tab=roles]]. See [[guide:manage-users-roles|Manage users and roles]].',
							'Under {ui:org.section.security}, turn on {ui:org.security.requireMfa}. If you sign in through an identity provider, configure {ui:orgSso.title} too.'
						]
					}
				]
			},
			{
				heading: 'Connect your systems',
				blocks: [
					{
						type: 'steps',
						items: [
							'Connect your accounting system under {ui:org.section.erp}: [[guide:connect-erp|Connect your ERP]].',
							'Bring in reference data: use {ui:glAccounts.action.syncErp} on [[page:/gl-accounts]], then load suppliers on [[page:/vendors]] with {ui:vendors.action.syncErp} or {ui:vendors.action.importCsv}. See [[guide:add-vendor|Add a vendor]].',
							'Choose how payments go out under {ui:org.rail.payments}, including the CFO sign-off threshold for large payment runs.',
							'Optionally set up {ui:org.section.emailIntake} so suppliers can email invoices straight in, and review {ui:org.section.extraction} and {ui:org.section.fraud}.'
						]
					}
				]
			},
			{
				heading: 'Decide how invoices get approved',
				blocks: [
					{
						type: 'steps',
						items: [
							'On [[page:/workflows]], start from {ui:workflows.list.newFromTemplate} or {ui:workflows.list.newWorkflow}.',
							'In the approval step, set who approves and the {ui:workflows.builder.approval.thresholdsTitle}: an auto-approve amount, the amount above which a CFO must approve, and a maximum invoice amount.',
							'Leave {ui:workflows.builder.approval.requireSegregation} on unless you have a deliberate reason not to.',
							'See [[guide:design-workflows|Design workflows]] for the full builder.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Editing a workflow does not change invoices already in progress. Each invoice keeps the workflow it started with, so a rule change cannot rewrite an approval that is already under way.'
					}
				]
			},
			{
				heading: 'Keep it healthy',
				blocks: [
					{
						type: 'list',
						items: [
							'Set record retention and handle data-subject requests under {ui:nav.group.governance}: [[guide:audit-and-compliance|Audit and compliance]].',
							'Issue API keys and webhooks only when an integration needs them: [[guide:api-and-webhooks|API keys and webhooks]].',
							'Check [[page:/admin/health]] now and then to confirm background jobs are running.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Most of these pages are admin-only. Even as an admin you cannot approve an invoice you uploaded or execute a payment run you created when segregation of duties is on.'
					}
				]
			}
		],
		terms: ['organization', 'entity', 'role', 'permission', 'workflow', 'erp-sync'],
		related: ['configure-organization', 'manage-users-roles', 'connect-erp', 'design-workflows']
	}
];
