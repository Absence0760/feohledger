// English marketing slice: the apex landing page (`Landing`, `HeroPipeline`,
// `AdapterRail`) and the apex "find your workspace" page. Only the platform
// apex renders these, so they ship as their own chunk per locale rather than
// in the catalogue every signed-in page downloads (decisions §261, §300). The
// pricing grid's `marketing.pricing.*` keys stay in the main catalogue.
// Lazy in English too: `Landing` awaits the slice before it draws, and
// /find-workspace's `+layout.ts` loads it before the page renders.
//
// Every figure in this copy is substantiated in docs/marketing-substantiation.md
// — change a number here and its row there in the same commit. Provider names
// are proper nouns and stay as written in every locale, one name per provider.
export const enMarketing = {
	// ── Header (Landing.svelte) ─────────────────────────────────────────
	'marketing.nav.label': 'Main',
	'marketing.nav.features': 'Features',
	'marketing.nav.how': 'How it works',
	'marketing.nav.pricing': 'Pricing',
	'marketing.nav.signIn': 'Sign in',
	'marketing.nav.menu': 'Menu',
	// The one signup label, everywhere on the page; `ctaShort` is its narrow-
	// header form (issue #635 finding 29).
	'marketing.cta.create': 'Create workspace',
	'marketing.cta.short': 'Start free',

	// ── Hero ────────────────────────────────────────────────────────────
	'marketing.hero.eyebrow': 'Accounts payable, automated',
	'marketing.hero.title': 'AP automation your finance team',
	'marketing.hero.titleAccent': 'will actually use.',
	'marketing.hero.lede':
		'From invoice to payment in minutes, not days. AI reads your invoices, approvals follow your rules, and approved bills post to your ERP — with a typical 1–2% rebate on payments to vendors that accept card.',
	'marketing.hero.secondary': 'See how it works',
	'marketing.hero.subNote': 'Free to start · No credit card · Self-service signup',

	// ── Hero illustration (HeroPipeline.svelte; aria-hidden, so read by no
	//    screen reader — but seen in the visitor's language) ─────────────
	'marketing.demo.status': 'Ready for review',
	'marketing.demo.matched': '3-way matched',
	'marketing.demo.vendor': 'Vendor',
	'marketing.demo.invoiceNo': 'Invoice no.',
	'marketing.demo.amount': 'Amount',
	'marketing.demo.due': 'Due',
	'marketing.demo.glCode': 'GL code',
	'marketing.demo.glName': 'Cost of goods',
	'marketing.demo.stamp': 'Approved · Paid by virtual card',
	'marketing.demo.stage.capture': 'Capture',
	'marketing.demo.stage.extract': 'Read',
	'marketing.demo.stage.approve': 'Approve',
	'marketing.demo.stage.pay': 'Pay',

	// ── Integrations row (AdapterRail.svelte) ───────────────────────────
	'marketing.adapters.heading': 'Built-in integrations',

	// ── Stats ───────────────────────────────────────────────────────────
	'marketing.stats.rails': 'payment rails, ACH to CHAPS',
	'marketing.stats.stepTypes': 'workflow step types',
	'marketing.stats.languages': 'interface languages',
	'marketing.stats.rebateValue': '1–2%',
	'marketing.stats.rebate': 'typical rebate where vendors accept card',

	// ── Features ────────────────────────────────────────────────────────
	'marketing.features.eyebrow': 'Everything you need',
	'marketing.features.heading': 'One platform, from capture to payment.',
	'marketing.features.lede':
		'Most teams piece together invoice capture, approvals, matching and ERP posting from four different tools. FeohLedger does all of it in one place, and works with the systems you already run.',
	'marketing.features.extract.title': 'AI invoice reading',
	'marketing.features.extract.body':
		'Drop in a PDF or a photo. AI reads every field and line item and shows how sure it is of each one, so your team checks only what needs checking.',
	'marketing.features.approve.title': 'Approval workflows',
	'marketing.features.approve.body':
		'Route invoices by amount, vendor or GL code — no code to write. Segregation of duties and a full audit trail are built in, and approvers can sign off from Slack, Microsoft Teams, email or the mobile app.',
	'marketing.features.match.title': '2-way and 3-way PO matching',
	'marketing.features.match.body':
		'Invoices are matched against purchase orders and goods receipts, within the tolerances you set. Mismatches go to the exception queue, not your inbox.',
	'marketing.features.erp.title': 'Two-way ERP sync',
	'marketing.features.erp.body':
		'Approved invoices post to NetSuite or Dynamics 365 Business Central, or to other accounting systems through Merge.dev. Payment status comes back, so your ERP stays the system of record.',
	'marketing.features.card.title': 'Virtual card payments',
	'marketing.features.card.body':
		'Pay each invoice with its own single-use virtual card, issued through Lithic or Nium, and earn a typical 1–2% rebate on payments to vendors that accept card.',
	'marketing.features.exceptions.title': 'Exception queue',
	'marketing.features.exceptions.body':
		'Duplicates, fraud flags, amounts over limit and unverified vendors are flagged, assigned and resolved in one queue — no digging through email.',

	// ── How it works ────────────────────────────────────────────────────
	'marketing.how.eyebrow': 'How it works',
	'marketing.how.heading': 'Invoice to ledger, in three steps.',
	'marketing.how.capture.title': 'Capture',
	'marketing.how.capture.body':
		'Forward invoices by email, upload or drag them in, or take a photo in the mobile app. PDFs, images and scanned paper all work.',
	'marketing.how.route.title': 'Read and route',
	'marketing.how.route.body':
		'AI picks out the vendor, amount, GL code and line items. Your workflow sends the invoice to the right approver automatically.',
	'marketing.how.pay.title': 'Pay and sync',
	'marketing.how.pay.body':
		'Approved invoices post to your ERP and are scheduled for payment by ACH, wire, check or virtual card.',

	// ── Why teams switch (the tally section) ────────────────────────────
	'marketing.tally.eyebrow': 'Why teams switch',
	'marketing.tally.heading': 'Built for finance, not marketed at them.',
	'marketing.tally.lede':
		'The Exchequer settled a debt by notching a stick, splitting it, and giving each party half — the debt was paid when the halves matched. That is still the job: an invoice against a purchase order, a payment against a statement. Everything here exists to make the halves meet.',
	'marketing.tally.alt':
		'A notched gold tally stick split lengthwise into two halves, the notches on each half lining up with the other.',
	'marketing.diff.ai.title': 'AI you control',
	'marketing.diff.ai.body':
		'Use our built-in AI, or connect your own Claude, OpenAI or AWS Textract account under your own data-retention terms. If invoices must never leave your servers, run the AI there with Ollama.',
	'marketing.diff.connect.title': 'Works with what you already run',
	'marketing.diff.connect.body':
		'Your ERP, bank, card issuer and email stay where they are — FeohLedger connects to each of them. Change one of those providers later and your approval rules and history stay put.',
	'marketing.diff.cards.title': 'Cards that pay you back',
	'marketing.diff.cards.body':
		'Virtual card payments through Lithic or Nium earn a typical 1–2% rebate. If {monthly} a month of invoices moved to card, that would return {low}–{high} a year to your budget.',
	'marketing.diff.cards.note':
		'Illustrative. Rebates depend on your negotiated rate, how much spend moves to card, and which vendors accept it.',
	'marketing.diff.mobile.title': 'Mobile without compromise',
	'marketing.diff.mobile.body':
		'Native iOS and Android apps with camera capture, biometric sign-in, swipe-to-approve and offline mode. Approvers can clear the queue from an airport lounge.',

	// ── Closing call to action ──────────────────────────────────────────
	'marketing.final.heading': 'Set up your workspace in minutes.',
	'marketing.final.body':
		'Choose your workspace address, confirm your email, then set your password from the link we send you. The Free plan needs no card, no contract and no sales call.',

	// ── Footer ──────────────────────────────────────────────────────────
	'marketing.footer.label': 'Footer',
	'marketing.footer.copyright': '© {year} {product}.',

	// ── Find your workspace (routes/find-workspace) ─────────────────────
	'marketing.findWorkspace.pageTitle': 'Sign in — {product}',
	'marketing.findWorkspace.heading': 'Sign in to your workspace',
	'marketing.findWorkspace.subtitle':
		'Each company signs in at its own workspace address. Enter yours and we will take you to its sign-in page.',
	'marketing.findWorkspace.label': 'Workspace address',
	'marketing.findWorkspace.hint':
		'It is in your welcome email, and in the link of any email your workspace has sent you.',
	'marketing.findWorkspace.submit': 'Continue',
	'marketing.findWorkspace.going': 'Opening {host}…',
	'marketing.findWorkspace.problem.empty': 'Enter your workspace address.',
	'marketing.findWorkspace.problem.startLetter': 'A workspace address starts with a letter.',
	'marketing.findWorkspace.problem.characters':
		'Use only the first part of the address — lowercase letters, digits and hyphens.',
	'marketing.findWorkspace.problem.tooShort': 'A workspace address is at least 3 characters.',
	'marketing.findWorkspace.problem.tooLong': 'A workspace address is at most 30 characters.',
	'marketing.findWorkspace.problem.endHyphen': "A workspace address can't end with a hyphen.",
	'marketing.findWorkspace.problem.doubleHyphen': "A workspace address can't contain two hyphens in a row.",
	'marketing.findWorkspace.ownDomain': 'That looks like your company\'s own address. {link}',
	'marketing.findWorkspace.ownDomainLink': 'Sign in at {host}',
	'marketing.findWorkspace.configError':
		"We couldn't load the sign-in details. Check your connection and try again.",
	'marketing.findWorkspace.retry': 'Try again',
	'marketing.findWorkspace.lost':
		"Can't find it? Ask a colleague who already signs in — the address is in their browser's address bar.",
	'marketing.findWorkspace.noWorkspace': 'New to {product}? {create}',
	'marketing.findWorkspace.panelHeading': 'Why we ask for the address',
	'marketing.findWorkspace.panelBody':
		'Every company has its own workspace, with its own sign-in. We never look a workspace up from an email address, so nobody can use this page to find out where someone works.'
};
