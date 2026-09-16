<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import Fact from '$lib/legal/Fact.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';

	/**
	 * The public sub-processor register.
	 *
	 * The editorial decision this page turns on: almost every external
	 * integration in this product is an opt-in adapter that defaults to a local
	 * mock and contacts nobody until it is configured with a real credential. A
	 * register that listed them all as active would tell a customer's DPO to
	 * record vendors that are not processing their data; one that omitted them
	 * would hide what a customer can switch on. So the page carries both lists,
	 * separated, with the mechanic explained — see § 1.
	 *
	 * Extraction is the one family that does NOT follow that rule
	 * (`services/extraction.py::resolve_platform_provider`: a deployed instance
	 * resolves to `claude_vision`, key or no key, because the mock fabricates
	 * invoice fields). It sends the whole document, so it is the single most
	 * consequential sentence on the page — it is stated in § 2, in the
	 * engaged-today list, and § 1 says outright that it is the exception.
	 *
	 * Every "Data it receives" cell states the fields the outbound call
	 * actually carries, read off the adapter that builds it. That specificity
	 * is the whole value of the register, and it is also the thing most likely
	 * to rot: a change to an adapter's request body changes this page in the
	 * same commit, and adds a § 10 change-log row.
	 */
</script>

<LegalPage
	title="Sub-processors"
	intro="Every third party that can receive personal data through {OPERATOR.serviceName}, what each one receives, and — the part that matters most — which are engaged today versus which stay dormant until you switch them on."
>
	<h2 id="how-this-works">1. How sub-processors work here</h2>

	<p>
		When your organisation uses {OPERATOR.serviceName}, you are the controller of
		the supplier, invoice and payment data in your account and we are your
		processor. A <strong>sub-processor</strong> is a third party we engage that
		can process that data in the course of providing the service. This page is
		the register of those parties, and it is the list the
		<a href="/legal/dpa">Data Processing Addendum</a> points at when it grants a
		general authorisation to engage sub-processors.
	</p>

	<p>
		Nearly every external integration in this product is a pluggable adapter
		with a local, in-process default. ERP posting, payments, cards,
		screening, tax filing, currency rates, email, chat notifications, vendor
		enrichment and e-invoicing all ship pointed at a mock or console
		implementation that computes an answer on our own machines and sends
		nothing anywhere. A real provider becomes reachable only once a live
		credential is entered for it — in <em>your</em> organisation's own
		settings, or by an operator for the whole instance. Until then the
		provider's adapter has no key, and an adapter with no key fails closed: it
		refuses the operation rather than calling out.
	</p>

	<p>
		<strong>Invoice extraction is the exception.</strong> It is the
		integration that sends whole documents, and it is on by default rather
		than off, so it sits in § 2 with everything else that applies to
		everybody.
	</p>

	<p>
		For the rest, two consequences follow, and they are why this page is split
		the way it is:
	</p>

	<ul>
		<li>
			<strong>The choice is made for your organisation, not to it.</strong> Each
			integration is selected in your own organisation's settings; an operator can
			also set a default for the whole instance, which is how the invoice reader in
			§ 2 is chosen. What never happens is another customer's choice reaching you:
			their enabling a provider does not make it a sub-processor of your data.
		</li>
		<li>
			<strong>Your accurate answer is "the ones we configured".</strong> If you are
			completing an Article 30 record, the sub-processors you should list are the
			ones in § 2 — which apply to everybody — plus only those in § 3 that your
			administrators have actually turned on. Your administrators can see which those
			are in your organisation settings.
		</li>
	</ul>

	<p>
		We list the dormant providers anyway, in full detail, because the point of
		a register is to let you decide before you switch something on, not to
		discover afterwards what it received.
	</p>

	<h2 id="engaged-today">2. Engaged today, for everyone</h2>

	<p>
		These are in the path of the running service without anyone having to
		switch them on: the hosting substrate, the provider that reads uploaded
		invoices, and the bot-protection control on the public sign-up form.
	</p>

	<p>
		<strong>One row is a qualified entry.</strong> Amazon SES is listed here
		rather than in § 3 because outbound mail is our own path and not one you
		select — but it is engaged only where an operator has configured the
		deployment to send through it. The shipped default is a console adapter
		that writes the message to our own log and sends nothing at all. So SES
		is available to every customer rather than engaged for every customer,
		and the rest of the table is unconditional.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Sub-processors engaged for every customer</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Amazon Web Services — EC2</th>
					<td>The virtual machine the application, its PostgreSQL databases and its Redis instance run on.</td>
					<td>Everything the service holds: invoices, suppliers, payments, users and the audit trail. The databases run on the instance's own encrypted disk, not on a managed database service.</td>
					<td>United States (us-east-1)</td>
				</tr>
				<tr>
					<th scope="row">Amazon Web Services — S3</th>
					<td>Object storage for uploaded files and backups.</td>
					<td>Invoice and receipt files, contract documents, supplier tax forms, database backups, the write-once audit archive and server access logs. Positive Pay files held here carry full bank account and routing numbers, because matching on those is what the file is for.</td>
					<td>United States (us-east-1)</td>
				</tr>
				<tr>
					<th scope="row">Amazon Web Services — KMS</th>
					<td>Encryption keys for the storage buckets and for deployment secrets.</td>
					<td>No customer data. Key material, and the metadata of the encrypt and decrypt calls AWS makes against the key on our behalf when writing and reading those objects.</td>
					<td>United States (us-east-1)</td>
				</tr>
				<tr>
					<th scope="row">Amazon Web Services — CloudWatch Logs</th>
					<td>Application logs, and the audit-event archive when that sink is selected.</td>
					<td>Audit events: who acted, what action, on which record, and what changed — the before and after values for ordinary business fields, and only the last four digits for bank details and tax identifiers. Application logs deliberately carry no bank numbers, tax identifiers or card numbers.</td>
					<td>United States (us-east-1)</td>
				</tr>
				<tr>
					<th scope="row">Amazon Web Services — Route 53</th>
					<td>DNS for the service's domain and for each customer's subdomain.</td>
					<td>No customer data. DNS query metadata, and the subdomain name itself, which is derived from your organisation's slug.</td>
					<td>United States, served from global edge locations</td>
				</tr>
				<tr>
					<th scope="row">Amazon Web Services — SES</th>
					<td>Sending transactional email out of the service, where the deployment is configured to send through SES rather than to the console. This row is the outbound direction only; SES in the <em>inbound</em> role — receiving mail at an invoice-intake address — is a separate, opt-in integration described in § 3.7.</td>
					<td>Recipient name and email address, and the message itself — sign-in and multi-factor codes, approval requests, notifications, and any invoice reference the message quotes.</td>
					<td>United States (us-east-1)</td>
				</tr>
				<tr>
					<th scope="row">Anthropic</th>
					<td>Reading uploaded invoices. Unless your organisation supplies its own key, or an operator selects one of the alternatives in § 3.1, an invoice that is not a structured e-invoice is read by Claude. Several further features run over the same connection, each behind a switch of its own and each off until it is turned on — they are listed below the table.</td>
					<td>The invoice file itself — the whole document, exactly as uploaded, including anything a supplier chose to put on the page — together with the context that guides the reading: your chart of accounts, and fields from earlier invoices used as worked examples. Each of the separately-switched features below adds to that, and what each one adds is set out there.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">hCaptcha (Intuition Machines, Inc.)</th>
					<td>Bot protection on the public self-service sign-up form.</td>
					<td>The IP address of the person completing the form, the challenge token, and the browser signals hCaptcha's own script collects in the page. No invoice, supplier or payment data.</td>
					<td>United States</td>
				</tr>
			</tbody>
		</table>
	</div>

	<p>
		<strong>Anthropic is the exception to the opt-in rule, and it is the
		consequential one</strong>, so it is stated here rather than left to be
		inferred from a table. A deployed instance reads invoices through Claude
		by default, which means the invoice document itself leaves the service on
		upload — every field on it, including anything personal a supplier printed
		there. Three things change that. Your organisation can supply its own key
		and name a different provider. An operator can point the instance at one
		of the alternatives in § 3.1, or at a self-hosted model server that sends
		nothing off the machine. And a structured e-invoice — UBL, CII or
		Factur-X — is parsed in our own process and reaches no model provider at
		all.
	</p>

	<p>
		Four further features run over that same Anthropic connection. Each is
		off until it is switched on separately, each has its own switch rather
		than riding the invoice reader's, and each widens what Anthropic
		receives:
	</p>

	<ul>
		<li>
			<strong>The conversational AP assistant</strong> — the natural-language
			question your user types, and the invoice, supplier and payment figures
			the tools it calls return.
		</li>
		<li>
			<strong>Audit-log summaries</strong> — the audit timeline of one invoice:
			what happened, when, the <strong>full name of each employee who acted</strong>,
			and any free text one of them typed as the reason for a rejection or the
			resolution of an exception. This is the only feature here that sends your
			own staff's names.
		</li>
		<li>
			<strong>Anomaly and fraud commentary on an invoice</strong> — the only
			feature on this page whose prompt carries a supplier's remit-to address,
			alongside amounts and dates drawn from recently approved invoices for the
			same supplier.
		</li>
		<li>
			<strong>The written rationale an exception agent records for a decision</strong>
			— the amounts, variances, purchase-order numbers and general-ledger codes
			of the exception concerned. The agent's decision itself is computed by
			rules and does not depend on the model; only the sentence explaining it is
			reworded.
		</li>
	</ul>

	<p>
		None of the four adds a sub-processor you did not already have, and
		leaving all four off does not affect invoice reading. The converse is the
		easier thing to assume wrongly, so it is worth saying plainly: each of the
		four addresses Anthropic directly. Choosing a different invoice reader in
		§ 3.1, or pointing the instance at a self-hosted model server, changes who
		reads your invoices and nothing else — a feature in this list keeps going
		to Anthropic until its own switch is turned off.
	</p>

	<p>
		hCaptcha is the one third party a visitor meets before they are a customer
		at all, so it is worth being plain about it: it is mandatory in any
		deployed environment, the service will not start without it configured,
		and it is on the sign-up form only. It is not present anywhere inside the
		signed-in application.
	</p>

	<p>
		Everything above sits in a single region. The hosting region for customer
		data is
		<Fact value={OPERATOR.hostingRegion} label="hosting region" />, because the
		workload stack that will hold it is not yet deployed; the infrastructure
		that exists today — the storage buckets, the encryption key and the DNS
		zone — is configured in AWS <code>us-east-1</code>, in the United States.
		See § 8 on transfers.
	</p>

	<h2 id="available">3. Available, but engaged only if you switch them on</h2>

	<p>
		Nothing in this section is contacted until it has been configured with a
		live credential — in your organisation's own settings, or by an operator
		for the whole instance. Read these tables as "what this would mean if we
		switched it on" rather than as a list of parties holding your data.
	</p>

	<h3 id="ai">3.1 Other AI and document processing</h3>

	<p>
		These stand in for, or add to, the default invoice reader described in
		§ 2. Selecting one of the first two replaces Anthropic in that role; the
		embeddings use is additional. A self-hosted model server is a third
		option and sends nothing off the machine at all.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">AI and document-processing providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">OpenAI</th>
					<td>Reading invoices, in place of the default reader; and text embeddings, which are what duplicate detection and free-text search compare against.</td>
					<td>For reading: the invoice's text layer where the document has one, and the whole page image where it does not — a scan or a photograph. For embeddings: the full extracted text of an invoice, and the free-text queries your users type into search and the assistant.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Amazon Textract</th>
					<td>Reading invoices by OCR, in place of the default reader.</td>
					<td>The whole invoice file, sent as raw bytes.</td>
					<td>The AWS region configured for it (us-east-1 today)</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="erp">3.2 ERP and accounting</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">ERP connectivity providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Merge.dev</th>
					<td>A unified API that brokers the connection to your accounting system, so one integration reaches many ERPs.</td>
					<td>The invoice as posted: its number, issue and due dates, currency, total, subtotal, tax and discount, the memo or description, the purchase-order number, and every line item with its description, quantity, unit price, line total and general-ledger account. It also carries our own internal reference for the invoice — an opaque identifier, which doubles as the key that stops a retry posting the invoice twice. The supplier's tax identifier and address are not sent.</td>
					<td>United States</td>
				</tr>
			</tbody>
		</table>
	</div>

	<p>
		<strong>This connection also runs the other way.</strong> A supplier sync
		pulls vendor records out of your accounting system and into
		{OPERATOR.serviceName}, and those records can carry a supplier's tax
		identifier and postal address. That is your own data coming back to you
		from a system you control, not data we send — but a register with only an
		outbound column cannot show it, and your own Article 30 record should.
	</p>

	<p>
		Direct connections to <strong>NetSuite</strong> and
		<strong>Microsoft Dynamics 365 Business Central</strong> are also
		supported, and they are a different relationship — see § 4.
	</p>

	<h3 id="payments">3.3 Payments and cheque printing</h3>

	<p>
		One point applies to every payment provider below and is worth stating
		before the table: the outbound payment call does not carry a raw bank
		account or routing number. The counterparty is registered with the
		provider beforehand, and the payment instruction references it. A rail
		that is not given that reference refuses the payment rather than falling
		back to sending the account details.
	</p>

	<p>
		What each rail carries beyond that varies slightly, so the first row below
		states the widest set rather than the narrowest. Cheque printing is the
		one genuine departure, and has its own row.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Payment providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Modern Treasury</th>
					<td>Initiating ACH and wire payments.</td>
					<td>Amount and currency, the payment method, the reference to the counterparty already registered with the provider, our own payment and invoice identifiers, and — carried as the payment's description or metadata so the payment can be reconciled at the bank — the invoice number and the supplier's name.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Stripe Treasury</th>
					<td>Initiating payments over Stripe's treasury rails.</td>
					<td>As above.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Increase</th>
					<td>Initiating bank-rail payments.</td>
					<td>As above.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Column</th>
					<td>Initiating bank-rail payments.</td>
					<td>As above.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Dwolla</th>
					<td>Initiating ACH payments.</td>
					<td>As above.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Checkeeper</th>
					<td>Printing and posting paper cheques.</td>
					<td>More than the rails above, necessarily: the supplier's legal name and <strong>full mailing address</strong> — street, city, state, postal code and country — the amount and currency, a memo line carrying the invoice number or description, our invoice identifier, and a reference to the drawing account already held with Checkeeper.</td>
					<td>United States</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="cards">3.4 Virtual cards</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Virtual card issuers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Lithic</th>
					<td>Issuing single-use or supplier-locked virtual cards.</td>
					<td>The spend limit, the supplier's name, and the invoice reference the card is issued against. The card number is generated by the issuer — we never send one, we never store one, and we hold only its last four digits. Revealing a full number fetches it from the issuer at that moment.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Nium</th>
					<td>Issuing virtual cards for international spend.</td>
					<td>The spend limit and its currency, the invoice reference, and the supplier's name inside the card's free-text memo rather than as a field of its own. As with Lithic, the card number is generated by the issuer and we store only the last four digits.</td>
					<td>Varies by issuing region</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="screening">3.5 Sanctions and watchlist screening</h3>

	<p>
		The transmitted field list here is deliberately short, and it is smaller
		than the screening function's own inputs: our internal screening call also
		accepts a supplier tax identifier and beneficial-owner details, and every
		one of the three adapters below drops them. Only the name and a country
		code reach the provider.
	</p>

	<p>
		Two precisions about that country code, because it is not what the label
		suggests. It is the country of the <strong>destination bank account</strong>
		we hold for the supplier, not the supplier's own country — we do not record
		a country for a supplier at all — and a supplier with no bank details on
		file is therefore screened on its name alone. Each request also carries a
		small amount of non-personal tuning that tells the provider how to search:
		a fuzzy-match threshold, which watchlists and content sets to search, and
		our own account or group identifier with that provider. Nothing else about
		the supplier is transmitted.
	</p>

	<p>
		That last sentence is enforced rather than asserted: an automated test pins
		the exact field set each of the three providers receives, so a change that
		began sending a tax identifier or a beneficial owner's details would fail
		before it shipped — and would have to be disclosed here in the same change.
		We mention the mechanism because a promise about data minimisation is only
		as good as whatever keeps it true after the page is written.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Sanctions screening providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">ComplyAdvantage</th>
					<td>Screening a supplier against sanctions, politically-exposed-person and adverse-media lists.</td>
					<td>The supplier's name as the search term, and the destination bank's two-letter country code as a filter where we hold one. No other supplier data.</td>
					<td>United Kingdom, EU and United States</td>
				</tr>
				<tr>
					<th scope="row">Dow Jones Risk &amp; Compliance</th>
					<td>Screening a supplier against sanctions and watchlists.</td>
					<td>The supplier's name, and the destination bank's country code where we hold one. No other supplier data.</td>
					<td>United States and EU</td>
				</tr>
				<tr>
					<th scope="row">LSEG / Refinitiv World-Check</th>
					<td>Screening a supplier against sanctions and watchlists.</td>
					<td>The supplier's name, and the destination bank's country code as a nationality filter where we hold one. No other supplier data.</td>
					<td>United States and EU</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="tax">3.6 Tax filing and taxpayer-identifier matching</h3>

	<p>
		This is the one opt-in integration that receives a raw tax identifier, so
		it deserves a heading of its own rather than a line in a longer table.
		With no provider configured, both uses are served entirely on our own
		machines and no identifier leaves the service: identifier matching runs an
		offline structural check of the number, and filing is recorded against a
		local, in-process filer.
	</p>

	<p>
		The two behave differently if Tax1099 is named but no credential is
		entered, and the difference is worth stating rather than blurring.
		Identifier matching degrades quietly to that same offline structural
		check. Filing does not degrade — it refuses outright, the claimed filing
		is released, and nothing is filed anywhere. In neither case does an
		identifier leave the service.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Tax filing providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Tax1099 (Zenwork, Inc.)</th>
					<td>Electronically filing 1099 forms with the IRS, and real-time matching of a taxpayer identifier against IRS records.</td>
					<td>For filing: the recipient's legal name, their <strong>full taxpayer identification number</strong>, the form type, the box amounts and our supplier identifier. For matching: the full identifier, the legal name and the identifier type.</td>
					<td>United States</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="email">3.7 Email</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Email providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">An SMTP host you nominate</th>
					<td>Sending outbound email through your own relay instead of ours.</td>
					<td>Recipient name and address, and the message body — the same content described for SES in § 2.</td>
					<td>Wherever your relay runs</td>
				</tr>
				<tr>
					<th scope="row">Amazon SES, Mailgun, or a provider you nominate</th>
					<td>Receiving email sent to your organisation's invoice-intake address and turning each message into an invoice.</td>
					<td>The sender's address, the recipient intake address, the subject, and every attachment — which is to say the invoice documents themselves.</td>
					<td>United States or EU, depending on the provider</td>
				</tr>
			</tbody>
		</table>
	</div>

	<p>
		Email intake is off until an intake domain is configured; with none set,
		no inbound provider is in the path. The default for outbound email is a
		console adapter that writes the message to our own log and sends nothing.
	</p>

	<p>
		<strong>Amazon SES appears on this page twice, and the two entries are
		different things.</strong> In § 2 it is the <em>outbound</em> path — the
		service sending mail to your users, such as sign-in codes, approval
		requests and notifications — selected by an operator for the whole
		deployment. The second row above is the <em>inbound</em> path: mail your
		suppliers send to your organisation's invoice-intake address, which we
		receive and turn into invoices. They are configured separately and carry
		different data in opposite directions, so turning one on does not turn the
		other on, and a deployment can run either, both or neither. Mailgun and a
		relay you nominate appear only in this section, and only in one direction
		each.
	</p>

	<h3 id="chat">3.8 Chat notifications</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Chat notification providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Slack (Salesforce)</th>
					<td>Posting an approval request into your own workspace, with buttons that approve or reject from the message.</td>
					<td>The invoice number, the supplier's name, the amount and currency, the status, and a deep link back into the application. No line-item descriptions and no contact details. The approve and reject buttons carry a single-use token, which is signed rather than encrypted: anyone able to read the button's data can decode it, and it decodes to three identifiers — your organisation, the invoice, and the person the button was issued to. Identifiers, not names: no name of an approver is sent.</td>
					<td>Your workspace's region</td>
				</tr>
				<tr>
					<th scope="row">Microsoft Teams</th>
					<td>The same, as a Teams message card.</td>
					<td>The same fields.</td>
					<td>Your Microsoft tenant's region</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="lookups">3.9 Reference-data lookups</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">Reference-data lookup providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">Dun &amp; Bradstreet</th>
					<td>Matching a supplier to a D-U-N-S record and returning firmographics as a suggestion.</td>
					<td>The supplier's legal name, as the match query — that is the whole request. No country is sent either: we hold no country field for a supplier, so the lookup goes out on the name alone. The supplier's tax identifier is not part of it and is never transmitted; it is not redacted on the way out, it is simply never included. (A last-four form of it appears in the stored result so a reviewer can tell which supplier a match refers to, which is a different thing and happens after the call.)</td>
					<td>United States and global</td>
				</tr>
				<tr>
					<th scope="row">Clearbit (HubSpot)</th>
					<td>Looking up company details from a web domain.</td>
					<td>The supplier's domain name. Nothing else.</td>
					<td>United States</td>
				</tr>
				<tr>
					<th scope="row">Open Exchange Rates</th>
					<td>Fetching a currency conversion rate.</td>
					<td>The currency pair. No personal data of any kind is sent.</td>
					<td>United States</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="einvoicing">3.10 E-invoicing</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">E-invoicing providers</caption>
			<thead>
				<tr>
					<th scope="col">Provider</th>
					<th scope="col">What it is used for</th>
					<th scope="col">Data it receives</th>
					<th scope="col">Location</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">A PEPPOL Access Point operator you choose</th>
					<td>Sending and receiving structured invoices over the PEPPOL network.</td>
					<td>The full structured invoice — both parties' names, addresses and registration identifiers, every line item, the tax breakdown and the payment terms.</td>
					<td>Determined by the operator you appoint, typically the EU</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h2 id="your-own-systems">4. Your own systems and counterparties</h2>

	<p>
		Some connections send data outward without engaging a sub-processor of
		ours, because the destination is a system you already control under your
		own agreement with its vendor. We are delivering your data back to you,
		not appointing someone to process it on our behalf. These belong in your
		records as your own relationships:
	</p>

	<ul>
		<li>
			<strong>Direct ERP connections</strong> — NetSuite (Oracle) and Microsoft
			Dynamics 365 Business Central. When you connect one, invoices are posted
			into your own tenant of your own accounting system.
		</li>
		<li>
			<strong>Your identity provider</strong> — Okta, Microsoft Entra ID,
			Authentik, Keycloak or any other, for single sign-on and user provisioning.
			The exchange is an authentication assertion and the user attributes you
			choose to send us; we hold your users' identities, your identity provider
			does not gain ours.
		</li>
		<li>
			<strong>Supplier catalogue punch-out</strong> — a supplier's own catalogue
			system, which receives the buyer's session identity and the cart contents
			during a punch-out.
		</li>
	</ul>

	<p>
		Slack and Microsoft Teams sit on the line: the receiving workspace is
		yours, but the message does transit the vendor's infrastructure, so we
		list them in § 3.8 rather than here.
	</p>

	<h2 id="not-reachable">5. In the software, reachable by nobody</h2>

	<p>
		A few integrations exist in the source as unfinished code: the request
		shape is written down, the network call is not. Asked to do their job
		they either refuse outright or answer locally — the financing connector
		returns a "not eligible" result rather than contacting a financier — and
		in neither case does anything leave the service. They are the tax-rate
		integrations (Avalara and TaxJar), the quality-management system
		connector, and the supply-chain financing connector (C2FO). None of them
		can transmit anything to anyone in any configuration, so none of them is
		a sub-processor. We name them because someone auditing us should not have
		to wonder whether this page quietly omitted them. If one is completed it
		moves into § 3, with the § 9 notice and a § 10 change-log row, before it
		ships.
	</p>

	<h2 id="not-sub-processing">6. Outbound paths that are not sub-processing</h2>

	<p>
		Two features send data to a destination you nominate: outbound webhooks
		from the developer API, and scheduled reports emailed to addresses you
		choose. We deliver to the address or URL you configured, and we do not
		choose or engage the recipient, so these are transfers you direct rather
		than sub-processing by us. They are worth recording in your own register
		for the same reason we name them here: your data does leave, and you
		decided where it goes.
	</p>

	<h2 id="our-own-billing">7. Our own billing</h2>

	<p>
		Separate from everything above, because it is not your data being
		processed on your behalf. <strong>Stripe is the billing provider the
		platform is built against, and it is not engaged yet</strong> — billing
		currently runs on the same local mock every other integration defaults to,
		so nothing has been sent to Stripe at all. The same engaged-versus-available
		distinction as section 1, applied to our own billing rather than yours.
	</p>

	<p>
		What would reach Stripe once it is switched on is narrower than the word
		"billing" suggests, so it is worth stating as fields rather than as a
		function. On a plan change we would register your organisation with Stripe
		as a customer — its name, a billing contact email address, and our own
		internal identifier for it — and register the selected plan's monthly
		price under that plan's code. Beyond that we would ask Stripe for the
		invoices and receipts it has issued you, and for the payment method held
		against that customer. <strong>Your subscription record and your usage
		metering stay on our side</strong>: both are computed and held here, and
		no metering event is reported to Stripe. Card details would be collected
		by Stripe directly and never reach us — the brand, last four digits and
		expiry a billing page shows you are read back from Stripe at the moment
		you ask for them and are not stored by us. No supplier, invoice, banking
		or tax data is sent to Stripe for this purpose.
	</p>

	<p>
		For that processing we are the controller and Stripe is an independent
		controller of the payment transaction, not our sub-processor of your
		supplier data. The <a href="/legal/privacy">Privacy Policy</a> covers it.
	</p>

	<h2 id="transfers">8. International transfers</h2>

	<p>
		Almost every provider named on this page is established in the United
		States, and the infrastructure in § 2 is in a single US region. If you are
		in the EEA, the UK or Switzerland, using {OPERATOR.serviceName} involves
		transferring personal data to the United States.
	</p>

	<p>
		Those transfers rely on the European Commission's Standard Contractual
		Clauses, with the UK Addendum issued by the Information Commissioner's
		Office where UK data is involved, together with the supplementary measures
		described in the <a href="/legal/dpa">Data Processing Addendum</a>. Where a
		provider maintains its own certification or transfer framework we rely on
		that too, but the Clauses are the baseline.
	</p>

	<p>
		Two honest qualifications:
	</p>

	<ul>
		<li>
			<strong>Choosing an integration is choosing its transfer.</strong> When your
			administrator enables a provider in § 3, personal data begins flowing to that
			provider in its own location. That decision is yours, and it is yours to record
			in your transfer-impact assessment.
		</li>
		<li>
			<strong>We do not offer a data-residency guarantee.</strong> The application
			has a residency setting, and it is honest to say what it currently is: a recorded
			preference. Nothing routes on it. All customer data lives in one region today, and
			we will not tell you otherwise until a regional deployment actually exists.
		</li>
	</ul>

	<h2 id="changes">9. Changes to this register</h2>

	<p>
		Before we engage a new sub-processor that affects your data, we will give
		you <strong>at least 30 days' advance notice</strong>, and you may object
		on reasonable data-protection grounds within that window. If we cannot
		resolve your objection, you may terminate the affected part of the service
		as set out in the <a href="/legal/dpa">Data Processing Addendum</a>.
	</p>

	<p>
		The mechanism today is exactly this page: we update it, change the
		last-updated date at the top, and record a dated row in § 10. There is no
		automatic mailing list yet. If you want to be told directly rather than
		having to check, write to
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> with the address
		to notify and we will send the notice there. Objections go to the same
		address; contractual questions about the Addendum go to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>.
	</p>

	<p>
		A provider moving from § 3 to § 2 — that is, from optional to engaged for
		everyone — is a new sub-processor for this purpose and gets the same
		notice. So does a change to what an existing provider receives.
	</p>

	<h2 id="change-log">10. Change log</h2>

	<div class="table-scroll" tabindex="0">
		<table>
			<caption class="visually-hidden">History of changes to this register</caption>
			<thead>
				<tr>
					<th scope="col">Date</th>
					<th scope="col">Change</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<th scope="row">15 September 2026</th>
					<td>Initial publication of the sub-processor register.</td>
				</tr>
			</tbody>
		</table>
	</div>
</LegalPage>
