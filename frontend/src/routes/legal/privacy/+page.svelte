<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import Fact from '$lib/legal/Fact.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';
</script>

<LegalPage
	title="Privacy Policy"
	intro="This policy covers the personal data {OPERATOR.serviceName} holds in its own right — accounts, authentication, platform billing, support correspondence and operational logs — and explains our separate role as a processor for the invoice, vendor and payment data our customers load into the service; if your details are in {OPERATOR.serviceName} because a company you supply put them there, section 3 is written for you and your first point of contact is that company, not us."
>
	<h2 id="who-we-are">1. Who we are</h2>

	<p>
		{OPERATOR.serviceName} is a business-to-business accounts-payable platform. Companies use it to
		receive supplier invoices, code and approve them, and pay them. It is operated by
		{OPERATOR.controllerDescription}, at
		<Fact value={OPERATOR.postalAddress} label="a postal address for the controller" />.
		{#if OPERATOR.legalEntity}
			The registered legal entity is {OPERATOR.legalEntity}.
		{:else}
			Once the business has a registered legal entity, it is named here:
			<Fact value={OPERATOR.legalEntity} label="the registered legal entity" />.
		{/if}
	</p>

	<p>
		For anything covered by this policy you can write to
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>. That address is monitored for
		data-protection enquiries, access requests and erasure requests. Security reports go to
		<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>, contract and DPA matters to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>, and everything else to
		<a href="mailto:{CONTACT.support}">{CONTACT.support}</a>.
	</p>

	<h3>Data Protection Officer</h3>

	{#if OPERATOR.dataProtectionOfficer}
		<p>
			We have appointed a Data Protection Officer under Article 37 of the GDPR. You may contact
			them directly, and you do not have to go through us first:
			{OPERATOR.dataProtectionOfficer.name}, {OPERATOR.dataProtectionOfficer.address},
			<a href="mailto:{OPERATOR.dataProtectionOfficer.email}"
				>{OPERATOR.dataProtectionOfficer.email}</a
			>.
		</p>
	{:else if OPERATOR.dataProtectionOfficer === false}
		<p>
			We have assessed whether Article 37(1) of the GDPR obliges us to appoint a Data Protection
			Officer and concluded that it does not: we are not a public authority, our core activities
			do not consist of large-scale regular and systematic monitoring of data subjects, and they
			do not consist of large-scale processing of Article 9 special-category or Article 10
			criminal-offence data. We keep that assessment under review and will appoint a DPO and name
			them here if it changes. In the meantime, data-protection questions go to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</p>
	{:else}
		<p>
			Whether we are required to appoint a Data Protection Officer is
			<Fact value={null} label="whether a Data Protection Officer is appointed" />. Until that is
			settled and published here, send data-protection questions to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>, which is monitored either way.
		</p>
	{/if}

	<h3>EU and UK representatives</h3>

	{#if OPERATOR.euRepresentative}
		<p>
			Our representative in the European Union under Article 27 of the GDPR is
			{OPERATOR.euRepresentative.name}, {OPERATOR.euRepresentative.address},
			<a href="mailto:{OPERATOR.euRepresentative.email}">{OPERATOR.euRepresentative.email}</a>.
			If you are in the EEA you may address any matter under this policy to them instead of to us.
		</p>
	{:else}
		<p>
			Our representative in the European Union under Article 27 of the GDPR is
			<Fact value={null} label="an EU representative (GDPR Art 27)" />. Until one is appointed and
			named here, EEA data subjects should write to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>, which we monitor and answer
			directly.
		</p>
	{/if}

	{#if OPERATOR.ukRepresentative}
		<p>
			Our representative in the United Kingdom under Article 27 of the UK GDPR is
			{OPERATOR.ukRepresentative.name}, {OPERATOR.ukRepresentative.address},
			<a href="mailto:{OPERATOR.ukRepresentative.email}">{OPERATOR.ukRepresentative.email}</a>.
		</p>
	{:else}
		<p>
			Our representative in the United Kingdom under Article 27 of the UK GDPR is
			<Fact value={null} label="a UK representative (UK GDPR Art 27)" />. Until one is appointed
			and named here, UK data subjects should write to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</p>
	{/if}

	<h2 id="roles">2. The two halves of this policy: where we are a controller and where we are a processor</h2>

	<p>
		Almost every complaint about a privacy policy for a platform like this one comes from blurring
		these two roles, so we will state the split before anything else. It decides who you talk to
		and who is accountable.
	</p>

	<p>
		<strong>We are a controller</strong> — we decide the purposes and means of the processing — for
		a narrow set of data: the accounts people sign in with and how that authentication works, our
		own billing and subscription records for the customer companies that pay us, the
		correspondence you send us, and our operational and security logs. Section 4 lists exactly what
		that is, section 5 gives the lawful basis for each purpose, and any right you exercise over it
		you exercise against us directly.
	</p>

	<p>
		<strong>We are a processor</strong> — we act on documented instructions and decide nothing
		ourselves — for everything a customer loads into their tenant: their vendor master, tax
		identifiers, payment instructions, invoices, payments, expenses, contracts, supplier-portal
		accounts and the audit trail over all of it. The customer company is the controller of that
		data. They decide what to collect, why, how long to keep it, who may see it, and whether to
		switch on any of the optional integrations described in section 8. Our obligations to them are
		contractual, in the <a href="/legal/dpa">Data Processing Addendum</a>, which is our Article 28
		agreement.
	</p>

	<p>
		One consequence matters more than the rest: <strong
			>for tenant data, we cannot act on your request by ourselves.</strong
		> If you ask us to delete a bank account number or correct an address that sits in a customer's
		tenant, we will not do it on our own initiative, because doing so would be processing outside the
		controller's instructions and would quietly alter a financial record they are legally required to
		keep. What we will do is described in section 12: identify the controller for you, pass the request
		on, and assist them in answering it, which is what Article 28(3)(e) requires of us.
	</p>

	<p>
		There is a shared edge worth naming. For the customer's own staff accounts, the customer
		decides who gets an account and what role they hold; we decide how authentication itself works
		— what security data exists, how a session record is shaped, how long it lives. Treat the
		account and its security record as ours and everything those users do inside the tenant as
		theirs.
	</p>

	<h2 id="suppliers">3. If you are a supplier: where your data came from, and what that means</h2>

	<p>
		If you are reading this because a company you invoice uses {OPERATOR.serviceName}, you are
		probably not our customer. Your name, business address, tax identifier and bank details reached
		us <em>from that company</em>, not from you. Under Article 14 of the GDPR — the article that
		governs personal data obtained from a source other than the data subject — the position is:
	</p>

	<ul>
		<li>
			<strong>Source of the data.</strong> Your personal data was provided to us by the customer
			company whose tenant holds it, or by you directly if you have a supplier-portal login and
			used it to submit an invoice, update your details or upload a tax form.
		</li>
		<li>
			<strong>Who owes you the notice.</strong> The customer is the controller, so the primary
			Article 14 notice — what they hold about you, why, for how long, and on what basis — is
			theirs to give, within one month of obtaining your data or at first communication with you,
			whichever comes first. Ask them for their privacy notice. This document tells you what
			happens on our side of that arrangement, and we publish it rather than relying on their
			notice to do our explaining for us.
		</li>
		<li>
			<strong>Why we have not written to you individually.</strong> We do not send Article 14
			notices to suppliers ourselves. Article 14(5)(a) disapplies the duty where the data subject
			already has the information, which is the ordinary case: your customer told you they use an
			AP platform when they onboarded you as a supplier. Article 14(5)(b) disapplies it where
			giving notice individually would involve disproportionate effort — we do not control
			whether a supplier record carries a working contact address, we have no relationship with
			you to authenticate a message against, and unsolicited mail quoting your bank details back
			at you is itself a fraud risk. In both cases Article 14(5)(b) requires the alternative of a
			publicly available notice, which is this page.
		</li>
		<li>
			<strong>What you can still do.</strong> Every right in section 12 is available to you. Send
			it to <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> and we will route it, or go
			straight to the company you supply, which will usually be faster.
		</li>
	</ul>

	<p>
		Categories of your data we receive, and from where, are in section 4. We do not buy supplier
		data, enrich it from data brokers by default, or build any profile of you that our customers
		did not ask for.
	</p>

	<h2 id="data-we-hold">4. What personal data we hold</h2>

	<h3>Held by us, as controller</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr>
					<th scope="col">Category</th>
					<th scope="col">What it includes</th>
					<th scope="col">Where it is held</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>Account identity</td>
					<td>Full name, email address, language preference.</td>
					<td>Control-plane database</td>
				</tr>
				<tr>
					<td>Authentication and security</td>
					<td>
						Password hash (never a recoverable password), TOTP multi-factor secret, WebAuthn
						passkey public keys, the subject identifier your employer's single sign-on provider
						asserts about you, and digests of any API keys you create.
					</td>
					<td>Control-plane database</td>
				</tr>
				<tr>
					<td>Session records</td>
					<td>
						The IP address a session signed in from, a short device description derived from
						your browser's user-agent string and truncated, and the sign-in time — so you can
						see your own live sessions and end one you do not recognise.
					</td>
					<td>
						Redis only, never a database column. Each record expires with the access token it
						describes and is destroyed on logout, revocation or expiry.
					</td>
				</tr>
				<tr>
					<td>Notification settings</td>
					<td>Your in-app and email notification preferences, and mobile push device tokens if you use the mobile app.</td>
					<td>Control-plane database</td>
				</tr>
				<tr>
					<td>Billing and subscription</td>
					<td>The plan and subscription attached to a customer account, and the usage counts metered against it.</td>
					<td>Control-plane database</td>
				</tr>
				<tr>
					<td>Correspondence</td>
					<td>Whatever you write to one of our contact addresses, and our replies.</td>
					<td>Our mailboxes</td>
				</tr>
				<tr>
					<td>Operational logs</td>
					<td>
						Application and error logs used to keep the service running. Bank account numbers,
						tax identifiers, full vendor addresses and payment-instrument numbers are kept out
						of them by design, and out of error responses and URLs.
					</td>
					<td>Our logging infrastructure</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3>Held in a customer's tenant, where we are the processor</h3>

	<p>
		Each customer has its own database, separate from every other customer's, plus its own
		object-storage area for uploaded files. Those hold:
	</p>

	<ul>
		<li>Vendor master records: legal name, postal address, phone number, email address.</li>
		<li>
			Tax identifiers: EIN, VAT registration numbers, a Social Security Number where a sole
			proprietor uses one as their taxpayer identifier, and uploaded W-9 and W-8 forms.
		</li>
		<li>Payment instructions: bank account number, routing number, IBAN and the account-holder name.</li>
		<li>Beneficial-ownership and know-your-customer information collected for screening.</li>
		<li>
			Invoices and their line items, purchase orders, goods receipts, credit memos, payments,
			payment runs and virtual cards.
		</li>
		<li>Expenses, expense reports and receipts; contracts and contract documents.</li>
		<li>Supplier-portal logins: name, email, password hash and multi-factor enrolment.</li>
		<li>Supplier chat messages and their attachments.</li>
		<li>
			The audit log: who did what, when, and to which record. For ordinary business fields it
			records the before and after values — that is what makes the trail useful — and for the
			restricted ones, bank details and tax identifiers, only the last four digits. It is
			append-only: a database trigger rejects every row delete, and every edit except the stamp
			recording that a row has been copied to write-once storage.
		</li>
		<li>
			Documents in object storage: invoice PDFs, expense receipts, contract documents, W-9 and W-8
			forms, and chat attachments.
		</li>
	</ul>

	<h3>A specific note on bank details</h3>

	<p>
		A supplier's payment instructions — account number, routing number or IBAN — are stored in the
		vendor record inside the customer's tenant database, because the platform has to be able to pay
		that supplier. Four things are true of them, and we would rather state each precisely than
		imply a protection we do not have:
	</p>

	<ul>
		<li>
			Those raw values never enter the audit trail, application logs or an error response. Where
			the audit trail has to record that a bank detail changed, it records the last four digits of
			the old and new value and nothing more.
		</li>
		<li>
			Changing them is dual-control. A change requested through the supplier portal, or by a
			member of the customer's AP staff, is <em>staged</em> rather than applied: it becomes a
			pending change request that a second, different person at the customer must approve. This
			is the specific defence against business-email-compromise fraud, where an attacker
			impersonates a supplier to redirect a payment.
		</li>
		<li>
			<strong>Positive Pay files hold full numbers, and the files are kept.</strong> A Positive
			Pay file is a fraud-control file handed to a bank, so writing full numbers into it is the
			point of it. The database record of the file keeps only the last four digits of the
			customer's own account, but the file itself is stored in the customer's object storage and
			stays there until someone deletes it by hand — no retention timer covers it. One of the two
			kinds, the ACH debit-authorization file, lists every active supplier's full routing and
			account number, not only the customer's own account.
		</li>
		<li>
			<strong>Two screens return the full value, and both are deliberate.</strong> The first is
			the dual-control review above: the second person has to be able to read the account number
			they are approving, so the detail view shows it. The second is the data-subject export in
			section 12 — run by a customer's administrator, it carries the supplier's bank details and
			beneficial-ownership data unmasked, because Article 15 is a right to the data and not to a
			masked copy of it; the bundle goes to whoever requested it and is neither stored nor logged.
			Everywhere else — every list, every other screen, every API response and the audit trail —
			shows the last four digits only.
		</li>
	</ul>

	<p>
		We do not knowingly ask for, and our customers should not load, Article 9 special-category data
		(health, biometrics, religious or political views, trade-union membership, sexual life) or
		Article 10 criminal-offence data. Invoices and receipts are free-form documents, so such data
		can arrive incidentally; where it does, the customer remains its controller and the
		<a href="/legal/dpa">DPA</a> governs it.
	</p>

	<h2 id="lawful-basis">5. Why we process it, and on what lawful basis</h2>

	<p>
		Each purpose below has exactly one lawful basis under Article 6 of the GDPR. This table covers
		only the processing where <strong>we</strong> are the controller.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr>
					<th scope="col">Purpose</th>
					<th scope="col">Lawful basis</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td>Creating your account, signing you in, and making the service work for you.</td>
					<td>
						Performance of a contract, Article 6(1)(b) — the contract with the customer company
						you are a user of, or with you directly if you signed up.
					</td>
				</tr>
				<tr>
					<td>
						Keeping accounts secure: multi-factor authentication, passkeys, session records you
						can review and revoke, rate limiting, and the captcha on the public signup form.
					</td>
					<td>
						Legitimate interests, Article 6(1)(f) — our interest, and yours, in accounts that
						cannot be taken over. A platform holding supplier bank details is a fraud target,
						and we consider this to be processing you would reasonably expect.
					</td>
				</tr>
				<tr>
					<td>Billing a customer for their subscription and metering their usage against it.</td>
					<td>Performance of a contract, Article 6(1)(b).</td>
				</tr>
				<tr>
					<td>Keeping our own accounting and tax records of what we invoiced and were paid.</td>
					<td>Legal obligation, Article 6(1)(c).</td>
				</tr>
				<tr>
					<td>Answering your support, security or legal correspondence.</td>
					<td>
						Legitimate interests, Article 6(1)(f) — answering someone who wrote to us; or
						Article 6(1)(b) where the correspondence is about the contract itself.
					</td>
				</tr>
				<tr>
					<td>
						Sending service notifications you have configured, and operational notices about
						the service.
					</td>
					<td>
						Performance of a contract, Article 6(1)(b). Marketing email is a separate matter and
						we do not send it on this basis.
					</td>
				</tr>
				<tr>
					<td>
						Running, debugging and securing the platform, including operational and error logs.
					</td>
					<td>Legitimate interests, Article 6(1)(f) — operating a reliable, secure service.</td>
				</tr>
				<tr>
					<td>Answering a data-protection request and keeping a record that we answered it.</td>
					<td>
						Legal obligation, Article 6(1)(c) — the record we keep is the non-identifying fact
						that a request was made and completed, never the contents of the response.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

		<p>
		<strong>Whether you have to give us this data.</strong> For an account, the requirement is
		contractual rather than statutory: a name, an email address and a means of signing in — a
		password, or the identity your employer's single sign-on asserts — are what an account is made
		of, and without them we cannot create one. That is the whole consequence of declining: no
		account, and nothing else follows from it. If you are a supplier, the requirement is your
		customer's rather than ours — they need your payment and tax details to pay you and to meet
		their own tax obligations — and section 3 explains who to ask about it.
	</p>

	<p>
		Where we are a processor, the lawful basis is the customer's to identify and to be able to
		demonstrate. In practice AP data is processed on the customer's performance of its contract
		with its supplier (Article 6(1)(b)), its legal obligations for tax, accounting and sanctions
		compliance (Article 6(1)(c)), and its legitimate interests in preventing payment fraud (Article
		6(1)(f)). We process it only on their instructions.
	</p>

	<h2 id="screening">6. Sanctions and financial-crime screening</h2>

	<p>
		Before a supplier is paid, the platform can screen them against sanctions, politically-exposed-person
		and adverse-media sources. Where a customer is subject to sanctions law — which most are — this
		is processing necessary for compliance with a legal obligation (Article 6(1)(c)); where they
		run it beyond what the law strictly compels, it rests on their legitimate interests in not
		paying a sanctioned counterparty (Article 6(1)(f)). Either way the customer is the controller
		of that decision, and screening is performed by whichever provider they have configured.
	</p>

	<p>
		A screening result is one of three things, and none of them is a final answer produced by a
		machine:
	</p>

	<ul>
		<li><strong>Clear</strong> — the payment proceeds.</li>
		<li>
			<strong>Match</strong> — the payment is refused and an exception is raised for a person at
			the customer to work.
		</li>
		<li>
			<strong>Review required</strong> — the payment is held and the vendor enters a review queue
			for a human to assess.
		</li>
	</ul>

	<p>
		Screening providers return free-form match details that can include dates of birth, passport
		numbers and addresses. We keep that response so a reviewer can cite the source of a hit and so
		the decision can be audited, and we never echo it into logs or an HTTP response. A match is
		often a name collision rather than a real hit; if you believe a payment to you has been held
		because of a false match, raise it with the company that owes you the money — they hold the
		review queue and the decision — and we will assist them in resolving it.
	</p>

	<h2 id="ai-and-automated-decisions">7. Artificial intelligence and automated decision-making</h2>

	<p>
		The platform applies AI to customer documents in four places: extracting fields from an
		uploaded invoice, a conversational assistant that answers questions about a customer's own AP
		data, a cash-flow copilot that drafts plans and payment runs, and autonomous agents that triage
		the exception queue. Three of those four run against a <em>local, offline default</em> and reach
		an external provider only if the customer switches one on. <strong
			>Invoice extraction is the exception, and it is the one that handles whole documents.</strong
		>
		On any deployed instance it sends the uploaded invoice — every field on it, including anything
		personal a supplier printed there — to Anthropic, whether or not anyone configured a provider.
		That is deliberate: the offline stand-in returns a fabricated invoice, and putting invented
		figures against a real supplier's document is worse than calling out. So Anthropic is a
		sub-processor by default, not by choice, and we say so here rather than in a footnote. See
		section 8 and the <a href="/legal/sub-processors">sub-processor register</a>.
	</p>

	<p>
		Article 22 of the GDPR gives you a right not to be subject to a decision based solely on
		automated processing which produces legal effects concerning you or similarly significantly
		affects you. We have looked at each path against the code rather than describing an intention:
	</p>

	<ul>
		<li>
			<strong>Invoice approval is a human act by default.</strong> An invoice moves to approved
			when a named person with the right role approves it, and that person is recorded on the
			transition. Approval is also constrained by segregation of duties: whoever created the
			invoice — and anyone who materially shaped the template behind a recurring one — is refused
			as its approver.
		</li>
		<li>
			<strong>There is an opt-in auto-approval path, and we will not pretend otherwise.</strong> A
			customer can enable auto-approval on the extraction step. It is <em>off by default</em>.
			When a customer turns it on, an invoice can be approved without human review if the
			extraction confidence clears a threshold they set, or if the amount falls below a floor they
			set. Even then the same money controls apply as to a human: an invoice over the customer's
			maximum invoice amount, or over the threshold at which a CFO must sign, falls back to human
			review rather than approving past the control.
		</li>
		<li>
			<strong>Exception agents escalate by default.</strong> The default autonomy level is
			"conservative", implemented as a confidence threshold no evaluation can ever clear — so
			every exception escalates to a person. A customer who raises the level lets an agent
			resolve a narrow set of cases (a small purchase-order amount variance, linking a missing
			purchase order, matching a consolidated invoice to the set of purchase orders that sums to
			its total, correcting a GL code from vendor history). Each agent decision is written to
			an append-only decision log with the confidence and a rationale, alongside the ordinary
			audit row for whatever it changed, and the agent inherits the same segregation-of-duties
			refusal a human would face and escalates rather than acting.
		</li>
		<li>
			<strong>The assistant and the cash-flow copilot propose; they do not execute.</strong> They
			answer questions and draft payment runs. A person still approves and releases the money.
		</li>
		<li>
			<strong>Screening.</strong> A match or review result holds a payment. That can plainly
			affect a supplier, which is why section 6 describes the human review queue behind it.
		</li>
	</ul>

	<p>
		Because each of these settings belongs to the customer, they are the controller of any decision
		reached under them, and the Article 22 safeguards — obtaining human intervention, expressing
		your point of view, contesting the decision — are exercised against that customer. Write to
		them; write to <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> if you do not know who
		they are or cannot reach them, and we will help. We do not use AI to make any decision about
		you in our own controller capacity.
	</p>

	<p>
		<strong>The EU AI Act.</strong> Regulation (EU) 2024/1689 is the other instrument that bears
		on this section, so we will say where we stand rather than leave it unmentioned. We do not
		train or fine-tune a model: the AI here is a general-purpose model called through an API, with
		deterministic code around it. The transparency the Act asks for is what this section is — where
		AI is applied, what it reads, and what a person still decides. A customer who turns on
		auto-approval or raises an agent's autonomy is the deployer of that behaviour and carries a
		deployer's obligations; we are the provider of the software they deploy. We are not aware of a
		use here that falls into the Act's high-risk categories, and if that assessment changes we will
		say so on this page rather than quietly.
	</p>

	<h2 id="recipients">8. Who else can receive personal data</h2>

	<p>
		<strong>Three third parties are involved before anyone configures anything</strong>: the
		infrastructure provider that hosts the service (AWS), <strong>Anthropic</strong>, which
		receives uploaded invoice documents for extraction on any deployed instance (section 7
		explains why), and <strong>hCaptcha</strong>, which receives the IP address of anyone using the
		public signup form — the service refuses to start without it.
	</p>

	<p>
		Beyond those three, every external integration — ERP sync, payment rails, card issuing,
		sanctions screening, e-invoicing, email delivery, chat notifications, audit-log shipping, FX
		rates, data enrichment, platform billing — sits behind a pluggable adapter whose default is a
		local mock or console implementation. Those receive data only when a customer deliberately
		configures a real provider, which means <strong
			>which of them apply to your data depends on what your customer switched on.</strong
		>
	</p>

	<p>
		Rather than list providers here, where the list would go stale, we keep one register:
		<a href="/legal/sub-processors">the sub-processor register</a>. It names every third party that
		can receive customer personal data, what each receives, which are engaged today, and how
		changes are notified. On our own controller side the recipients are the infrastructure provider
		that hosts the service; the anti-abuse captcha provider, which sees your IP address and a
		challenge token when you submit the public signup form; whichever provider a deployment sends
		account and service email through; and, once we bill through a payment provider rather than
		our own records, that provider, which receives the customer organisation's name, a billing
		contact email address and the plan. Every one of them is in the register.
	</p>

	<p>
		We also disclose personal data where we are legally compelled to — a binding court order, a
		lawful request from a regulator or law-enforcement body — and, if the business is ever sold or
		merged, to the acquirer under the same obligations. We tell the affected controller before
		complying with a compelled disclosure unless we are prohibited by law from doing so. We do not
		sell personal data, and we do not disclose it for anyone else's marketing.
	</p>

	<h2 id="transfers">9. International transfers</h2>

	<p>
		{OPERATOR.serviceName} is built and operated as a single-region deployment. All customer data
		is held in one hosting region:
		<Fact value={OPERATOR.hostingRegion} label="the hosting region for customer data" />. If you
		are in the EEA or the UK and that region is outside it, your data is transferred out of the
		EEA or the UK by our holding it there, and the transfer relies on the European Commission's
		Standard Contractual Clauses (Decision 2021/914), with the UK Information Commissioner's
		International Data Transfer Addendum applied for UK transfers, or on an adequacy decision
		where one covers the destination. A transfer out of Switzerland relies on the same Clauses
		with the amendments the Swiss Federal Data Protection and Information Commissioner
		recognises. The equivalent terms for customer data, those Swiss amendments included, are in
		the <a href="/legal/dpa">Data Processing Addendum</a>. The same
		mechanisms are flowed down to every sub-processor in the
		<a href="/legal/sub-processors">register</a>, together with the transfer-impact assessment
		obligations that follow from <em>Schrems II</em>.
	</p>

	<p>
		<strong>How to get a copy of the safeguards.</strong> Naming a safeguard is not the same as
		giving you access to it, and Article 13(1)(f) asks for both: the safeguard, and the means of
		obtaining a copy of it. Write to <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> and
		we will send you the clauses relied on for the transfer that affects you. They are also
		published independently of us — the Standard Contractual Clauses by the European Commission
		as the annex to Decision 2021/914, and the International Data Transfer Addendum by the UK
		Information Commissioner's Office — and the modules, options and completed annexes we have
		selected are in the <a href="/legal/dpa">Data Processing Addendum</a>.
	</p>

	<p>
		<strong>About the residency setting.</strong> The application exposes a per-organisation data-residency
		option (US, EU, UK, Canada, Australia). We want to be exact about what it does today: it is
		<strong>advisory only</strong>. Nothing in the code routes a database or an object-storage
		bucket on the strength of it. What the platform does is compare the region a tenant has
		configured against the region the operator has <em>declared</em> in its own configuration —
		a declaration, not an observation of where the servers are — and report that comparison as
		aligned, misaligned, or unknown where nothing has been declared. All data currently lives in
		the one region named above. Setting it does not create a residency guarantee and you should not rely
		on it as one. If you need a genuine regional commitment, ask us at
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a> before you sign, and do not infer one from
		the setting existing.
	</p>

	<h2 id="retention">10. How long data is kept</h2>

	<p>
		We would rather publish what the system does than a schedule it does not enforce. There is a
		retention sweep in the platform, and its true scope is narrow:
	</p>

	<ul>
		<li>
			It is <strong>off by default</strong> and each customer configures whether it runs and over
			what window.
		</li>
		<li>
			It covers exactly two record classes: invoices and the audit log. Nothing else has an
			automated retention timer.
		</li>
		<li>
			For invoices it <strong>soft-archives</strong>: an invoice in a terminal state, older than
			the customer's window, gets an archive marker stamped on it. It deletes nothing, and the
			action is reversible.
		</li>
		<li>
			For the audit log, "retention" means verifying that rows past the window have been shipped
			to write-once storage and recording a manifest of counts. <strong
				>Audit rows are not deletable through the product</strong
			> — a row-level trigger in each customer's database rejects every delete, and every edit except
			the stamp recording that a row has been shipped — because an audit trail that can be pruned
			is not an audit trail. One limit, stated because a trigger is not magic: it fires row by row,
			so a single statement that empties the whole table at once falls outside it, and an account
			holding database-owner rights could switch the trigger off in any case. It stops every path
			the application has and every ordinary query; it does not stop the owner of the database it
			runs in.
		</li>
	</ul>

	<p>
		Everything else — vendors, payments, expenses, contracts, documents in object storage, and the
		accounts of the people who use the service — persists until the customer deletes it, exercises
		an erasure on our behalf, or the contract ends and we return or delete tenant data under the
		<a href="/legal/dpa">DPA</a>. Financial records are typically kept for years because tax and
		accounting law requires it, which is also why erasure preserves the money trail (section 12).
	</p>

	<p>
		On our own controller side: your account record lives for as long as the account does; session
		records expire with the access token, which is a matter of minutes to hours, not months;
		billing records are kept for the period our own tax law requires; correspondence is kept for as
		long as it is useful to the matter it concerns and then deleted.
	</p>

	<h2 id="security">11. How data is protected</h2>

	<p>These are the controls the service is built and operated with:</p>

	<ul>
		<li>
			<strong>Passwords are never stored in a recoverable form.</strong> A password is pre-hashed
			with HMAC-SHA256 and then hashed with bcrypt, in a single shared implementation used
			everywhere a password is checked.
		</li>
		<li>
			<strong>Multi-factor authentication</strong> via time-based one-time codes and WebAuthn
			passkeys, with step-up re-authentication required to change a security factor.
		</li>
		<li>
			<strong>Role-based access control plus a granular segregation-of-duties layer</strong>, so
			fraud-sensitive duties — approving an invoice, approving or executing a payment run,
			approving a supplier bank-detail change, blocking a vendor, managing users — can be split
			between different people and cannot be self-granted.
		</li>
		<li>
			<strong>Append-only audit logging</strong>, enforced by a database trigger rather than by
			convention. For ordinary business fields an entry records the before and after values,
			because that is what makes the trail useful to an auditor; for the restricted ones — bank
			details and tax identifiers — it records only the last four digits.
		</li>
		<li>
			<strong>Tenant isolation at the data layer.</strong> Each customer has its own database, and
			every request resolves its tenant through a single chokepoint that cross-checks the
			organisation claim inside your signed session token against the tenant being requested — so
			a forged or swapped tenant header alone cannot widen access.
		</li>
		<li>
			<strong>Secrets held outside the application repository</strong>, encrypted with sops under
			an AWS KMS key, so no credential lives in the source code.
		</li>
		<li>
			<strong>PII and banking data kept out of logs, error responses and URLs</strong> as a
			standing engineering rule with automated checks against it.
		</li>
	</ul>

	<p>
		Two honest limits. First, <strong
			>we hold no SOC 2 report and no ISO 27001 certificate.</strong
		> SOC 2 Type II readiness is something we are working toward; it is an intention, not a status,
		and nothing in this policy should be read as a certification. Second, this document describes how
		the service is designed and operated rather than certifying the configuration of any particular
		deployment — if you need the transport-security and storage-encryption settings of a specific environment
		confirmed in writing, ask <a href="mailto:{CONTACT.security}">{CONTACT.security}</a> and we will
		tell you what is configured for it.
	</p>

	<h2 id="your-rights">12. Your rights and how to exercise them</h2>

	<p>
		Under the GDPR and the UK GDPR you have the right to: obtain confirmation of whether we process
		your data and a copy of it (Article 15); have inaccurate data corrected and incomplete data
		completed (Article 16); have data erased in the circumstances Article 17 allows; restrict
		processing (Article 18); receive data you gave us in a portable, machine-readable form and have
		it transmitted to another controller where technically feasible (Article 20); object to
		processing based on legitimate interests, on grounds relating to your particular situation
		(Article 21); and not be subject to a solely automated decision with legal or similarly
		significant effects (Article 22, and section 7 above). Where processing rests on consent, you
		may withdraw it at any time (Article 7(3)) without affecting the lawfulness of what was done
		before. We are also obliged to notify each recipient of a rectification, erasure or
		restriction, unless that proves impossible or disproportionate (Article 19).
	</p>

	<h3>How to make a request</h3>

	<p>
		Email <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>. Tell us what you want, and give
		us enough to find you — the email address you use to sign in, or, if you are a supplier, the
		name of the company you invoice and the name your supplier record is under. Do not send us your
		bank details or a tax identifier to prove who you are; we will tell you what we need.
	</p>

	<p>
		<strong>We answer within one month</strong> of receiving the request, as Article 12(3) requires.
		Where a request is complex or you have made several, we may extend by up to two further months,
		and we will tell you within the first month if we do, with the reason. There is no charge unless
		a request is manifestly unfounded or excessive, in which case we will say so and why rather than
		simply not replying.
	</p>

	<p>
		<strong>If your request concerns data in a customer's tenant</strong> — which is the case for
		almost every supplier — we act on the customer's instruction, not our own. We will identify the
		controller, pass your request to them, tell you we have done so, and assist them in answering
		it. You can also approach them directly, and that is usually faster.
	</p>

	<h3>What the automated tools do, and what they do not yet do</h3>

	<p>
		The platform gives a customer's administrator two tools: an export that assembles the record
		held about a data subject, and an erasure that redacts identifying fields. Both cover three
		kinds of subject — an employee user, a supplier-portal user, and a vendor contact. Only the
		employee user exists in both databases, so only that subject's export and erasure reach the
		control-plane database as well as the customer's tenant; a supplier-portal user and a vendor
		contact live entirely inside the tenant.
	</p>

	<p>
		Erasure redacts contact details, tax identifiers, bank details and beneficial-ownership data,
		nulls credentials and deactivates the account, and redacts the supplier's own chat message
		bodies. It deliberately
		<strong>preserves the money trail and the audit log</strong> — amounts, currencies, statuses,
		dates and the vendor's legal name on each invoice — because those are records the customer is
		legally required to keep, and because an audit trail with the actor removed would defeat its
		purpose. It also leaves the supplier company's own business identity alone: its legal name and
		the public company website recorded against it describe the company rather than a person, and
		erasing a contact is not the same as erasing the company they work for.
	</p>

	<p>
		Two limits remain, and we are not going to describe around them:
	</p>

	<ul>
		<li>
			<strong>The export lists your documents rather than attaching them.</strong> Invoice PDFs,
			receipts, contract documents, W-9 and W-8 forms and chat attachments are enumerated in the
			bundle — each one named, with the record it belongs to and what an erasure would do to it —
			but the bundle carries references, not the files. Putting the bytes inline would write a
			scanned W-9 bearing your taxpayer identifier into the body of a routine export. Each
			document stays retrievable through the download route that already checks who is asking.
		</li>
		<li>
			<strong>Two things are deliberately withheld or capped.</strong> Where an audit entry was
			written by someone else about their own activity, the entry is listed without its detail,
			because that detail carries another person's identifiers and Article 15(4) does not let
			your copy override their rights. And a very large collection is cut at one thousand entries,
			with the true total stated beside it, so you can see what was trimmed and ask for the rest.
		</li>
	</ul>

	<p>
		Erasure also keeps some documents on purpose, which is not a gap but a choice worth stating
		plainly. It deletes the ones that are yours alone — your signed W-9 or W-8, the attachments you
		sent through the supplier portal, and the payment-instruction files that carry account numbers
		in the clear. It keeps the ones that are evidence behind a financial record it is already
		required to preserve: the invoice PDF, which <em>is</em> the invoice; the contract that
		authorised the spend; the receipt behind a reimbursement that was paid. Destroying those would
		leave a preserved money trail with its supporting evidence gone, which is the outcome the
		record-keeping rule exists to prevent. Erasure of a user account also removes any registered
		passkey and ends every live session.
	</p>

	<p>
		If a request needs something outside that, say so and we will confirm what was done for each
		part of it.
	</p>

<h2 id="complaints">13. Complaining to a supervisory authority</h2>

	<p>
		If you think we have handled your personal data unlawfully, you can complain to a data
		protection supervisory authority. You may complain in the EU Member State of your habitual
		residence, your place of work, or the place of the alleged infringement (Article 77 GDPR); in
		the UK the authority is the Information Commissioner's Office, at ico.org.uk. Our lead
		supervisory authority is
		<Fact
			value={OPERATOR.supervisoryAuthority}
			label="the lead supervisory authority for complaints"
		/>. You also have a right to an effective judicial remedy (Articles 78 and 79) and to
		compensation for damage caused by an infringement (Article 82).
	</p>

	<p>
		We would rather hear from you first, at
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>, but nothing in this policy requires
		you to contact us before going to a regulator and nothing here limits that right.
	</p>

	<h2 id="children">14. Children</h2>

	<p>
		{OPERATOR.serviceName} is a business product. Every account is created either by an organisation
		for its own finance staff, or by a supplier company for the people who submit its invoices. The
		service is not directed to children, we do not market it to them, and it has no feature a child
		would have reason to use. We state this as a fact about the business rather than as a technical
		control: there is no age-gate in the product, because there is no consumer signup path for one
		to sit on. If you believe a child's personal data has reached us, write to
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> and we will delete it, working with the
		relevant customer where the data sits in their tenant.
	</p>

	<h2 id="cookies">15. Cookies and browser storage</h2>

	<p>
		The application sets <strong>no cookies</strong>. It uses your browser's local and session
		storage for a small number of things it cannot work without — your sign-in token, the supplier
		portal's separate token, which legal entity you have selected, your language, a short-lived
		multi-factor challenge, and the record of the storage choice you made in the consent banner
		itself. There is no analytics script, no tracking pixel and no advertising technology anywhere
		in the application; we do not measure you.
	</p>

	<p>
		The full itemised list — every key, what it is for, how long it lasts, and which are optional —
		is in the <a href="/legal/cookies">Cookie Notice</a>.
	</p>

	<h2 id="breach">16. Personal-data breaches</h2>

	<p>
		We maintain a documented incident runbook built around the statutory clock in Article 33: where
		we are the controller, a notifiable breach is reported to the supervisory authority without
		undue delay and, where feasible, within 72 hours of becoming aware of it, and affected
		individuals are told without undue delay where Article 34 requires it. Where we are the
		processor, we notify the affected customer — the controller — without undue delay after
		becoming aware, with the information they need to make their own notification, and we assist
		them in making it. We do not offer a shorter contractual deadline than that here; if you need
		one, negotiate it in the <a href="/legal/dpa">DPA</a>.
	</p>

	<p>
		If you find a vulnerability, report it to
		<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>. We will not pursue anyone who
		reports a genuine issue in good faith and does not access or exfiltrate other people's data.
	</p>

	<h2 id="us-state-rights">17. California and other US state privacy rights</h2>

	<p>
		This section is for residents of US states with a comprehensive privacy law — California
		(CCPA as amended by the CPRA), and on materially similar terms Virginia, Colorado, Connecticut,
		Utah, Texas and the other states whose statutes are now in force.
	</p>

	<p>
		<strong>Our role.</strong> For personal information inside a customer's tenant, we are a
		<em>service provider</em> (California) or <em>processor</em> (the other states) to that
		customer, which is the business or controller. We process it only to perform the service under
		our written contract, and we are prohibited by that contract from retaining, using or
		disclosing it for any other purpose, from selling or sharing it, and from combining it with
		personal information from another source except as the statute permits. Direct a request about
		tenant data to the business; section 12 explains how we help.
	</p>

	<p>
		<strong>Categories we collect as a business in our own right</strong> (using the California
		statutory categories): identifiers (name, email address, account identifier, IP address);
		commercial information (the subscription and what was billed); internet or other electronic
		network activity limited to the sign-in IP and a coarse device description on a session record;
		professional or employment-related information, to the extent your role at your employer is
		implied by your account. We collect these for the purposes in section 5 and keep them for the
		periods in section 10. Sensitive personal information reaches us in the processor role — account
		log-in credentials, financial account numbers, and a Social Security Number where a sole
		proprietor's taxpayer identifier is one — and we use it only to perform the service, which is a
		purpose permitted by section 1798.121(a), so the right to limit its use does not produce a
		different outcome; we will still honour a request and tell you what we did.
	</p>

	<p>
		<strong>We do not sell personal information and we do not share it for cross-context behavioural
		advertising</strong>, as those terms are defined in the CCPA. We have never done so. There is no
		analytics, advertising or tracking technology in the product, which is what makes that statement
		verifiable rather than merely asserted. We therefore have no "Do Not Sell or Share My Personal
		Information" link, because there is nothing for it to opt you out of, and we do not process
		personal information for targeted advertising or for profiling with legal or similarly
		significant effects in our own right.
	</p>

	<p>
		<strong>Your rights.</strong> You may request to know what personal information we have
		collected about you, the categories of sources, the business purpose, and the categories of
		third parties it was disclosed to; request a copy of it in a portable form; request correction
		of inaccurate personal information; and request deletion, subject to the exceptions the statute
		allows — most relevantly where we must keep a record to complete a transaction, detect security
		incidents, or comply with a legal obligation such as tax record-keeping. Send a request to
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>. We will verify it against the
		information we already hold, and we will respond within the 45 days the CCPA allows, extendable
		once by a further 45 days where reasonably necessary, and within the equivalent periods in the
		other states. An authorised agent may act for you with written permission we can verify.
	</p>

	<p>
		<strong>Non-discrimination.</strong> We will not deny you a service, charge you a different
		price, give you a different level of quality, or retaliate in any way because you exercised a
		privacy right. We operate no financial-incentive programme in exchange for personal
		information. Where a state gives you a right to appeal a refused request — Virginia, Colorado,
		Connecticut and Texas do — reply to our decision at the same address and we will reconsider it
		and explain the outcome in writing; if we refuse again, your state's Attorney General can take
		a complaint.
	</p>

	<h2 id="other-jurisdictions">18. South Africa and other jurisdictions</h2>

	<p>
		This policy is written against the GDPR, the UK GDPR and the US state statutes in section 17,
		because those are the regimes the product was built to serve. Nothing in the product confines
		a customer to those countries — the only country restrictions we operate are the
		export-control and sanctions limits in the <a href="/legal/terms">Terms</a> — so it is worth
		saying plainly where this document stops rather than leaving it to be assumed.
	</p>

	<p>
		<strong>South Africa.</strong> POPIA uses different words for the same split section 2 of this
		policy describes: a customer is the <em>responsible party</em> for the data in its tenant and we
		are its <em>operator</em>. Sections 20 and 21 of POPIA require that relationship to be in writing,
		with the operator processing only with the responsible party's authorisation and keeping what it
		processes confidential and secure — obligations the <a href="/legal/dpa">DPA</a> already
		carries, whatever law is named in it. Two things follow that a South African customer should
		settle before loading data rather than after. First, <strong
			>there is no South African hosting region.</strong
		> The residency options are the United States, the EU, the UK, Canada and Australia, and section
		9 says where data actually sits — so putting supplier data here is a transfer out of South Africa,
		and POPIA's section 72 makes the basis for that transfer the responsible party's to establish and
		record.
		Second, whether POPIA also reaches <em>our own</em> processing, where we are the responsible party
		rather than the operator, turns on where this business is established — one of the facts still
		to be confirmed at the top of this page. We would rather answer that when the entity is registered
		than guess at it now, and in the meantime we make no claim to have registered an information officer
		with the Regulator, because we have not. A South African data subject may complain to the Information
		Regulator, and nothing in this policy affects that.
	</p>

	<p>
		<strong>Anywhere else.</strong> If you think a law we have not named applies to the data you are
		about to load, raise it at <a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a> before you sign
		rather than afterwards. We would rather negotiate a term we can keep than have you discover, at
		a regulator's deadline, that you were relying on one we never made.
	</p>

	<h2 id="changes">19. Changes to this policy</h2>

	<p>
		When we change this policy we update the "Last updated" date at the top of this page, which is
		the authoritative marker of the version you are reading. For a change that materially affects
		how we handle personal data, we will additionally email the administrator of each affected
		customer account, and show a notice in the application, before the change takes effect. We do
		not make a material change retroactive.
	</p>

	<p>
		If you are a supplier with no account of your own, this page is where the current version lives
		— we have no way to email you about it, for the reasons in section 3, and a bookmark is the
		reliable route.
	</p>

	<h2 id="contact">20. How to contact us</h2>

	<p>
		Write to <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> for anything in this policy:
		a question, an access or erasure request, a correction, an objection, or a complaint. Postal
		mail reaches
		{OPERATOR.controllerDescription} at
		<Fact value={OPERATOR.postalAddress} label="a postal address for the controller" />.
	</p>

	<p>
		The rest of the document set is linked below. The one most likely to be what you actually want
		is the <a href="/legal/dpa">Data Processing Addendum</a> if you are a customer deciding whether
		to load supplier data into the service, and the
		<a href="/legal/sub-processors">sub-processor register</a> if you need to know which third
		parties can see it.
	</p>
</LegalPage>
