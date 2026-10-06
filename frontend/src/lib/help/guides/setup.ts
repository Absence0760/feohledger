// Setup & governance how-tos: securing your own account, people and roles,
// organization settings, the ERP connection, workflows, entities, the
// developer API + webhooks, and the audit / compliance surfaces.
// Authoring rules: lib/help/types.ts (schema) and lib/help/inline.ts (markup).

import type { Guide } from '../types.ts';

export const SETUP_GUIDES: Guide[] = [
	// -------------------------------------------------------------------------
	{
		id: 'secure-your-account',
		title: 'Secure your account',
		summary:
			'Set a strong password, add two-factor authentication or a passkey, sign out devices you no longer use, and pick your language.',
		kind: 'howto',
		route: '/profile',
		sections: [
			{
				heading: 'Find your security settings',
				blocks: [
					{
						type: 'p',
						text: 'Click your name at the bottom of the sidebar and choose {ui:shell.profileAndSecurity}. The page has a section list on the left: {ui:profile.account.heading}, {ui:profile.password.heading}, {ui:profile.mfa.heading}, {ui:profile.passkeys.heading}, {ui:profile.sessions.heading}, {ui:profile.notifications.heading} and {ui:profile.language.heading}.'
					},
					{
						type: 'p',
						text: 'You can change your own name here. Your email address and your roles are shown but only an administrator can change them.'
					}
				]
			},
			{
				heading: 'Change your password',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/profile|your profile]] and choose {ui:profile.password.heading}.',
							'Enter your {ui:profile.password.current}, then your {ui:profile.password.new} twice.',
							'Click {ui:profile.password.submit}.'
						]
					},
					{
						type: 'p',
						text: 'A password must be at least 12 characters and include an uppercase letter, a lowercase letter and a digit. Changing it signs you out on every other device, but keeps you signed in where you made the change. That is deliberate: people usually change a password because they think someone else has it.'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'If your organization signs in through single sign-on, you may have no password here at all, or one that is not used to sign in. Your identity provider controls your sign-in instead.'
					}
				]
			},
			{
				heading: 'Turn on two-factor authentication or add a passkey',
				blocks: [
					{
						type: 'p',
						text: 'A second factor means a stolen password alone cannot get into your account. You can use an authenticator app, a passkey (Touch ID, Face ID, Windows Hello or a hardware security key), or both.'
					},
					{
						type: 'steps',
						items: [
							'Choose {ui:profile.mfa.heading} and click {ui:profile.mfa.setUp}.',
							'Scan the QR code with your authenticator app. If you cannot scan, open {ui:profile.mfa.manualSecret} and type the secret in.',
							'Enter the 6-digit code the app shows and click {ui:profile.mfa.verifyAndEnable}.',
							'To add a passkey, choose {ui:profile.passkeys.heading}, give it a name if you like, and click {ui:profile.passkeys.add}. Your browser or device asks you to confirm.'
						]
					},
					{
						type: 'p',
						text: 'At sign-in you then confirm with your passkey or a code from your app. If you lose access to your authenticator, the sign-in screen can email a one-time code to your account address instead.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'If your organization requires two-factor authentication, you are asked to enroll at sign-in and the option to turn it off is not available. Organization-wide rules like this one are set by your administrator.'
					}
				]
			},
			{
				heading: 'Why the app asks you to confirm it is you',
				blocks: [
					{
						type: 'p',
						text: 'Some changes ask you to prove again that you are the account holder before they go through. This is called a [[step-up]]. It protects you if someone gets hold of a signed-in browser: they still cannot quietly swap your security settings.'
					},
					{
						type: 'list',
						items: [
							'Turning two-factor off, removing a passkey, or adding a passkey when you already have a second factor asks for your password, a current authenticator code, or one of your existing passkeys.',
							'A few especially sensitive actions, such as an administrator exporting a supplier’s full bank details, open a {ui:stepUpPrompt.title} dialog that accepts only an authenticator code or a passkey. A password alone is not enough there, so set up a second factor first if you will need them.'
						]
					}
				]
			},
			{
				heading: 'Sign out devices and choose your language',
				blocks: [
					{
						type: 'p',
						text: '{ui:profile.sessions.heading} lists every browser or app currently signed in to your account. If you see one you do not recognize, or you left yourself signed in on a device you no longer have, click {ui:profile.sessions.signOut} next to it. {ui:profile.sessions.signOutOthers} ends every session except the one you are using. A signed-out device stops working immediately.'
					},
					{
						type: 'p',
						text: 'Under {ui:profile.notifications.heading} you choose which events reach you in the app and by email. Under {ui:profile.language.heading}, pick a {ui:profile.language.label}. The language choice is saved on that device, so set it on each computer you use.'
					}
				]
			}
		],
		terms: ['step-up', 'mfa', 'role', 'organization'],
		related: ['getting-around', 'manage-users-roles', 'configure-organization']
	},

	// -------------------------------------------------------------------------
	{
		id: 'manage-users-roles',
		title: 'Manage users and roles',
		summary:
			'Invite people, give them the right roles, split sensitive duties with custom roles, and deactivate access when someone leaves.',
		kind: 'howto',
		roles: ['admin'],
		route: '/admin',
		sections: [
			{
				heading: 'Invite a user',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/admin?tab=users]] under {ui:nav.group.settings} and click {ui:admin.usersRoles.inviteUser}.',
							'Enter their {ui:admin.users.field.fullName} and {ui:admin.users.field.email}, and tick one or more roles.',
							'Click {ui:admin.users.modal.invite.create}. The app generates a temporary password.',
							'Click {ui:admin.users.modal.created.copy} and send the sign-in details to the person yourself. The temporary password is shown only once.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Administrators manage users. Your organization can also give the user-management permission to a custom role, which then sees the {ui:nav.users} tab too. Only administrators can create or edit roles.'
					}
				]
			},
			{
				heading: 'Know what each system role can do',
				blocks: [
					{
						type: 'p',
						text: 'There are four built-in [[role|roles]]. A person can hold more than one, and gets everything each of them allows.'
					},
					{
						type: 'diagram',
						id: 'roles-overview',
						caption:
							'What each built-in role is mainly for. The sensitive steps (approving, paying, changing bank details) sit with the roles that do not prepare the work, which is [[segregation-of-duties]] built into the defaults.'
					},
					{
						type: 'list',
						items: [
							'Admin: everything, including users, roles, organization settings, workflows and every sensitive permission.',
							'AP Manager: approves invoices, approves and executes payment runs, manages vendors, approves vendor bank changes and works the exception queue. Voiding a payment is not included by default.',
							'CFO: approves invoices, including those above the CFO thresholds, approves, executes and voids payments, and sees the finance dashboards and governance pages.',
							'AP Clerk: enters and prepares invoices (upload, create, import open invoices, correct and code them, submit for review) and sees the work around them. A clerk can change an invoice only until it is submitted, can never approve or pay, and holds none of the sensitive permissions.'
						]
					},
					{
						type: 'p',
						text: 'System roles cannot be edited or deleted. When you change someone’s roles, they are signed out everywhere so the new access takes effect at once.'
					}
				]
			},
			{
				heading: 'Split sensitive duties with a custom role',
				blocks: [
					{
						type: 'p',
						text: 'Some duties should never sit with one person. For example, the person who approves a change to a vendor’s bank account should not also be the person who sends the money. A custom role lets you grant just the [[permission|permissions]] you choose, so you can split those duties between people. This is [[segregation-of-duties]].'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/admin?tab=roles]] and click {ui:admin.usersRoles.createRole}.',
							'Give the role a {ui:admin.roles.field.name} and an optional {ui:admin.roles.field.description}.',
							'Tick the {ui:admin.roles.field.permissions} it grants: approve invoices, approve payment runs, execute payment runs, void payments, approve vendor bank or tax changes, block vendor payments, manage vendors, or manage users and roles.',
							'Click {ui:admin.roles.modal.create.create}, then assign the role to people on the {ui:nav.users} tab.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'A custom role with no permissions ticked grants no access. It is only a label. A role’s name cannot be changed later, but its description and permissions can.'
					}
				]
			},
			{
				heading: 'People who sign in through SSO',
				blocks: [
					{
						type: 'p',
						text: 'If your organization uses single sign-on, a person’s first sign-in through your identity provider creates their account automatically, as an AP Clerk. Give them more access here when they need it. You can limit which email domains may create accounts this way in the organization’s {ui:orgSso.title} settings. See [[guide:configure-organization|Configure your organization]].'
					},
					{
						type: 'p',
						text: 'If your identity provider also creates and deactivates users for you automatically, those changes show up in this list as well.'
					}
				]
			},
			{
				heading: 'When someone leaves or changes jobs',
				blocks: [
					{
						type: 'steps',
						items: [
							'Find the person on the {ui:nav.users} tab.',
							'Click {ui:admin.users.row.deactivate}, then {ui:admin.users.row.confirm}. They are signed out and can no longer sign in by any method, including SSO.',
							'If they only changed jobs, open their row and change their roles instead.'
						]
					},
					{
						type: 'p',
						text: 'Prefer deactivating to deleting. A deactivated account keeps its history in the [[audit-trail]] and can be reactivated later. {ui:admin.users.row.delete} is for accounts created by mistake, and it is refused while the person still has open invoices, pending approvals or active workflows.'
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Every quarter or so, admins and CFOs should check [[page:/admin/access-review]] for people whose elevated access has gone unused. See [[guide:audit-and-compliance|Audit and compliance]].'
					}
				]
			}
		],
		terms: ['role', 'permission', 'segregation-of-duties', 'access-review', 'audit-trail'],
		related: ['secure-your-account', 'segregation-of-duties', 'audit-and-compliance', 'start-admin']
	},

	// -------------------------------------------------------------------------
	{
		id: 'configure-organization',
		title: 'Configure your organization',
		summary:
			'Set your company profile, invoice defaults, branding, email intake, sign-in rules and data residency from one settings page.',
		kind: 'howto',
		roles: ['admin'],
		route: '/organization',
		sections: [
			{
				heading: 'Find your way around the settings page',
				blocks: [
					{
						type: 'p',
						text: 'Open [[page:/organization]] under {ui:nav.group.settings}. A section list down the left groups the settings: {ui:org.rail.group.company}, {ui:org.rail.group.integrations}, {ui:org.rail.group.money}, {ui:org.rail.group.compliance} and {ui:org.rail.group.account}. Each section saves on its own button.'
					},
					{
						type: 'p',
						text: 'New here? Start with {ui:org.gettingStarted.title}, which links to the settings most organizations configure first.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only administrators can change organization settings.'
					}
				]
			},
			{
				heading: 'Company profile, invoice defaults and branding',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:org.section.company}: your legal name, tax and registration numbers, address and contact details.',
							'{ui:org.section.defaults}: the {ui:org.defaults.currency}, {ui:org.defaults.paymentTerms}, {ui:org.defaults.numberPrefix}, {ui:org.defaults.defaultGl} and {ui:org.defaults.defaultCostCenter} applied to new invoices. Unless a separate reporting currency has been set up, this currency is also what dashboards total in.',
							'{ui:org.section.branding}: your own {ui:org.branding.productName}, logo and accent colors. The app rethemes as soon as you save, and generated PDFs and outbound emails carry your brand too.',
							'{ui:org.section.customDomains}: serve the app under your own web address. Your IT team handles the DNS and certificate. Registering the address here tells the platform which organization it belongs to.'
						]
					}
				]
			},
			{
				heading: 'Integrations: ERP, extraction and email intake',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:org.section.erp}: connect your accounting system. See [[guide:connect-erp|Connect your ERP]].',
							'{ui:org.section.extraction}: choose how invoice data is read from uploaded files, either the platform’s AI (charged per extraction) or your own provider key.',
							'{ui:org.section.emailIntake}: create your organization’s [[email-intake]] address. Vendors email invoices to it and each attachment becomes an invoice. Treat it like a password: if it leaks, click {ui:org.emailIntake.rotate}, and give vendors the new address, because mail to the old one is dropped.',
							'{ui:org.section.chat}: post approval events to a Slack or Microsoft Teams channel.'
						]
					}
				]
			},
			{
				heading: 'Money settings and the CFO sign-off threshold',
				blocks: [
					{
						type: 'p',
						text: 'Under {ui:org.rail.payments}, pick the payment provider that moves money when a payment run is executed, and set the CFO sign-off threshold. A [[payment-run]] above that total waits for someone with the CFO role to sign it off and cannot be executed until they do. That is the [[cfo-gate]]. {ui:org.section.cards} turns on virtual cards for invoice payments.'
					}
				]
			},
			{
				heading: 'Sign-in rules, fraud checks and data residency',
				blocks: [
					{
						type: 'list',
						items: [
							'{ui:org.section.security}: tick {ui:org.security.requireMfa}. People without a second factor are asked to set one up at their next sign-in.',
							'{ui:orgSso.title}: let people sign in through your identity provider, over OpenID Connect or SAML. Copy the values under {ui:orgSso.register.title} into your provider, limit which email domains may create accounts, and once sign-in works you can tick {ui:orgSso.ssoOnly}. Every change here is recorded in the audit trail.',
							'{ui:org.section.fraud}: switch individual fraud checks on or off and tune their limits, such as round amounts, rush payments, or a large invoice from a brand-new vendor.',
							'{ui:org.section.dataResidency}: record the region where your data must live. This records the requirement and shows whether the deployment already runs there. It does not move any data by itself.'
						]
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Requiring SSO closes password sign-in for everyone, so it only saves once your identity-provider settings are complete. Test a real SSO sign-in before you turn it on.'
					}
				]
			}
		],
		terms: ['organization', 'email-intake', 'cfo-gate', 'reporting-currency', 'payment-run', 'data-residency'],
		related: ['start-admin', 'connect-erp', 'manage-users-roles', 'fraud-controls']
	},

	// -------------------------------------------------------------------------
	{
		id: 'connect-erp',
		title: 'Connect your ERP',
		summary:
			'Link FeohLedger to your accounting system so approved invoices post there, payments sync back, and master data stays in step.',
		kind: 'howto',
		roles: ['admin'],
		route: '/organization',
		sections: [
			{
				heading: 'Set up the connection',
				blocks: [
					{
						type: 'steps',
						items: [
							'Open [[page:/organization]] and choose {ui:org.section.erp} in the section list.',
							'Pick your {ui:org.erp.system}, for example Microsoft Dynamics 365 Business Central, Oracle NetSuite or SAP S/4HANA.',
							'Choose the {ui:org.erp.method}. {ui:org.erp.methodMergeDev} works with every listed ERP. {ui:org.erp.methodDirect} is available for Business Central and NetSuite.',
							'Enter the credentials your ERP or Merge.dev account gives you.',
							'Click {ui:org.erp.save}, then {ui:org.common.testConnection} to check that the details work.'
						]
					},
					{
						type: 'note',
						tone: 'tip',
						text: 'Ask whoever administers your ERP for the credentials. They are stored with your organization’s settings and are never shown to other roles.'
					}
				]
			},
			{
				heading: 'How invoices reach the ERP',
				blocks: [
					{
						type: 'p',
						text: 'Once an invoice is {ui:invoices.status.approved}, the ERP Export step of your [[workflow]] sends it to the ERP. You decide in the workflow builder whether that happens automatically on approval, or only when someone clicks {ui:invoices.modal.submit.toErp} on the invoice. See [[guide:design-workflows|Design approval workflows]].'
					},
					{
						type: 'p',
						text: 'The invoice then moves through {ui:invoices.status.sendingToErp}, {ui:invoices.status.sentToErp} and {ui:invoices.status.postedInErp} as the ERP accepts and posts it. Once a payment for it settles, the ERP is told and the invoice moves to {ui:invoices.status.paid}.'
					}
				]
			},
			{
				heading: 'When a send fails',
				blocks: [
					{
						type: 'p',
						text: 'Temporary problems such as a timeout are retried automatically. If the ERP still refuses, the invoice shows {ui:invoices.status.failed}. Fix the cause, often expired credentials or a vendor or GL code the ERP does not recognize, then open the invoice and click {ui:invoices.modal.erp.retrySend}.'
					},
					{
						type: 'p',
						text: 'If paid invoices are stuck at {ui:invoices.status.paymentScheduled} because the payment sync-back did not reach the ERP, someone who can execute payments can use {ui:payments.runs.erpSync} on the payment run. It moves no money and is safe to run again.'
					}
				]
			},
			{
				heading: 'Keep master data in step',
				blocks: [
					{
						type: 'p',
						text: 'Vendors, purchase orders and the chart of accounts can be pulled from the ERP. Each page has its own {ui:vendors.action.syncErp} button: [[page:/vendors]], [[page:/purchase-orders]] and [[page:/gl-accounts]]. The {ui:org.section.dataSync} section of the organization settings links to all three.'
					}
				]
			}
		],
		terms: ['erp-sync', 'gl-coding', 'purchase-order', 'vendor', 'workflow'],
		related: ['configure-organization', 'design-workflows', 'invoice-lifecycle', 'run-payments']
	},

	// -------------------------------------------------------------------------
	{
		id: 'design-workflows',
		title: 'Design approval workflows',
		summary:
			'Build the steps every invoice goes through, set who approves and at what amounts, and test changes safely before they go live.',
		kind: 'howto',
		roles: ['admin'],
		route: '/workflows',
		sections: [
			{
				heading: 'Create or edit a workflow',
				blocks: [
					{
						type: 'p',
						text: 'A [[workflow]] is the series of steps an invoice follows from capture to your ERP. Open [[page:/workflows]] under {ui:nav.group.automation}.'
					},
					{
						type: 'steps',
						items: [
							'Click {ui:workflows.list.newWorkflow}, or {ui:workflows.list.newFromTemplate} to start from a ready-made design.',
							'Open the workflow. Drag steps from the {ui:workflows.builder.palette.title} onto the canvas, or click one to add it at the end.',
							'Select a step to configure it. The usual backbone is {ui:workflows.list.step.extraction}, {ui:workflows.list.step.approval} and {ui:workflows.list.step.erpExport}. You can add conditions that branch on invoice fields, parallel approvals, emails, webhooks and delays.',
							'Click {ui:common.save}. The toggle beside it switches the workflow between {ui:workflows.builder.status.active} and {ui:workflows.builder.status.inactive}.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only administrators can build and change workflows.'
					}
				]
			},
			{
				heading: 'Set who approves and at what amounts',
				blocks: [
					{
						type: 'p',
						text: 'In an {ui:workflows.list.step.approval} step, choose an {ui:workflows.builder.approval.approverAssignment}: manual assignment per invoice, always the same approver, a multi-level chain, or automatic. A chain lets you define approval levels by amount band and by department, GL account or vendor, with several approvers per level and [[escalation]] to other people if a level waits too long. That is an [[approval-chain]].'
					},
					{
						type: 'list',
						items: [
							'An auto-approve amount: invoices under it skip approval.',
							'A CFO approval amount: invoices over it need someone with the CFO role.',
							'A maximum invoice amount: invoices over it are rejected automatically.',
							'{ui:workflows.builder.approval.requireSegregation}: the person who uploaded an invoice cannot approve it.'
						]
					},
					{
						type: 'p',
						text: 'Every [[approval-threshold]] compares the invoice amount converted to your organization’s reporting currency.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Leave segregation of duties on unless you have a deliberate reason. It is the classic control against someone approving their own invoice, and auditors look for it. See [[guide:segregation-of-duties|Segregation of duties]].'
					}
				]
			},
			{
				heading: 'Changes never touch invoices already in flight',
				blocks: [
					{
						type: 'p',
						text: 'When an invoice is created, it takes a copy of the workflow as it stands at that moment. Editing a workflow later only affects new invoices. Anything already moving finishes on the rules it started with, so nobody can change an invoice’s approval path halfway through.'
					},
					{
						type: 'list',
						items: [
							'{ui:workflows.list.action.simulate} runs a sample invoice through the workflow so you can check the route before you save it.',
							'{ui:workflows.list.action.versions} shows every saved version, compares two of them, and can restore an earlier one.',
							'A workflow cannot be deleted while it is active, while it is the default, or while invoices are still running through it.'
						]
					}
				]
			},
			{
				heading: 'Workflows for each entity',
				blocks: [
					{
						type: 'p',
						text: 'If your organization has more than one [[entity]], each can have its own workflow. A workflow you create belongs to the entity selected in the sidebar switcher. An invoice uses its own entity’s active workflow, and falls back to a shared one if the entity has none. Activating a workflow only switches off the others in the same entity.'
					}
				]
			},
			{
				heading: 'Experiments and adaptive suggestions',
				blocks: [
					{
						type: 'p',
						text: '[[page:/experiments]] runs an A/B test between two versions of a workflow’s rules and compares time to approval, touchless rate, exception rate and rejection rate. Experiments only change routing. They never move money.'
					},
					{
						type: 'p',
						text: '[[page:/adaptive]] learns from your approval history. It flags unusual invoices, proposes routing and workflow changes, and can recommend a higher auto-approve amount for vendors with a clean record. Nothing changes until someone applies a suggestion. Raising the threshold is limited to administrators and, like any edit, affects new invoices only.'
					}
				]
			}
		],
		terms: ['workflow', 'approval-chain', 'approval-threshold', 'cfo-gate', 'escalation', 'adaptive-workflow', 'workflow-experiment'],
		related: ['approve-invoices', 'segregation-of-duties', 'invoice-lifecycle', 'manage-entities']
	},

	// -------------------------------------------------------------------------
	{
		id: 'manage-entities',
		title: 'Set up legal entities',
		summary:
			'Run several subsidiaries in one workspace: create entities, switch between them, route inter-company invoices and report across them.',
		kind: 'howto',
		roles: ['admin'],
		route: '/admin/entities',
		sections: [
			{
				heading: 'Create an entity',
				blocks: [
					{
						type: 'p',
						text: 'An [[entity]] is a legal entity or subsidiary inside your organization. Every organization starts with one default entity. Add more when you need separate books for each company.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/admin/entities]] under {ui:nav.group.settings} and click {ui:admin.entities.createEntity}.',
							'Enter a {ui:admin.entities.field.name} and a {ui:admin.entities.field.slug}. The slug is permanent.',
							'Optionally set a {ui:admin.entities.field.currency}. Leave it blank to use the organization’s reporting currency.',
							'Click {ui:admin.entities.create.create}.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only administrators can create and edit entities.'
					}
				]
			},
			{
				heading: 'Work inside one entity',
				blocks: [
					{
						type: 'p',
						text: 'Once there are two or more entities, an entity switcher appears in the sidebar. Choose an entity to see only its invoices, vendors, payments and dashboards. Choose {ui:entity.all} to see everything at once.'
					},
					{
						type: 'p',
						text: 'Nothing moves when you create an entity. Only invoices, vendors and payments created while that entity is selected land in it. The chart of accounts can hold codes shared by every entity alongside codes that belong to one, and [[page:/gl-accounts]] shows which is which.'
					},
					{
						type: 'note',
						tone: 'caution',
						text: 'Check the switcher before you create something. A new record goes to whichever entity is selected.'
					}
				]
			},
			{
				heading: 'Change the default or retire an entity',
				blocks: [
					{
						type: 'p',
						text: 'The default entity is the home for records that arrive without one, such as invoices sent to your email intake address. To change it, click {ui:admin.entities.row.makeDefault} on another entity’s row, then {ui:admin.entities.row.confirm}. To retire an entity, edit it and clear {ui:admin.entities.field.active}. The default entity cannot be deactivated.'
					}
				]
			},
			{
				heading: 'Inter-company invoices and consolidated reporting',
				blocks: [
					{
						type: 'p',
						text: 'When one of your entities bills another, open the invoice and use {ui:invoices.modal.intercompany.route} to create the matching payable under the other entity, so both sets of books reflect it. This is an [[intercompany]] invoice. It moves no money, and anyone involved in the original invoice cannot approve its mirror.'
					},
					{
						type: 'p',
						text: 'Admins and CFOs see a {ui:byEntity.heading} breakdown on [[page:/cfo]], with spend and outstanding amounts for each entity in the reporting currency.'
					}
				]
			}
		],
		terms: ['entity', 'intercompany', 'reporting-currency', 'gl-coding'],
		related: ['currencies-and-entities', 'design-workflows', 'configure-organization', 'reports-and-analytics']
	},

	// -------------------------------------------------------------------------
	{
		id: 'api-and-webhooks',
		title: 'Connect other systems with API keys and webhooks',
		summary:
			'Give a script or integration read access with an API key, and push invoice, payment and exception events to your own systems with webhooks.',
		kind: 'howto',
		roles: ['admin'],
		route: '/admin/api-keys',
		sections: [
			{
				heading: 'Create an API key',
				blocks: [
					{
						type: 'p',
						text: 'An [[api-key]] lets another system read your data through the Developer API without anyone signing in. Each key belongs to your organization and has read access only.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/admin/api-keys]] under {ui:nav.group.settings} and click {ui:admin.apiKeys.createKey}.',
							'Give the key a name that says what uses it, such as the reporting tool, and click {ui:admin.apiKeys.create.create}.',
							'Copy the key straight away and store it somewhere safe. It is shown only once.'
						]
					},
					{
						type: 'p',
						text: 'Click a key’s name to see how much it has been used. If a key leaks or is no longer needed, click {ui:admin.apiKeys.row.revoke}. It stops working at once.'
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Only administrators can create and revoke API keys and webhooks.'
					}
				]
			},
			{
				heading: 'Send events to your systems with a webhook',
				blocks: [
					{
						type: 'p',
						text: 'A [[webhook]] posts a message to a web address you choose whenever an event happens: an invoice is approved, a payment settles, or an exception is raised.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/admin/webhooks]] and click {ui:admin.webhooks.createWebhook}.',
							'Enter a {ui:admin.webhooks.field.name}, the {ui:admin.webhooks.field.targetUrl} and the {ui:admin.webhooks.field.events} to send.',
							'Click {ui:admin.webhooks.create.create} and copy the signing secret. It is shown only once. Your receiving system uses it to check each message really came from FeohLedger.'
						]
					}
				]
			},
			{
				heading: 'Watch deliveries and rotate secrets',
				blocks: [
					{
						type: 'p',
						text: '{ui:admin.webhooks.deliveries} lists every attempt, filtered by {ui:admin.webhooks.filter.pending}, {ui:admin.webhooks.filter.delivered}, {ui:admin.webhooks.filter.failed} or {ui:admin.webhooks.filter.dead}. Once your endpoint is fixed, click {ui:admin.webhooks.row.redeliver} on a failed delivery to send it again.'
					},
					{
						type: 'p',
						text: 'To replace a signing secret, click {ui:admin.webhooks.row.rotate}. Choose an overlap window so the old secret keeps working while your team installs the new one. If the secret has been exposed, choose to cut over immediately instead.'
					}
				]
			}
		],
		terms: ['api-key', 'webhook', 'exception', 'settlement'],
		related: ['configure-organization', 'connect-erp', 'start-admin']
	},

	// -------------------------------------------------------------------------
	{
		id: 'audit-and-compliance',
		title: 'Audit trail and compliance',
		summary:
			'Pull audit evidence, verify approval signatures, review who holds elevated access, set retention windows and answer data-subject requests.',
		kind: 'howto',
		roles: ['admin', 'cfo'],
		route: '/audit',
		sections: [
			{
				heading: 'Search and export the audit trail',
				blocks: [
					{
						type: 'p',
						text: 'The [[audit-trail]] records who did what and when: every status change, approval, payment and settings change. Entries can be added but never edited or deleted, so it is reliable evidence for auditors.'
					},
					{
						type: 'steps',
						items: [
							'Open [[page:/audit]] under {ui:nav.group.governance}.',
							'Choose {ui:audit.mode.dateRange} and set {ui:audit.field.from} and {ui:audit.field.to}, optionally narrowing {ui:audit.field.entity} to one kind of record such as invoice, payment or vendor. Or choose {ui:audit.mode.byInvoice} to see one invoice’s full history.',
							'Click {ui:audit.runQuery}.',
							'Click {ui:audit.downloadCsv} to save exactly what is on screen for your auditors.'
						]
					},
					{
						type: 'note',
						tone: 'role',
						text: 'Admins and CFOs can open the audit trail and the access review. Retention and privacy requests are for administrators only.'
					}
				]
			},
			{
				heading: 'Verify approval signatures',
				blocks: [
					{
						type: 'p',
						text: 'Each approval is signed when it is given. Under {ui:audit.verify.title}, pick a period and click {ui:audit.verify.run} to check every approval still matches its signature. {ui:audit.verify.verdict.invalid} means the amount, approver or time changed after the fact, so investigate that row. {ui:audit.verify.verdict.unsigned} means there was nothing to check, which is not by itself a sign of tampering.'
					}
				]
			},
			{
				heading: 'Run the periodic access review',
				blocks: [
					{
						type: 'p',
						text: '[[page:/admin/access-review]] lists everyone holding an elevated role or a sensitive permission, and highlights those who have not made a change with it recently. Only changes count. Viewing records does not. This is the [[access-review]] SOX auditors expect.'
					},
					{
						type: 'steps',
						items: [
							'Go through the list, starting with the people marked dormant.',
							'Remove access nobody needs on [[page:/admin?tab=users]].',
							'Click **Acknowledge review** to record that the review was done for this period.'
						]
					}
				]
			},
			{
				heading: 'Set retention and handle privacy requests',
				blocks: [
					{
						type: 'p',
						text: '[[page:/admin/retention]] sets, in months, how long finished invoices and audit-log entries are kept under your [[retention-policy]]. Past the window, finished invoices are archived rather than deleted, and audit-log entries are never deleted at all.'
					},
					{
						type: 'p',
						text: '[[page:/admin/privacy]] handles [[dsar|data-subject requests]] for a person on your team, a supplier-portal login, or a vendor contact.'
					},
					{
						type: 'list',
						items: [
							'**Export data (DSAR)** gathers everything held about that person into one bundle. Bank details are masked unless you tick {ui:privacyDsar.includeBanking}, which needs a written reason, permission to approve vendor bank changes, and an authenticator code or passkey.',
							'**Erase data…** permanently redacts the person’s personal details. Invoice and payment amounts and the audit trail are kept, so your financial records stay complete. Erasure cannot be undone.'
						]
					}
				]
			}
		],
		terms: ['audit-trail', 'access-review', 'retention-policy', 'dsar', 'step-up', 'segregation-of-duties', 'data-residency'],
		related: ['manage-users-roles', 'segregation-of-duties', 'secure-your-account', 'start-cfo']
	}
];
