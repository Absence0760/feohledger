<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import Fact from '$lib/legal/Fact.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';
</script>

<LegalPage
	title="Terms of Service"
	intro="These Terms govern business use of the FeohLedger accounts-payable platform. They are written for organisations, not consumers: the person who accepts them does so on behalf of a company, and that company is the customer."
>
	<h2 id="agreement">1. The agreement</h2>

	<p>
		These Terms of Service (the <strong>Terms</strong>) are a binding agreement between
		the organisation that subscribes to or uses {OPERATOR.serviceName} (the
		<strong>Customer</strong>, <strong>you</strong>) and the provider of
		{OPERATOR.serviceName} (<strong>we</strong>, <strong>us</strong>, <strong>our</strong>).
	</p>

	<p>
		{OPERATOR.serviceName} is operated by {OPERATOR.controllerDescription}. Where these
		Terms refer to our registered legal entity, that entity is
		<Fact value={OPERATOR.legalEntity} label="registered legal entity" />. Until
		incorporation is complete the counterparty to this agreement is the individual named
		above, who provides the Service personally; on incorporation we may assign these
		Terms to the company formed to operate the Service, as described in
		<a href="#general">section 20</a>.
	</p>

	<p>
		<strong>Authority to bind.</strong> By accepting these Terms — by clicking an accept
		control, by signing an Order, or by accessing or using the Service — the individual
		doing so represents that they are at least 18 years old and are authorised to bind
		the organisation they name. If you do not have that authority, or the organisation
		does not agree to these Terms, do not accept them and do not use the Service. Where
		no organisation is named, the individual accepting is the Customer and is personally
		bound.
	</p>

	<p>
		<strong>Not a consumer service.</strong> The Service is offered for business purposes
		only. It is not offered to consumers, and it is not intended for personal, family or
		household use.
	</p>

	<p>
		<strong>Order of precedence.</strong> If we and you sign an order form, quotation or
		other written agreement for the Service (an <strong>Order</strong>), and it conflicts
		with these Terms, the Order prevails for that conflict only. The Data Processing
		Addendum at <a href="/legal/dpa">/legal/dpa</a> prevails over both in respect of the
		processing of personal data.
	</p>

	<p>
		<strong>Source code is separate.</strong> Parts of the {OPERATOR.serviceName} source
		code are published publicly under an open-source licence (the MIT licence, in the
		repository that contains it). These Terms govern the hosted service we operate; they
		do not restrict, and are not restricted by, any right granted to you under that
		licence in respect of the source code itself. If you run your own copy of the
		software, these Terms do not apply to that copy, and we provide it without warranty
		or support except as that licence states.
	</p>

	<h2 id="definitions">2. Definitions</h2>

	<ul>
		<li>
			<strong>Service</strong> — the hosted {OPERATOR.serviceName} application,
			including its web application, mobile applications, supplier portal, public
			developer API, outbound webhooks, and the documentation we publish for them.
		</li>
		<li>
			<strong>Customer</strong> — the organisation that subscribes to the Service, in
			whose workspace (tenant) its data is held.
		</li>
		<li>
			<strong>Authorised User</strong> — an individual the Customer provisions in its
			workspace to use the Service on the Customer's behalf, such as its finance,
			accounts-payable, procurement or administrative staff.
		</li>
		<li>
			<strong>Supplier User</strong> — an individual from one of the Customer's
			suppliers or vendors who is given access to the supplier portal in connection
			with that supplier's dealings with the Customer. A Supplier User is not our
			customer; access is granted by, and on the responsibility of, the Customer.
		</li>
		<li>
			<strong>Customer Data</strong> — all data, documents and content the Customer,
			its Authorised Users or its Supplier Users submit to, or generate in, the
			Service. This includes invoices and their attachments, purchase orders, goods
			receipts, vendor and bank records, expense claims and receipts, contracts,
			payment instructions and their results, workflow and approval records, audit
			records, and the personal data contained in any of them.
		</li>
		<li>
			<strong>Order</strong> — an order form, quotation, online plan selection or other
			written agreement recording the plan, fees and term applicable to the Customer.
		</li>
		<li>
			<strong>Documentation</strong> — the product documentation we publish for the
			Service, as updated from time to time.
		</li>
	</ul>

	<h2 id="service">3. The Service, accounts and access</h2>

	<h3>3.1 Access rights</h3>
	<p>
		Subject to these Terms and to payment of any applicable fees, we grant the Customer a
		non-exclusive, non-transferable, non-sublicensable right, during the term, to access
		and use the Service for its own internal business purposes and for the operation of
		its accounts-payable function, including allowing its Supplier Users to use the
		supplier portal for that purpose. All rights not expressly granted are reserved.
	</p>

	<h3>3.2 Accounts and credentials</h3>
	<p>
		The Customer is responsible for all activity under its workspace and for keeping
		credentials confidential. Multi-factor authentication is available, by
		time-based one-time code and by passkey, and the Customer's administrators can
		require it across the workspace; we recommend that they do. The Customer must notify
		us at <a href="mailto:{CONTACT.security}">{CONTACT.security}</a> promptly on becoming
		aware of any unauthorised access to, or use of, its workspace or credentials.
	</p>

	<h3>3.3 Administrator responsibility</h3>
	<p>
		The Customer's administrators provision Authorised Users and Supplier Users, assign
		their roles and permissions, and remove access when it is no longer appropriate.
		Because the Service enforces approval limits, segregation of duties and payment
		authority through those roles and permissions, the Customer is responsible for
		configuring them to reflect its own internal controls, and for reviewing them when
		staff or duties change. We do not police the Customer's internal allocation of
		authority.
	</p>

	<h3>3.4 Authorised Users and Supplier Users</h3>
	<p>
		The Customer must ensure that everyone it gives access complies with these Terms, and
		is responsible for their acts and omissions as if they were its own. Credentials are
		issued to a named individual and must not be shared.
	</p>

	<h2 id="customer-data">4. Customer Data and data protection</h2>

	<p>
		<strong>The Customer owns its data.</strong> As between us and the Customer, the
		Customer retains all right, title and interest in Customer Data. We acquire no
		ownership of it.
	</p>

	<p>
		<strong>The licence we take is limited to running the Service.</strong> The Customer
		grants us a worldwide, non-exclusive, royalty-free licence to host, copy, transmit,
		display, process and otherwise use Customer Data solely to the extent necessary to
		provide, secure, maintain and support the Service for the Customer, to carry out the
		operations the Customer initiates through it, and to comply with law. That licence
		lasts only as long as we hold the data, and ends when the data is deleted or returned
		under <a href="#termination">section 10</a>.
	</p>

	<p>
		<strong>Personal data.</strong> Customer Data contains personal data — the Customer's
		own staff, its suppliers' staff, and individuals named on invoices, contracts and
		expense claims. In respect of that data the Customer is the controller and we are the
		processor. Our processing is governed by the
		<strong><a href="/legal/dpa">Data Processing Addendum</a></strong> at
		<a href="/legal/dpa">/legal/dpa</a>, which is incorporated into these Terms by
		reference and forms part of them. The DPA sets out the instructions we act on, the
		security measures we apply, our use of sub-processors (listed at
		<a href="/legal/sub-processors">/legal/sub-processors</a>), how we assist with data
		subject requests, and the return-or-delete obligation that applies when this
		agreement ends. Personal data we hold about the Customer's own contacts in our own
		right — for billing and account administration — is covered by the
		<a href="/legal/privacy">Privacy Policy</a>.
	</p>

	<p>
		<strong>The Customer's warranty about its data.</strong> The Customer warrants that it
		has the right to submit Customer Data to the Service and to have it processed as
		these Terms and the DPA describe, including where the data concerns its suppliers and
		their staff, and that it has given any notice and obtained any consent or other legal
		basis required for that processing.
	</p>

	<h2 id="fees">5. Subscription, fees, taxes and renewal</h2>

	<h3>5.1 Plans and fees</h3>
	<p>
		The Service is provided on a subscription plan. A plan fixes a recurring charge in a
		stated currency, the features it entitles the Customer to, and any per-seat or
		usage-based components. <strong>These Terms do not set a price.</strong> The plan
		that applies to the Customer, the fees for it, the billing currency and the term are
		those recorded in the Order. Prices shown on our marketing pages are an indication of
		our current offering, may change, and are not an offer capable of acceptance until
		recorded in an Order or selected in the application.
	</p>

	<h3>5.2 No-charge plans</h3>
	<p>
		Some plans carry no charge. A workspace can be created on a no-charge plan without
		providing payment card details. A no-charge plan may carry usage limits, and may
		exclude features that are reserved to paid plans — the public developer API is one
		such feature today, and a request to it from a workspace whose plan does not include
		it is refused. We may change or withdraw a no-charge plan on reasonable notice, and
		nothing in these Terms obliges us to continue offering one.
	</p>

	<h3>5.3 Trials</h3>
	<p>
		Where a plan includes a trial period, the length of that period is part of the plan
		and is shown with it. Unless the Order says otherwise, at the end of a trial the
		subscription continues on that plan's standard charges. A trial may be withdrawn or
		shortened if it is being used to evade fees or in breach of
		<a href="#acceptable-use">section 6</a>.
	</p>

	<h3>5.4 Billing periods and renewal</h3>
	<p>
		Unless the Order states otherwise, subscriptions run in successive monthly periods
		anchored on the date the subscription started, and <strong>renew automatically</strong>
		for further periods until cancelled under clause 5.8. Where a month has no
		corresponding day, the period ends on the last day of that month.
	</p>

	<h3>5.5 Changing plan mid-period</h3>
	<p>
		An administrator or CFO user can change the Customer's plan in the application. A
		change takes effect immediately. The difference between the plans is apportioned
		across the days remaining in the current period — charged on an upgrade, credited on
		a downgrade — computed exactly and rounded to two decimal places, and recorded in the
		Customer's audit trail. Changing to the plan the Customer is already on has no effect
		and is not charged.
	</p>

	<h3>5.6 Usage metering</h3>
	<p>
		Where a plan has usage-based components, we meter the relevant activity for each
		billing period — for example the number of invoice extractions performed — and bill
		or report it for that period. Usage figures produced by the Service are our records
		of the Service's own operation and, absent manifest error, are what the invoice is
		based on.
	</p>

	<h3>5.7 Payment, taxes and payment details</h3>
	<p>
		Fees are payable in advance for each period unless the Order says otherwise, and are
		charged through the payment processor identified in the Order or in the application.
		We do not store full payment card numbers: the processor holds the payment method,
		and we retain only metadata such as the card brand, the last four digits and the
		expiry date. Fees are exclusive of VAT, GST, sales and other transaction taxes, which
		the Customer pays in addition. If the Customer is required by law to withhold tax on
		a payment to us, it will gross up so that we receive the amount we would have
		received without the withholding. Save where these Terms or the law say otherwise,
		fees are non-refundable.
	</p>

	<h3>5.8 Non-payment, and cancelling</h3>
	<p>
		If a charge fails or a fee is not paid when due, the subscription moves to a past-due
		state. If it remains unpaid after a grace period, we may cancel the subscription,
		which withdraws the entitlements that plan granted, and we may suspend access under
		<a href="#termination">section 10</a>. The Customer may cancel by moving to a
		no-charge plan where one is offered, or by writing to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>. Unless the Order says otherwise,
		cancellation takes effect at the end of the then-current period, and fees already paid
		for that period are not refunded.
	</p>

	<h2 id="acceptable-use">6. Acceptable use</h2>

	<p>The Customer must not, and must not permit anyone else to:</p>

	<ul>
		<li>
			use the Service in breach of any law, regulation or sanctions measure, or for any
			fraudulent, deceptive or unlawful purpose;
		</li>
		<li>
			upload or submit data it does not have the right to submit, or that infringes
			another person's rights;
		</li>
		<li>
			attempt to access, probe or interfere with another customer's workspace or data,
			test or circumvent the tenant isolation that separates workspaces, or manipulate
			the headers, tokens, subdomains or identifiers by which a workspace is resolved;
		</li>
		<li>
			circumvent or attempt to circumvent rate limits, quotas, plan entitlements or
			seat limits on the developer API or anywhere else in the Service, including by
			rotating keys, accounts or addresses to defeat them;
		</li>
		<li>
			use the Service to facilitate fraud, money laundering, terrorist financing,
			sanctions evasion, tax evasion or bribery — including using the approval
			notifications the Service sends into email, Slack or Microsoft Teams, or the
			supplier portal, to induce a payment or a change of bank details by deception,
			and including obtaining or using a virtual card for a purpose the Customer has not
			genuinely authorised;
		</li>
		<li>
			impersonate another person or misrepresent an affiliation, including by sending
			supplier communications or approval requests in a name that is not the Customer's
			own;
		</li>
		<li>
			introduce malware or any code intended to harm, disable or gain unauthorised
			access to the Service or to any system it connects to;
		</li>
		<li>
			conduct penetration testing, vulnerability scanning, load testing or any
			comparable security testing against the hosted Service without our prior written
			permission — we welcome good-faith vulnerability reports at
			<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>;
		</li>
		<li>
			resell, rent, time-share or otherwise make the hosted Service available to a third
			party who is not an Authorised User or a Supplier User, except under a partner or
			reseller arrangement we have agreed in writing;
		</li>
		<li>
			scrape or bulk-extract data from the Service other than through its own export and
			API features, or use the Service to benchmark it for, or build, a competing
			product — nothing in this bullet limits any right granted by the open-source
			licence of published source code;
		</li>
		<li>
			reverse engineer, decompile or disassemble the hosted Service, except to the
			extent that restriction is prohibited by law or permitted by that licence.
		</li>
	</ul>

	<p>
		We may investigate suspected breaches of this section and may suspend access under
		<a href="#termination">section 10</a>. The Customer must report any breach it becomes
		aware of to <a href="mailto:{CONTACT.security}">{CONTACT.security}</a>.
	</p>

	<h2 id="not-advice">7. What {OPERATOR.serviceName} is not</h2>

	<p>
		This section matters more than any other in these Terms. {OPERATOR.serviceName} is
		software that helps a finance team do its work. It does not do that work, and it does
		not take responsibility for it. Read the five statements below as limits on what the
		Service is, not as small print.
	</p>

	<h3>7.1 It is not professional advice</h3>
	<p>
		The Service does not provide tax, accounting, legal, audit or financial advice, and
		nothing it produces — a suggested general-ledger code, a tax treatment, a 1099 or VAT
		figure, a cash-flow forecast, an assistant answer, a report or an export — is advice
		or an opinion on which the Customer should rely without its own professional
		judgement. The Customer remains responsible for its books, filings, disclosures and
		professional obligations, and should take its own advice.
	</p>

	<h3>7.2 It is not a bank or a payment institution</h3>
	<p>
		We are not a bank, payment institution, electronic money institution, money
		transmitter or money services business, and we are not licensed as any of those. We do
		not hold, control or take custody of the Customer's funds. Payments the Customer makes
		through the Service are initiated on, and settled by, the Customer's own banks,
		payment providers and card issuers, under the Customer's own agreements with them. The
		Customer authorises each payment and remains solely responsible for doing so — for
		whether it is due, to whom, in what amount, and on what bank details. A payment status
		shown in the Service reflects what the relevant provider has reported to us; it is not
		a confirmation by us that funds have moved.
	</p>

	<h3>7.3 Sanctions screening is a screening aid, not a compliance determination</h3>
	<p>
		The vendor sanctions and watchlist screening in the Service is a screening aid. A
		result — a hit, a clear result, or a risk score — is not a determination that a
		person or entity is or is not sanctioned, and is not legal advice. Results depend on
		the data source configured for the workspace, on the completeness and accuracy of the
		vendor records screened, and on matching that can produce both false positives and
		false negatives. Every result requires human review. The Customer remains solely
		responsible for its own sanctions, anti-money-laundering, know-your-supplier and
		counter-terrorist-financing obligations, and for any decision to pay, block or report.
	</p>

	<h3>7.4 AI extraction and AI suggestions can be wrong</h3>
	<p>
		The Service uses automated and AI-assisted processing to read invoices, receipts,
		statements and contracts, to propose general-ledger coding, to match invoices against
		purchase orders and goods receipts, to detect possible duplicates and fraud
		indicators, to route approvals, to resolve exceptions and to answer questions. Every
		output of that processing is a <strong>suggestion requiring human review</strong>. It
		can be incomplete, mis-read, mis-matched or simply wrong, and an absence of a warning
		is not an assurance that nothing is wrong. The Customer is responsible for reviewing
		what it approves and pays, and for the consequences of approving or paying it. The
		Customer must not configure the Service so that invoices are approved or paid without
		the human review its own controls require.
	</p>

	<h3>7.5 E-invoicing and tax features assist with compliance; they do not guarantee it</h3>
	<p>
		The structured e-invoicing, PEPPOL, country-format and tax features assist the
		Customer in producing and exchanging documents and figures. They do not guarantee
		compliance with any jurisdiction's e-invoicing mandate, clearance regime, tax
		reporting obligation or record-keeping rule, and they are not a certification,
		accreditation or clearance by us or by any authority. Mandates change, and a format
		or a network we support may cease to be accepted. The Customer remains responsible
		for determining what applies to it and for meeting it.
	</p>

	<h2 id="third-party">8. Third-party services and integrations</h2>

	<p>
		The Service connects to third parties the Customer chooses: ERP and accounting
		systems, banks and payment rails, card issuers, e-invoicing networks and access
		points, screening data providers, AI and document-extraction providers, identity
		providers, email and chat platforms, and others. Each of those relationships is
		between the Customer and that provider, governed by that provider's own terms and
		pricing, and the Customer is responsible for its own account, credentials,
		entitlements and compliance with them.
	</p>

	<p>
		We are not responsible for the acts, omissions, availability, accuracy, security or
		pricing of any third-party service, for any data it returns, or for any charge it
		levies. If a third-party service changes, degrades, imposes a limit or withdraws an
		interface, the corresponding feature of the Service may change or stop working, and
		that alone is not a breach of these Terms by us. Where the Customer supplies its own
		credentials or keys for such a service, it is responsible for their safekeeping,
		scope and rotation.
	</p>

	<h2 id="service-levels">9. Availability, support and changes</h2>

	<h3>9.1 Availability</h3>
	<p>
		<strong>The Service is provided without a committed uptime service level agreement
		unless a separate written Order says otherwise.</strong> We aim to keep the Service
		available and to keep unplanned downtime short, and we will use reasonable efforts to
		give advance notice of planned maintenance where it is likely to be disruptive, but we
		do not commit to an availability percentage, a maintenance window or a service credit
		except in an Order that expressly provides one.
	</p>

	<h3>9.2 Support</h3>
	<p>
		Support is provided by email at <a href="mailto:{CONTACT.support}">{CONTACT.support}</a>
		during our normal business hours, on reasonable efforts. We do not commit to a
		response or resolution time except in an Order that expressly provides one. Support
		covers the Service as we provide it; it does not extend to the Customer's own
		infrastructure, its third-party integrations, or a self-hosted copy of the software.
	</p>

	<h3>9.3 Changes to the Service</h3>
	<p>
		We develop the Service continuously and may add, change or remove features. We will
		not make a change that materially reduces the core functionality of a paid plan during
		a period the Customer has paid for without giving reasonable prior notice; if we do,
		the Customer may terminate the affected subscription by notice before the change takes
		effect and we will refund any pre-paid fees for the unused remainder of the period.
		Features identified as beta, preview or experimental are provided as-is, may change or
		be withdrawn at any time, and are excluded from clause 12.1.
	</p>

	<h2 id="termination">10. Suspension, termination, export and deletion</h2>

	<h3>10.1 Suspension</h3>
	<p>
		We may suspend the Customer's access, in whole or in part, where: a fee is overdue and
		remains unpaid after notice; we reasonably believe the Service is being used in breach
		of <a href="#acceptable-use">section 6</a>; continued access poses a security risk to
		the Service, to us or to another customer; or suspension is required by law. Except
		where an immediate suspension is necessary to address a security, legal or fraud risk,
		we will give notice and, where the breach is capable of remedy, a reasonable
		opportunity to remedy it. We will restore access promptly once the cause is resolved.
		Suspension does not relieve the Customer of its obligation to pay fees for the
		suspended period unless the suspension was our fault.
	</p>

	<h3>10.2 Termination</h3>
	<p>
		The Customer may terminate for convenience with effect from the end of the
		then-current subscription period, as described in clause 5.8. Either party may
		terminate immediately on written notice if the other: commits a material breach that
		is not remedied within 30 days of written notice describing it; commits a material
		breach that is not capable of remedy; or becomes insolvent, enters an insolvency or
		analogous process, or ceases to carry on business. We may also terminate immediately
		for a serious breach of <a href="#acceptable-use">section 6</a> or for non-payment
		that persists after suspension.
	</p>

	<h3>10.3 Effect of termination</h3>
	<p>
		On termination the right to access the Service ends, all fees accrued up to the
		effective date become due, and each party returns or destroys the other's Confidential
		Information under <a href="#confidentiality">section 11</a>. Termination does not
		affect rights or liabilities that accrued before it.
	</p>

	<h3>10.4 Exporting the Customer's data</h3>
	<p>
		The Customer can export its data throughout the term using the Service's own export
		features — the list, report and audit exports, the data-subject export, and the
		developer API. We strongly recommend exporting before terminating, because export
		depends on access. We will not delete Customer Data without giving the Customer a
		reasonable opportunity to export it — in practice, the same 30-day election window the
		<a href="/legal/dpa#deletion">Data Processing Addendum's deletion section</a> describes —
		and we will provide a reasonable export on request made before deletion, except where
		suspension for a security or legal reason makes that impossible or where law forbids it.
	</p>

	<h3>10.5 Deletion</h3>
	<p>
		After termination we will delete or return Customer Data in accordance with the
		return-or-delete obligation in the
		<a href="/legal/dpa">Data Processing Addendum</a>, which governs the timing and the
		manner of that deletion. We may retain data where a law requires us to, and backups
		are deleted on their ordinary cycle; anything so retained remains subject to these
		Terms and to the DPA for as long as we hold it. Records the Service keeps as an
		append-only audit trail are deleted with the workspace, not edited out of it. Where a
		copy of that trail has been shipped to a write-once archive, the
		<a href="/legal/dpa#deletion">DPA's deletion section</a> explains why that copy can
		outlast the workspace until its own retention period ends.
	</p>

	<h2 id="confidentiality">11. Confidentiality</h2>

	<p>
		Each party may receive information of the other that is marked confidential or that a
		reasonable person would understand to be confidential (<strong>Confidential
		Information</strong>). Customer Data is the Customer's Confidential Information; the
		non-public parts of the Service, its security architecture, and our pricing are ours.
	</p>

	<p>
		The receiving party will use the other's Confidential Information only to perform this
		agreement, will protect it with at least the care it applies to its own confidential
		information and in no case less than reasonable care, and will disclose it only to its
		personnel, professional advisers and subcontractors who need it and are bound by
		confidentiality obligations no less protective. These obligations do not apply to
		information that is or becomes public without breach, was already known free of
		obligation, is independently developed without use of the other's Confidential
		Information, or is rightfully received from a third party. A party may disclose
		Confidential Information where required by law or a regulator, giving such notice to
		the other as the law permits. These obligations survive for three years after
		termination, and for as long as the information remains a trade secret.
	</p>

	<h2 id="warranties">12. Warranties and disclaimers</h2>

	<h3>12.1 Our warranty</h3>
	<p>
		We warrant that during the term the Service will perform materially in accordance with
		the Documentation, and that we will provide it with reasonable skill and care. If it
		does not, the Customer must notify us with enough detail to reproduce the problem, and
		our obligation — and the Customer's exclusive remedy for breach of this warranty — is
		to correct the non-conformity within a reasonable time or, if we cannot, to allow the
		Customer to terminate the affected subscription and to refund the pre-paid fees for
		the unused remainder of the then-current period.
	</p>

	<h3>12.2 Mutual warranties</h3>
	<p>
		Each party warrants that it has the power and authority to enter into this agreement
		and that doing so does not breach any other agreement binding on it.
	</p>

	<h3>12.3 Everything else is disclaimed</h3>
	<p>
		Except as expressly stated in clause 12.1, and to the fullest extent permitted by law,
		the Service is provided <strong>as is</strong> and <strong>as available</strong>, and
		we disclaim all other warranties, conditions, representations and terms, whether
		express, implied or statutory, including implied warranties of merchantability,
		satisfactory quality, fitness for a particular purpose, title, non-infringement,
		accuracy, and any warranty arising from a course of dealing or usage of trade. We do
		not warrant that the Service will be uninterrupted or error-free, that it will detect
		every duplicate, fraud indicator, sanctions match, mismatch or error, that automated
		or AI-assisted output will be accurate, or that the Service will meet any regulatory
		requirement applicable to the Customer.
	</p>

	<h3>12.4 Mandatory law</h3>
	<p>
		Nothing in these Terms excludes or limits any warranty, right or remedy that cannot be
		excluded or limited under the law that applies to the Customer, including mandatory
		consumer-protection law where it applies despite the business nature of this
		agreement. Where such a law applies, the exclusions and limitations in these Terms
		apply only to the extent that law permits.
	</p>

	<h2 id="liability">13. Limitation of liability</h2>

	<h3>13.1 Excluded losses</h3>
	<p>
		To the fullest extent permitted by law, neither party is liable to the other for any
		indirect, incidental, special, consequential, exemplary or punitive loss, or for loss
		of profit, revenue, business, goodwill, anticipated savings, or loss or corruption of
		data or its cost of recovery, however arising and whether or not the possibility of it
		was known.
	</p>

	<h3>13.2 Losses that are the Customer's own</h3>
	<p>
		Without limiting clause 13.1, and to the fullest extent permitted by law, we are not
		liable for any loss arising from: a payment the Customer or an Authorised User
		authorised or approved; an invoice, expense, credit memo or vendor bank detail the
		Customer accepted, whether or not the Service flagged it; a compliance, tax,
		accounting or sanctions determination the Customer made; or the Customer's reliance on
		an automated or AI-assisted output without the human review
		<a href="#not-advice">section 7</a> requires.
	</p>

	<h3>13.3 Cap</h3>
	<p>
		To the fullest extent permitted by law, each party's total aggregate liability arising
		out of or in connection with this agreement, whether in contract, tort (including
		negligence), breach of statutory duty or otherwise, is limited to the total fees paid
		or payable by the Customer for the Service in the twelve months immediately preceding
		the first event giving rise to the liability. Where the Customer uses the Service on a
		no-charge plan, and so has paid no fees, our total aggregate liability is limited to
		one hundred units of the billing currency stated in the Order, or the smallest amount
		the applicable law permits if that is greater.
	</p>

	<h3>13.4 What the cap and exclusions do not cover</h3>
	<p>
		Nothing in these Terms limits or excludes liability for: death or personal injury
		caused by negligence; fraud or fraudulent misrepresentation; gross negligence or
		wilful misconduct; the Customer's obligation to pay fees due; either party's breach of
		<a href="#confidentiality">section 11</a>; either party's obligations under
		<a href="#indemnities">section 14</a>; or any other liability that cannot be limited
		or excluded by law.
	</p>

	<h3>13.5 Basis of the bargain</h3>
	<p>
		The allocation of risk in sections 12 and 13 is a fundamental basis of the fees
		charged, and each party acknowledges it would not have entered into this agreement on
		these fees without it.
	</p>

	<h2 id="indemnities">14. Indemnities</h2>

	<h3>14.1 By the Customer</h3>
	<p>
		The Customer will defend us against any third-party claim arising from: Customer Data,
		including a claim that it infringes a third party's rights or was submitted or
		processed without the necessary rights, notices or legal basis; the Customer's use of
		the Service in breach of these Terms or of law; a claim by an Authorised User, a
		Supplier User or a supplier of the Customer concerning the Customer's own decisions,
		payments or communications; and the Customer's breach of
		<a href="#export-control">section 16</a>. The Customer will indemnify us against
		damages, costs and reasonable legal fees finally awarded or agreed in settlement of
		such a claim.
	</p>

	<h3>14.2 By us</h3>
	<p>
		We will defend the Customer against any third-party claim that the Service, as
		provided by us and used in accordance with these Terms, infringes that third party's
		patent, copyright, trade mark or trade secret, and will indemnify the Customer against
		damages, costs and reasonable legal fees finally awarded or agreed in settlement of
		it. This does not apply to a claim arising from: Customer Data; modification of the
		Service by anyone other than us; combination of the Service with anything we did not
		supply, where the claim would not have arisen without that combination; use of the
		Service in breach of these Terms or after we have told the Customer to stop; a
		third-party service; a self-hosted copy of the software; or use of the Service on a
		no-charge plan or during a trial.
	</p>

	<h3>14.3 Our options</h3>
	<p>
		If the Service becomes, or we reasonably believe it may become, the subject of such a
		claim, we may at our option and expense procure the right to continue using it, modify
		or replace it so that it is non-infringing while materially equivalent, or, if neither
		is reasonably available, terminate the affected subscription on notice and refund any
		pre-paid fees for the unused remainder of the then-current period. Clauses 14.2 and
		14.3 state our entire liability and the Customer's exclusive remedy for a third-party
		intellectual-property claim.
	</p>

	<h3>14.4 Conduct of a claim</h3>
	<p>
		The indemnified party must notify the indemnifying party promptly on becoming aware of
		a claim, give it sole control of the defence and settlement (except that no settlement
		imposing a non-monetary obligation or admission on the indemnified party may be made
		without its consent, not to be unreasonably withheld), provide reasonable cooperation
		at the indemnifying party's expense, and take reasonable steps to mitigate. A failure
		to notify promptly reduces the indemnity only to the extent the delay causes
		prejudice.
	</p>

	<h2 id="ip">15. Intellectual property and feedback</h2>

	<p>
		We and our licensors own all intellectual property rights in the Service, including
		its software, interfaces, documentation, designs, brand and the {OPERATOR.serviceName}
		name and marks. Apart from the access rights in
		<a href="#service">section 3</a> — and apart from any rights separately granted by the
		open-source licence of published source code — the Customer receives no licence in
		them, and must not remove or obscure any proprietary notice.
	</p>

	<p>
		If the Customer or any of its users sends us feedback, suggestions, feature requests or
		bug reports, we may use them without restriction, obligation or payment. That licence
		is perpetual, irrevocable, worldwide, royalty-free and sublicensable. Feedback must not
		include Customer Data or Confidential Information, and we will not treat it as either.
	</p>

	<p>
		To the extent the <a href="/legal/dpa">Data Processing Addendum</a> permits, we may
		generate aggregated and de-identified statistics about how the Service is used and use
		them to operate, secure and improve it, provided they do not identify the Customer,
		any individual or any supplier, and cannot reasonably be used to do so.
	</p>

	<h2 id="export-control">16. Export control and sanctions</h2>

	<p>
		Each party will comply with the export control, trade and economic sanctions laws that
		apply to it. The Customer represents and warrants that neither it, nor any of its
		Authorised Users or Supplier Users, nor any entity that controls it, is a person
		designated on a restricted-party or sanctions list, or located in or ordinarily
		resident in a jurisdiction subject to comprehensive sanctions; and that it will not
		use the Service, or permit it to be used, in breach of those laws, or to make or
		receive a payment that those laws prohibit. We may suspend or terminate access
		immediately where we reasonably believe continued provision would breach those laws.
		The screening features of the Service do not discharge the Customer's obligations
		under this section — see <a href="#not-advice">clause 7.3</a>.
	</p>

	<h2 id="force-majeure">17. Force majeure</h2>

	<p>
		Neither party is liable for a failure or delay in performing its obligations (other
		than an obligation to pay money) caused by an event beyond its reasonable control,
		including acts of God, fire, flood, epidemic, war, terrorism, civil unrest, labour
		dispute, act of government or regulator, failure of a public telecommunications or
		internet network, failure of a cloud infrastructure or payment provider, or a
		large-scale cyber attack. The affected party will notify the other as soon as
		reasonably practicable and will use reasonable efforts to mitigate. If the event
		continues for more than 60 days, either party may terminate the affected subscription
		on notice, and we will refund pre-paid fees for the unused remainder of the period.
	</p>

	<h2 id="changes">18. Changes to these Terms</h2>

	<p>
		We may update these Terms as the Service, our business or the law changes. For a
		change that materially affects the Customer's rights or obligations, we will give at
		least 30 days' notice before it takes effect, by email to the administrator contacts
		on the account or by a notice in the application; other changes take effect when the
		updated Terms are published. Every version carries the date it was last updated at the
		top of this page.
	</p>

	<p>
		Continued use of the Service after a change takes effect is acceptance of it. A
		Customer that does not accept a material change may terminate the affected
		subscription by notice given before the change takes effect, and we will refund
		pre-paid fees for the unused remainder of the then-current period. We will not apply a
		change retrospectively to a dispute that arose before it took effect.
	</p>

	<h2 id="governing-law">19. Governing law, venue and disputes</h2>

	<p>
		These Terms, and any dispute or claim arising out of or in connection with them or
		their subject matter (including non-contractual disputes), are governed by the laws of
		<Fact value={OPERATOR.governingLaw} label="governing law" />, without regard to its
		conflict-of-laws rules. The United Nations Convention on Contracts for the
		International Sale of Goods does not apply.
	</p>

	<p>
		<strong>Informal resolution first.</strong> Before starting proceedings, the party
		raising a dispute must describe it in writing to the other — to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a> in our case — and the parties will
		attempt in good faith to resolve it through their senior representatives for 30 days
		from that notice. Neither party need wait where it seeks urgent injunctive or
		equitable relief, or where a limitation period would otherwise expire.
	</p>

	<p>
		If the dispute is not resolved, it is subject to the exclusive jurisdiction of the
		courts of <Fact value={OPERATOR.governingLaw} label="governing law" />, and each party
		submits to that jurisdiction. <strong>These Terms contain no agreement to
		arbitrate</strong> and no waiver of any right to a jury trial or to participate in a
		class or representative proceeding; nothing here should be read as imposing one.
	</p>

	<h2 id="general">20. General</h2>

	<h3>20.1 Assignment</h3>
	<p>
		Neither party may assign or transfer this agreement without the other's prior written
		consent, not to be unreasonably withheld, except that either party may assign it in
		whole to a successor in connection with a merger, reorganisation or sale of
		substantially all of its assets or of the business to which it relates. We may in
		addition assign this agreement to a company incorporated to operate
		{OPERATOR.serviceName}, on notice to the Customer. Any purported assignment in breach
		of this clause is void. This agreement binds and benefits the parties' permitted
		successors and assigns.
	</p>

	<h3>20.2 Subcontractors</h3>
	<p>
		We may use subcontractors and sub-processors to provide the Service, and remain
		responsible for their performance of our obligations. Sub-processors that handle
		personal data are governed by the <a href="/legal/dpa">Data Processing Addendum</a>
		and listed at <a href="/legal/sub-processors">/legal/sub-processors</a>.
	</p>

	<h3>20.3 Notices</h3>
	<p>
		Notices to us must be in writing and sent to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>; where a notice must also be sent
		by post, the address is
		<Fact value={OPERATOR.postalAddress} label="postal address for notices" />. Notices to
		the Customer are sent to the administrator contact addresses on its account, or given
		in the application. An emailed notice is deemed given on the next business day after
		it is sent, unless the sender receives a delivery failure. It is the Customer's
		responsibility to keep its administrator contact addresses current.
	</p>

	<h3>20.4 Entire agreement</h3>
	<p>
		These Terms, the Data Processing Addendum, any Order, and the policies they expressly
		incorporate are the entire agreement between the parties on their subject matter and
		supersede all prior discussions, proposals and representations. Neither party relies
		on any statement not set out in them, but nothing excludes liability for fraudulent
		misrepresentation.
	</p>

	<h3>20.5 Severability and waiver</h3>
	<p>
		If any provision is held invalid or unenforceable, it is modified to the minimum extent
		necessary to make it enforceable, or severed if it cannot be, and the rest remains in
		full force. A failure or delay in exercising a right is not a waiver of it, and a
		single or partial exercise does not prevent further exercise. A waiver is effective
		only if given in writing.
	</p>

	<h3>20.6 Relationship of the parties</h3>
	<p>
		The parties are independent contractors. Nothing in these Terms creates a partnership,
		joint venture, agency, fiduciary or employment relationship, and neither party may
		bind the other. There are no third-party beneficiaries: no person who is not a party
		has any right to enforce these Terms.
	</p>

	<h3>20.7 Survival</h3>
	<p>
		Sections 2, 4 (as to ownership), 7, 10.3 to 10.5, 11, 12.3, 12.4, 13, 14, 15, 16, 19
		and 20, and any accrued payment obligation and the tax gross-up obligation in clause
		5.7, survive termination or expiry.
	</p>

	<h3>20.8 Language and interpretation</h3>
	<p>
		These Terms are made in English, and the English version prevails over any translation.
		Headings are for convenience only. <strong>Including</strong> means including without
		limitation, and a reference to a law includes it as amended or replaced.
	</p>

	<h3>20.9 Contact</h3>
	<p>
		Contract and Terms enquiries:
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>. Product support:
		<a href="mailto:{CONTACT.support}">{CONTACT.support}</a>. Security reports:
		<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>.
	</p>
</LegalPage>
