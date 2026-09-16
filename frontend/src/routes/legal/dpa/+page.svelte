<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import Fact from '$lib/legal/Fact.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';

	/**
	 * The Art 27 representatives and the DPO are records, not plain strings, so
	 * they cannot render through <Fact>. Aliasing them narrows each union once,
	 * here, rather than at every branch below — and the DPO alias is what keeps
	 * its three states (appointed / deliberately not appointed / undecided)
	 * distinguishable in the markup.
	 */
	const euRep = OPERATOR.euRepresentative;
	const ukRep = OPERATOR.ukRepresentative;
	const dpo = OPERATOR.dataProtectionOfficer;
</script>

<LegalPage
	title="Data Processing Addendum"
	intro="These are the Article 28 terms on which we process personal data on your behalf. If you use FeohLedger to run your accounts payable, you are the controller of the supplier, invoice, payment and employee data you load into it, and we are your processor. This page is that agreement: it forms part of our Terms of Service, it takes effect without a signature, and it is written to be read rather than skimmed. Where a statement about what the software actually does would flatter us, we have written the true version instead."
>
	<h2 id="scope">1. Scope and role of the parties</h2>

	<p>
		This Data Processing Addendum (the <strong>DPA</strong>) governs the processing of
		personal data carried out by {OPERATOR.serviceName} on behalf of a customer of the
		{OPERATOR.serviceName} accounts-payable service (the <strong>Service</strong>).
	</p>

	<p>
		<strong>You are the controller; we are the processor.</strong> You decide which
		suppliers, invoices, payments, expenses, contracts and people go into your tenant,
		why, and for how long. We hold and process that data to operate the Service for you
		and for no purpose of our own. You determine the purposes and means; we do not.
	</p>

	<p>
		Where you are yourself a processor for someone else — an outsourced finance
		function, a bookkeeper, a shared-service centre running AP for a group company —
		this DPA applies back to back and we act as your <strong>sub-processor</strong>. In
		that case your own controller's instructions reach us through you, and the
		obligations below run to you in that capacity.
	</p>

	<p>
		This DPA does <em>not</em> cover the personal data we process as a controller in our
		own right: your billing contact, the account we hold for you, the security telemetry
		of our own systems, and enquiries you send us. That processing is described in our
		<a href="/legal/privacy">Privacy Policy</a>, and nothing in this DPA makes us your
		processor for it.
	</p>

	<h2 id="parties">2. The parties and defined terms</h2>

	<p>
		<strong>Processor</strong> — {OPERATOR.serviceName}, operated by
		{OPERATOR.controllerDescription}, registered as
		<Fact value={OPERATOR.legalEntity} label="registered legal entity" />, of
		<Fact value={OPERATOR.postalAddress} label="postal address" />. Referred to below as
		<strong>we</strong>, <strong>us</strong> or <strong>our</strong>.
	</p>

	<p>
		<strong>Controller</strong> — the organisation that has accepted the
		<a href="/legal/terms">Terms of Service</a> and uses the Service, referred to below
		as <strong>you</strong> or the <strong>Customer</strong>.
	</p>

	<p>
		<strong>Personal data</strong>, <strong>processing</strong>,
		<strong>controller</strong>, <strong>processor</strong>,
		<strong>data subject</strong>, <strong>personal data breach</strong> and
		<strong>supervisory authority</strong> carry the meanings given to them in the
		GDPR (Regulation (EU) 2016/679) and, where it applies, the UK GDPR and the Data
		Protection Act 2018.
	</p>

	<p>
		<strong>Data Protection Laws</strong> means every privacy and data-protection law
		applicable to the processing described here — including the GDPR, the UK GDPR, the
		Swiss Federal Act on Data Protection, and the California Consumer Privacy Act as
		amended by the CPRA (together, <strong>CCPA</strong>) where your data includes the
		personal information of California residents.
	</p>

	<p>
		<strong>Agreement</strong> means the <a href="/legal/terms">Terms of Service</a>
		together with this DPA and any order or plan you have subscribed to.
		<strong>Sub-processor</strong> means any third party we engage to process personal
		data on your behalf. <strong>SCCs</strong> means the Standard Contractual Clauses
		annexed to Commission Implementing Decision (EU) 2021/914.
	</p>

	<h3 id="dpo">Data protection contacts</h3>

	<p>
		Data-protection matters under this DPA — instructions, data-subject requests,
		questions about this text — go to
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>. Security incidents and
		vulnerability reports go to
		<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>. Contract and execution
		matters go to <a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>.
	</p>

	{#if dpo}
		<p>
			We have appointed a Data Protection Officer under Article 37: {dpo.name},
			{dpo.address}, <a href="mailto:{dpo.email}">{dpo.email}</a>.
		</p>
	{:else if dpo === false}
		<p>
			We have <strong>not</strong> appointed a Data Protection Officer. We have
			considered Article 37(1) and none of its three thresholds is met: we are not a
			public authority, our core activity is not large-scale regular and systematic
			monitoring of data subjects, and our core activity is not large-scale processing
			of special categories of data. Saying so is the honest disclosure; the contact
			point for everything a DPO would handle is
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</p>
	{:else}
		<p>
			<Fact value={dpo} label="whether a Data Protection Officer is appointed" /> —
			until that is settled, every matter a Data Protection Officer would handle is
			answered at <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>, and you
			should treat that address as the contact point Article 28(3)(e) and the SCCs
			require.
		</p>
	{/if}

	{#if euRep}
		<p>
			Our representative in the European Union under Article 27 is {euRep.name},
			{euRep.address}, <a href="mailto:{euRep.email}">{euRep.email}</a>.
		</p>
	{:else}
		<p>
			<Fact value={euRep} label="an EU representative under GDPR Article 27" />. Until
			one is appointed, address enquiries that would go to a representative directly to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</p>
	{/if}

	{#if ukRep}
		<p>
			Our representative in the United Kingdom under UK GDPR Article 27 is
			{ukRep.name}, {ukRep.address}, <a href="mailto:{ukRep.email}">{ukRep.email}</a>.
		</p>
	{:else}
		<p>
			<Fact value={ukRep} label="a UK representative under UK GDPR Article 27" />. The
			same interim address applies:
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</p>
	{/if}

	<h2 id="incorporation">3. How this DPA is entered into</h2>

	<p>
		This DPA is <strong>incorporated by reference into the
		<a href="/legal/terms">Terms of Service</a></strong> and forms part of the
		Agreement. It takes effect on the earlier of the date you accept those Terms and the
		date you first use the Service, and it <strong>requires no signature</strong> to be
		binding on both parties. Article 28(9) requires the contract to be in writing,
		including in electronic form; this page is that written contract.
	</p>

	<p>
		If your procurement process needs an executed counterpart, write to
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a> and we will return a
		countersigned copy of this text, with your legal entity named as Controller and the
		annexes reproduced unchanged. A countersigned copy is a convenience for your records;
		it does not alter the terms, and you gain nothing by waiting for one before you start.
	</p>

	<p>
		Where you and we have signed a separate, negotiated data processing agreement, that
		document governs and this page does not apply to you. A DPA form you send us as part
		of a purchase order or vendor-onboarding pack is <em>not</em> agreed unless we have
		signed it — silence is not acceptance.
	</p>

	<h2 id="processing">4. Subject matter, duration, nature and purpose</h2>

	<p>
		<strong>Subject matter.</strong> Our processing of personal data contained in your
		accounts-payable records so that we can provide the Service to you.
	</p>

	<p>
		<strong>Nature of the processing.</strong> Hosting, storage, structuring, retrieval,
		display, extraction of data from documents you upload, matching, routing for
		approval, calculation, export, transmission to the integrations you enable, backup,
		archival, redaction and deletion. The full list of categories is in
		<a href="#annex-i">Annex I</a>.
	</p>

	<p>
		<strong>Purpose.</strong> Solely to provide, maintain, secure and support the Service
		in accordance with your documented instructions. We do not process your personal data
		for our own purposes, we do not sell it, we do not share it for cross-context
		behavioural advertising, and we do not use it to train or improve any machine-learning
		model of ours.
	</p>

	<p>
		<strong>Duration.</strong> For the term of the Agreement, plus the period needed to
		complete the deletion or return described in <a href="#deletion">section 13</a>, plus
		any period for which retention is required by law.
	</p>

	<p>
		<strong>Types of personal data and categories of data subject.</strong> Set out in
		<a href="#annex-i">Annex I</a>, which is the operative description for the purposes
		of Article 28(3) and of Annex I to the SCCs.
	</p>

	<h2 id="instructions">5. Processing only on documented instructions</h2>

	<p>
		We process your personal data <strong>only on your documented instructions</strong>,
		including with regard to transfers of personal data to a third country or an
		international organisation.
	</p>

	<p>Your documented instructions are, in full:</p>

	<ol>
		<li>this DPA and the <a href="/legal/terms">Terms of Service</a>;</li>
		<li>
			<strong>your configuration of the Service</strong> — which integrations you
			enable and with whose credentials, your retention windows, your SSO and SCIM
			settings, your data-residency selection, the webhook endpoints and scheduled-report
			recipients you nominate, the chat workspace you connect, the entities and users you
			create, and which of your suppliers you invite to the supplier portal; and
		</li>
		<li>
			any further written instruction you give us by a documented channel, including
			from an address we have agreed with you and a request raised at
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
		</li>
	</ol>

	<p>
		Point 2 is load-bearing and we would rather be blunt about it: <strong>turning on a
		feature that sends data somewhere is an instruction to send it there.</strong>
		Enabling a third-party extraction provider instructs us to transmit your invoice
		images to that provider. Nominating a webhook URL instructs us to post event data to
		that URL. Connecting a Slack or Teams workspace instructs us to post approval
		messages into it. We will carry out those instructions; you are responsible for their
		lawfulness and for the destination you chose.
	</p>

	<p>
		We process on instructions other than yours only where required to do so by Union or
		Member State law (or other applicable law) to which we are subject. In that case we
		will inform you of that legal requirement <strong>before</strong> processing, unless
		that law prohibits the notification on important grounds of public interest. Where we
		receive a binding legal demand for your data, we will — to the extent legally
		permitted — notify you, challenge any demand that appears unlawful or overbroad,
		provide only the minimum required, and redirect the requester to you where we lawfully
		can, because it is your data and not ours to disclose.
	</p>

	<p>
		<strong>If an instruction looks unlawful, we say so and we stop.</strong> Where in our
		opinion an instruction infringes the GDPR, the UK GDPR or another Data Protection Law,
		we will inform you immediately under Article 28(3), and we may suspend performance of
		that particular instruction — without terminating the Service — until you confirm,
		withdraw or amend it. If you confirm an instruction we still consider unlawful, we may
		decline to carry out that instruction and, where it goes to the substance of the
		Service, terminate the affected processing on notice. We will not quietly comply with
		something we believe to be unlawful, and we will not pretend we never noticed.
	</p>

	<h2 id="confidentiality">6. Confidentiality of personnel</h2>

	<p>
		We ensure that every person authorised to process your personal data is bound by an
		obligation of confidentiality — contractual where they are engaged by us, statutory
		or professional where that already applies — and that the obligation survives the end
		of their engagement.
	</p>

	<p>
		Access is granted on a need-to-know basis, limited to what a person needs to operate
		and support the Service, and removed when it is no longer needed. Access to a
		customer tenant for support purposes is logged in the same append-only audit trail as
		every other action. We are a small operation: at the time of writing, the population
		of people with production access is the operator named in
		<a href="#parties">section 2</a>. That is a security property worth knowing in both
		directions, and we would rather state it than imply a security team we do not have.
	</p>

	<h2 id="security">7. Security of processing (Article 32)</h2>

	<p>
		We implement appropriate technical and organisational measures to ensure a level of
		security appropriate to the risk, taking into account the state of the art, the costs
		of implementation, the nature, scope, context and purposes of processing, and the risk
		to the rights and freedoms of natural persons. Those measures are described —
		specifically, and in terms you can check against the product — in
		<a href="#annex-ii">Annex II</a>.
	</p>

	<p>
		Annex II also carries a section headed <em>Measures we do not claim</em>. Please read
		it. The data in this Service includes bank account details and government tax
		identifiers, which is precisely the category where an overstated security claim causes
		real harm, so we have listed what is not in place as carefully as what is.
	</p>

	<p>
		We may update our measures over time. We will not make a change that materially
		reduces the overall level of security of the Service.
	</p>

	<h2 id="sub-processors">8. Sub-processors</h2>

	<p>
		<strong>General authorisation.</strong> You give us general written authorisation to
		engage sub-processors, subject to the conditions in this section. This is the
		Article 28(2) general authorisation and the SCC Clause 9(a) Option 2 option.
	</p>

	<p>
		<strong>The current list is published at
		<a href="/legal/sub-processors">/legal/sub-processors</a></strong> and identifies each
		sub-processor, what it does, the categories of data it can receive, and where it
		processes. Read it alongside this DPA — it is part of the description of the
		processing, and <a href="#annex-iii">Annex III</a> incorporates it.
	</p>

	<p>
		<strong>An important structural point.</strong> Nearly every integration in the
		Service is a pluggable adapter that ships with a local, non-networked default, and an
		adapter with no credential fails closed to that default rather than calling out. Most
		rows on the sub-processor register are therefore <em>available</em> rather than
		<em>engaged</em>: the honest answer to "who are your sub-processors" is the three that
		are engaged in every deployed environment — the infrastructure provider, Anthropic for
		invoice extraction, and hCaptcha on the public signup form — plus whichever adapters
		your tenant or the deployment has actually turned on. The register marks which is
		which, and <a href="#ai-processing">Artificial intelligence and machine learning</a>
		explains why extraction is not on the opt-in side of that line.
	</p>

	<h3 id="sub-processor-changes">Notice of change, and your right to object</h3>

	<ol>
		<li>
			We will give you at least <strong>30 days' notice</strong> before a new
			sub-processor begins processing your personal data, or before we replace an
			existing one. <strong>Notice is given by updating
			<a href="/legal/sub-processors">/legal/sub-processors</a>, which carries a dated
			change log</strong> — that page is the notice, and checking it is how you receive
			it. There is no automatic mailing list yet; building one is committed work, and
			until it exists you may ask at
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> to be told directly, and
			we will do so manually. We would rather describe the mechanism we have than
			promise one we do not.
		</li>
		<li>
			You may <strong>object</strong> on reasonable data-protection grounds within
			those 30 days, in writing to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>, stating the grounds. We
			will work with you in good faith to address the objection — by proposing a
			commercially reasonable alternative, by changing the configuration so the
			sub-processor is not used for your tenant where the architecture allows it, or by
			explaining why we believe the concern is met.
		</li>
		<li>
			If we cannot resolve the objection within a reasonable period, you may
			<strong>terminate</strong> the affected part of the Service, without penalty, on
			written notice, with a pro-rata refund of fees paid for the unused remainder of
			your term. We will not treat a good-faith objection as a breach by you.
		</li>
		<li>
			Where a change is needed urgently to protect the security or availability of the
			Service, we may make it sooner than 30 days and will tell you as soon as we
			reasonably can, with the reason. Your right to object survives that.
		</li>
	</ol>

	<h3 id="sub-processor-flow-down">Flow-down and liability</h3>

	<p>
		We engage each sub-processor under a written contract imposing data-protection
		obligations <strong>no less protective</strong> than those in this DPA, in particular
		the obligation to implement appropriate technical and organisational measures, and we
		make a copy of the relevant terms available to you on request, redacted for
		commercial confidentiality. <strong>We remain fully liable to you for the performance
		of each sub-processor's obligations</strong> as if the acts were our own.
	</p>

	<h3 id="ai-processing">Artificial intelligence and machine learning</h3>

	<p>
		Some features use AI models: extraction of fields from an uploaded invoice, the
		conversational assistant, the cash-flow copilot, exception-handling agents and
		duplicate-detection embeddings. All but one default to a local or in-process
		implementation with no third-party call.
	</p>

	<p>
		<strong>Invoice extraction is the exception, and the Controller should read this
		before relying on the rest of this section.</strong> In any deployed environment it
		transmits the uploaded document — the image and everything printed on it — to
		Anthropic, whether or not the Controller or the operator has configured a model
		provider. The offline stand-in returns fabricated invoice values, so defaulting to it
		would post invented figures against a real supplier's document; the Service therefore
		fails loudly against the real provider instead. Anthropic is accordingly a
		sub-processor for every tenant from the moment the Service is deployed, and is listed
		as engaged on the register rather than as available.
	</p>

	<p>
		For the remaining AI features, if you enable a hosted model provider you instruct us to
		transmit the relevant content to that provider, and that provider becomes a
		sub-processor for your tenant.
	</p>

	<p>
		<strong>We do not train models on your data.</strong> We cannot make the same promise
		on behalf of a provider you enable: the register at
		<a href="/legal/sub-processors">/legal/sub-processors</a> records, per provider,
		whether zero-retention and no-training terms have been confirmed, and where the entry
		says they are unconfirmed, you should treat them as unconfirmed rather than assume.
		If that matters to you, keep the local default, which is what ships.
	</p>

	<h3 id="directed-transfers">Transfers you direct, which are not sub-processing</h3>

	<p>
		Three egress paths are deliberately not sub-processors, because you choose the
		destination and we are merely delivering to it: outbound Developer-API webhooks to a
		URL you nominate, scheduled report delivery to recipients you nominate, and approval
		notifications posted into your own Slack or Microsoft Teams workspace. These are
		disclosures made on your instruction. You are the controller of what arrives there and
		responsible for the lawfulness of the destination; we are responsible for sending only
		what the feature is specified to send. We name them rather than omit them, because a
		reader auditing data flows will find them either way.
	</p>

	<h2 id="data-subject-rights">9. Assistance with data-subject rights (Articles 12–23)</h2>

	<p>
		Taking into account the nature of the processing, we assist you by appropriate
		technical and organisational measures, insofar as this is possible, in fulfilling your
		obligation to respond to requests to exercise the rights of access, rectification,
		erasure, restriction of processing, data portability, objection, and the right not to
		be subject to a decision based solely on automated processing.
	</p>

	<p>The assistance is mostly built into the product, and you run it yourself:</p>

	<ul>
		<li>
			<strong>Access and portability.</strong> An administrator in your tenant can
			produce a subject-access bundle for one of your users, one supplier-portal user, or
			one supplier contact — profile data, the related invoice and payment summary with
			exact decimal amounts, portal accounts and activity counts — as structured JSON.
		</li>
		<li>
			<strong>Rectification.</strong> The supplier, invoice, expense, contract and user
			fields a data subject could ask you to correct are editable in the application by
			an appropriately permissioned user, and the correction writes an audit entry
			recording what changed.
		</li>
		<li>
			<strong>Erasure.</strong> An administrator can irreversibly redact a subject's
			personal data in place. Contact details, tax identifiers, bank details,
			beneficial-owner data, authentication secrets and supplier-authored chat bodies are
			replaced with non-identifying tombstones. Please read the three limits in the next
			paragraph, because they are the ones that matter.
		</li>
		<li>
			<strong>Restriction and objection.</strong> A user or portal account can be
			deactivated, and processing for a given supplier can be suspended, at your
			instruction.
		</li>
		<li>
			<strong>Automated decision-making.</strong> As shipped, an invoice reaches payment
			only after a person in your organisation approves it, and the approval is recorded
			against their identity. You can configure two exceptions and you should know they
			exist: an approval step can auto-approve invoices below a threshold you set (there
			is no such threshold unless you set one), and the exception-handling agents have an
			autonomy level that can let them resolve an exception without a person — the
			default level is conservative, at which every exception escalates to a human.
			Sanctions and watchlist screening can also place an automatic hold on a payment,
			which a person then clears. Every automated action writes an audit record naming
			the rule, the confidence and the change it made, and a person can reverse it.
			Whether any of that amounts to a decision within Article 22 is your assessment as
			controller, because the configuration is yours; we will give you whatever detail
			you need to make it.
		</li>
	</ul>

	<p>
		<strong>Three limits on erasure, stated plainly.</strong> First, the money trail
		survives: amounts, currencies, statuses and dates on invoices and payments are never
		mutated, the supplier's legal name is preserved where it is the payee denormalised
		onto a financial record, and the append-only audit log is never edited or deleted.
		That is a deliberate choice — Article 17(3)(b) and (e) allow retention where processing
		is necessary for compliance with a legal obligation and for the establishment or
		defence of legal claims, and an AP ledger you can erase retrospectively is not an AP
		ledger. Second, <strong>the in-product erasure function redacts the databases but does
		not today delete the documents you have uploaded from object storage</strong>. Where a
		request requires a stored invoice, receipt, contract or tax form to be deleted, raise
		it at <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> and we will perform that
		deletion for you within the timeframe in <a href="#deletion">section 13</a>. Third,
		<strong>the automated path does not clear a registered passkey or the record of a
		session already issued.</strong> Access itself stops at once — every request re-reads
		the account and refuses a deactivated one — but the stored authenticator credential
		survives, and the session record persists until it expires (at most the access-token
		lifetime, thirty minutes by default). Raise it with us at the same address if a
		request requires the credential itself to be destroyed. We would rather tell you where
		each seam is than let you discover one in the middle of a regulator's deadline.
	</p>

	<p>
		<strong>If a data subject contacts us directly</strong> about data we process for you,
		we will not respond to the substance of the request. We will, unless legally
		prohibited, promptly forward it to you and tell the data subject that the request has
		been passed to the controller. Responding is your call, not ours.
	</p>

	<p>
		Assistance under this section is provided at no additional charge for a reasonable
		volume of requests. Where your requests are excessive in volume or require bespoke
		engineering work, we may charge our reasonable costs, agreed with you in advance and
		never as a condition of meeting a statutory deadline.
	</p>

	<h2 id="assistance">10. Assistance with Articles 32 to 36</h2>

	<p>
		Taking into account the nature of processing and the information available to us, we
		assist you in ensuring compliance with your obligations under Articles 32 to 36:
	</p>

	<ul>
		<li>
			<strong>Article 32 (security).</strong> By implementing and maintaining the
			measures in <a href="#annex-ii">Annex II</a>, and by giving you the information you
			need to assess them — including honest answers to a security questionnaire.
		</li>
		<li>
			<strong>Articles 33 and 34 (breach).</strong> As set out in
			<a href="#breach">section 11</a>.
		</li>
		<li>
			<strong>Article 35 (data protection impact assessment).</strong> By providing the
			information about our processing, data flows, sub-processors, retention and
			security measures that you reasonably need to complete a DPIA. This DPA, the
			sub-processor register, our record of processing activities and our security
			documentation are designed to be usable as DPIA inputs; ask at
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> for anything they do not
			cover.
		</li>
		<li>
			<strong>Article 36 (prior consultation).</strong> By providing reasonable
			cooperation and information if you must consult a supervisory authority before
			processing.
		</li>
	</ul>

	<h2 id="breach">11. Personal data breach</h2>

	<p>
		We notify you <strong>without undue delay after becoming aware</strong> of a personal
		data breach affecting personal data we process on your behalf. We do not wait for the
		investigation to finish before telling you; we notify with what we have and update you
		as the picture fills in. We give no fixed hour count here, because a number invented to
		sound reassuring is worse than a commitment we will actually keep, and "without undue
		delay" is the standard Article 33(2) sets for a processor.
	</p>

	<p>Our notice will describe, to the extent known at the time and in phases as more becomes known:</p>

	<ol>
		<li>
			the nature of the breach, including where possible the categories and approximate
			number of data subjects concerned and the categories and approximate number of
			records concerned;
		</li>
		<li>the name and contact details of our contact point for further information;</li>
		<li>the likely consequences of the breach; and</li>
		<li>
			the measures taken or proposed to address the breach, including where appropriate
			measures to mitigate its possible adverse effects.
		</li>
	</ol>

	<p>
		That is the Article 33(3) content set, and we provide it to you so that you can meet
		your own obligation.
		<strong>The 72-hour notification to the supervisory authority is yours, not ours</strong>:
		as controller, you decide whether the breach is notifiable, you notify your lead
		authority within 72 hours of <em>your</em> awareness under Article 33(1), and you decide
		whether Article 34 communication to data subjects is required. We will not notify your
		supervisory authority or your data subjects on your behalf unless you instruct us in
		writing to do so, because a processor notifying in a controller's place creates exactly
		the confusion an authority least wants.
	</p>

	<p>
		We notify the security or privacy contact you have designated in your tenant, and
		failing that the administrator accounts on your tenant. Keep that contact current — a
		notification sent to a stale address is a notification you did not get.
	</p>

	<p>
		A breach at a sub-processor affecting your data is a breach we must tell you about, on
		the same terms. We will cooperate reasonably with your own investigation, preserve the
		relevant audit evidence, and provide the documentation you need for your Article 33(5)
		record. Our giving notice is not an acknowledgement of fault or liability.
	</p>

	<h2 id="audit">12. Audit and information rights</h2>

	<p>
		We make available to you all information necessary to demonstrate compliance with
		Article 28, and allow for and contribute to audits, including inspections, conducted by
		you or by an auditor you mandate.
	</p>

	<p>In the first instance we discharge that by providing:</p>

	<ul>
		<li>this DPA and its annexes;</li>
		<li>
			the sub-processor register at
			<a href="/legal/sub-processors">/legal/sub-processors</a>, which names each
			provider, its data categories and its processing location;
		</li>
		<li>our record of processing activities, so far as it relates to your data;</li>
		<li>the description of technical and organisational measures in <a href="#annex-ii">Annex II</a>;</li>
		<li>written answers to a reasonable security or privacy questionnaire; and</li>
		<li>
			on request, the audit trail for your own tenant — every status transition,
			approval, payment and vendor change, with actor and timestamp — which you can export
			yourself at any time.
		</li>
	</ul>

	<p>
		<strong>We hold no SOC 2 or ISO 27001 report, and we will not pretend otherwise.</strong>
		Many processors answer an audit request by producing an attestation; we cannot, because
		none exists. A SOC 2 Type II examination is an intention of ours, not an achievement,
		and we will say so on this page when that changes rather than before. Plan your own
		assurance accordingly — that is precisely why <a href="#annex-ii">Annex II</a> is
		written as a specific, checkable list instead of a paragraph of adjectives.
	</p>

	<p>
		<strong>Inspection.</strong> Where the information above is not sufficient, you may
		conduct or mandate an inspection, on the following terms: once in any twelve-month
		period, on at least 30 days' written notice, during business hours, subject to
		confidentiality obligations, and conducted so as not to unreasonably disrupt the
		Service. The scope is limited to systems and records used to process your personal
		data, and does not extend to another customer's data, to our own confidential or
		security-sensitive information where disclosure would itself create risk, or to a
		sub-processor's premises. An auditor who is our competitor may be refused on
		reasonable grounds, and we may require a replacement. You bear your own costs; we bear
		ours for the annual audit, and may charge our reasonable costs for any additional one
		you request.
	</p>

	<p>
		Those limits fall away where an inspection is required by a supervisory authority with
		jurisdiction over you, or follows a personal data breach affecting your data. In those
		cases we will cooperate promptly, whatever the notice period and however recently the
		last audit was.
	</p>

	<h2 id="deletion">13. Deletion or return at the end of the service</h2>

	<p>
		On expiry or termination of the Agreement, at your choice, we delete or return your
		personal data, and delete existing copies, unless Union or Member State law (or other
		applicable law) requires us to retain it.
	</p>

	<p>
		<strong>Making the choice.</strong> Tell us which you want at
		<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a> within <strong>30 days</strong>
		of termination. During those 30 days your data remains available for you to export and
		we take no deletion action — those 30 days <strong>are</strong> the reasonable
		opportunity to export that clause 10.4 of the
		<a href="/legal/terms#termination">Terms</a> promises, stated here as a definite number
		rather than left as a second, vaguer undertaking. If you tell us nothing, we proceed to
		deletion at the end of that window — we will not hold your data indefinitely on the
		theory that you might come back, and we will not delete it before you have had a fair
		chance to retrieve it.
	</p>

	<p>
		<strong>Return.</strong> Return means a copy in a structured, commonly used,
		machine-readable format: the Service's own report, audit and Developer-API exports,
		plus, for any record class those do not cover, an extract of your tenant database and
		of your uploaded documents, delivered by a secure channel we agree with you.
	</p>

	<p>
		<strong>Deletion.</strong> Deletion means we destroy your tenant database
		(<code>feoh_&lt;slug&gt;</code>), your control-plane organisation and user records, and
		the object-storage key prefix holding your uploaded invoices, receipts, contracts and
		tax forms. We will complete that within <strong>60 days</strong> of your instruction or
		of the end of the 30-day window, and will confirm in writing when it is done.
	</p>

	<p>
		<strong>What deletion does not reach immediately, and why.</strong> We would rather
		qualify this clause than write an absolute promise we cannot keep:
	</p>

	<ul>
		<li>
			<strong>Backups.</strong> The nightly backup is a <strong>per-database dump, not a
			whole-system snapshot</strong>. Because each tenant has its own database, each
			tenant's backup is its own object in an encrypted, versioned backup store — so
			removing your data from it is an ordinary object deletion rather than surgery on an
			encrypted image, and we will not tell you it cannot be done. We delete your backup
			objects as part of the deletion described above, within the same 60 days. Two
			residues we will not pretend away: the store retains a superseded version of an
			object until the versioning cleanup expires it, currently configured at 30 days, and
			any copy that deletion does not reach ages out on the ordinary backup retention
			cycle, currently configured at 90 days. Where a deployment also keeps a
			volume-level snapshot as a coarse fallback, that copy is not selectively editable
			and persists until the snapshot expires. Until each expires, anything still held
			remains subject to this DPA, is not restored or accessed except to recover the
			Service, and is deleted when it expires. These describe the backup design as our
			infrastructure code defines it; the note on the workload stack in
			<a href="#annex-ii">Annex II</a> applies here too.
		</li>
		<li>
			<strong>Audit records.</strong> The append-only audit log is immutable by design:
			the database refuses to delete or alter a row, so it is never edited. It is still
			<strong>destroyed with your tenant database</strong> when we delete that database,
			which is the same thing clause 10.5 of the
			<a href="/legal/terms#termination">Terms</a> says — deleted with the workspace, not
			edited out of it. What survives is the separate copy: where the deployment has
			enabled write-once archival, an audit event already shipped to that archive cannot
			be deleted before its retention period expires. Audit rows carry the actor, the
			action, the record and what changed, with tax identifiers and bank details reduced to
			last-four form — so no full account number, tax identifier or card number is in them.
			They are retained as evidence of the integrity of the financial record.
		</li>
		<li>
			<strong>Data held with a third party you enabled.</strong> Where you instructed us
			to send data to an ERP, a payment rail, a card issuer, an AI provider or your own
			chat workspace, that copy is held under your relationship with that provider. We
			delete our copy; you must approach them about theirs.
		</li>
		<li>
			<strong>Legally required retention.</strong> Where we must retain a record to
			comply with a legal obligation, we retain only that record, only for as long as
			required, and continue to protect it under this DPA. We will tell you what we have
			retained and why.
		</li>
	</ul>

	<h2 id="transfers">14. International transfers</h2>

	<p>
		We do not transfer your personal data out of the EEA, the United Kingdom or
		Switzerland except in accordance with this section and your instructions.
	</p>

	<p>
		Where such a transfer is made to a country that is not the subject of an adequacy
		decision, the parties agree that the <strong>SCCs</strong> apply and are incorporated
		into this DPA by reference, completed as follows:
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr><th>Clause</th><th>Selection</th></tr>
			</thead>
			<tbody>
				<tr>
					<td>Module</td>
					<td>
						<strong>Module Two</strong> (controller to processor) where you are a
						controller. <strong>Module Three</strong> (processor to processor) where
						you are yourself a processor and we act as your sub-processor, and for
						onward transfers from us to our sub-processors.
					</td>
				</tr>
				<tr><td>Clause 7 (docking)</td><td>Applies.</td></tr>
				<tr>
					<td>Clause 9 (sub-processors)</td>
					<td>
						Option 2, general written authorisation, with the notice period in
						<a href="#sub-processor-changes">section 8</a>.
					</td>
				</tr>
				<tr>
					<td>Clause 11 (redress)</td>
					<td>The optional independent dispute-resolution body does not apply.</td>
				</tr>
				<tr>
					<td>Clause 13 / supervisory authority</td>
					<td>
						The supervisory authority of the Member State in which the data exporter
						is established, or, where the exporter is not established in the EEA, the
						authority determined under Clause 13(a).
					</td>
				</tr>
				<tr>
					<td>Clause 17 (governing law)</td>
					<td>Option 1, the law of Ireland.</td>
				</tr>
				<tr><td>Clause 18(b) (forum)</td><td>The courts of Ireland.</td></tr>
				<tr>
					<td>Annexes I, II and III</td>
					<td>
						Populated by <a href="#annex-i">Annex I</a>,
						<a href="#annex-ii">Annex II</a> and <a href="#annex-iii">Annex III</a>
						of this DPA respectively.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<p>
		<strong>United Kingdom.</strong> For transfers subject to the UK GDPR, the SCCs apply
		as amended by the <strong>UK International Data Transfer Addendum</strong> (the ICO's
		Addendum to the EU SCCs, version B1.0). Table 1 is populated by
		<a href="#annex-i">Annex I</a>; Tables 2 and 3 by the selections above and by Annexes I
		to III; in Table 4, <strong>neither party</strong> may end the Addendum under its
		Section 19. Where you prefer the standalone UK International Data Transfer Agreement
		(IDTA), we will execute it on request at
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>.
	</p>

	<p>
		<strong>Switzerland.</strong> The SCCs apply with the amendments the Federal Data
		Protection and Information Commissioner requires: the FDPIC is the competent authority
		for Swiss transfers, references to the GDPR are read as references to the FADP, the term
		"Member State" is not read so as to prevent a data subject in Switzerland from suing in
		Switzerland, and the clauses also protect the data of legal entities until the FADP
		ceases to do so.
	</p>

	<p>
		If the SCCs or the Addendum are invalidated, replaced or amended, the parties will work
		together in good faith to put the replacement mechanism in place without undue delay. We
		will also provide the information you reasonably need for a transfer impact assessment,
		including what we know about government access requests we have received — which, at
		the time of writing, is none.
	</p>

	<h3 id="residency">Data residency — read this before relying on it</h3>

	<p>
		The Service exposes a data-residency setting, and an administrator can pin a tenant to
		a region. <strong>Nothing routes on that setting today.</strong> All customer data
		currently sits in a single region. Choosing a region records your intent, is written to
		an audit record, and is reported back in the application as "misaligned" where it
		differs from the region the stack actually runs in — but it does not move any data, and
		it is not a residency guarantee. The region customer data is hosted in is
		<Fact value={OPERATOR.hostingRegion} label="hosting region" />. If you have a hard
		residency requirement, do not treat the setting as satisfying it; talk to us at
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a> first.
	</p>

	<h2 id="controller-obligations">15. Your obligations as controller</h2>

	<p>You warrant and undertake that:</p>

	<ul>
		<li>
			you have a lawful basis under Article 6 for the processing you instruct, and where
			required have given the notices and obtained the consents that make our processing
			lawful — including in respect of your suppliers' staff and contacts, who are data
			subjects whether or not they ever log in;
		</li>
		<li>your instructions comply with Data Protection Laws;</li>
		<li>
			you are responsible for the accuracy, quality and legality of the personal data you
			load, and for what you choose to put into free-text fields and uploaded documents;
		</li>
		<li>
			you will not load special categories of personal data under Article 9, nor data
			relating to criminal convictions and offences under Article 10, except as expressly
			agreed with us in writing — the Service is not designed for them. Bank details and
			tax identifiers are not Article 9 data, but we treat them as requiring restricted
			handling and so should you;
		</li>
		<li>
			you manage your own users, roles and permissions, including the segregation of
			duties between the people who create records and the people who approve payment;
		</li>
		<li>
			you keep your security, privacy and notification contacts current, and promptly
			revoke access for people who leave; and
		</li>
		<li>
			you are responsible for the configuration decisions listed in
			<a href="#instructions">section 5</a>, including which integrations you enable and
			what data leaves the Service as a result.
		</li>
	</ul>

	<h2 id="ccpa">16. California — service provider terms</h2>

	<p>
		This section applies where the personal data you process through the Service includes
		the personal information of California residents. Terms used here have the meanings
		given in the CCPA.
	</p>

	<p>
		You are the <strong>Business</strong>. We are a <strong>Service Provider</strong>. You
		disclose personal information to us only for the limited and specified business purpose
		of providing the Service under the Agreement, and this DPA is the written contract the
		CCPA requires.
	</p>

	<p>We certify that we understand the restrictions in this section and will comply with them. We will not:</p>

	<ul>
		<li>
			<strong>sell</strong> personal information, or <strong>share</strong> it for
			cross-context behavioural advertising. We do neither, for any purpose, and receive
			no consideration of any kind for it;
		</li>
		<li>
			retain, use or disclose personal information for any purpose other than the business
			purposes specified in the Agreement, including for any commercial purpose other than
			providing the Service, or as otherwise permitted by the CCPA;
		</li>
		<li>
			retain, use or disclose personal information <strong>outside the direct business
			relationship</strong> between you and us; or
		</li>
		<li>
			combine personal information we receive from you with personal information we
			receive from or on behalf of anyone else, or that we collect from our own
			interaction with a consumer, except as the CCPA and its regulations permit for a
			service provider.
		</li>
	</ul>

	<p>
		We will comply with the applicable obligations the CCPA places on a service provider and
		provide the same level of privacy protection it requires. We will assist you, by the
		mechanisms in <a href="#data-subject-rights">section 9</a>, in responding to consumer
		requests to know, delete, correct, limit the use of sensitive personal information, and
		opt out. We impose these same restrictions on every sub-processor by written contract.
	</p>

	<p>
		We will notify you if we determine that we can no longer meet our obligations under the
		CCPA. You may take reasonable and appropriate steps to stop and remediate any
		unauthorised use of personal information, and you may, on notice, audit our compliance
		on the terms in <a href="#audit">section 12</a>.
	</p>

	<p>
		If we ever create deidentified data, we will take reasonable measures to prevent
		reidentification, will publicly commit to keeping it deidentified, and will contractually
		oblige any recipient to do the same.
	</p>

	<h2 id="liability">17. Liability, precedence and changes</h2>

	<p>
		Each party's liability under this DPA is subject to the exclusions and limitations of
		liability in the <a href="/legal/terms">Terms of Service</a>, except to the extent that
		Data Protection Laws do not permit a limitation — nothing here limits a data subject's
		rights or either party's liability to a supervisory authority.
	</p>

	<p><strong>Order of precedence.</strong> Where documents conflict on the subject of personal data:</p>

	<ol>
		<li>the SCCs and the UK Addendum, where they apply to a transfer;</li>
		<li>a separate DPA that you and we have signed;</li>
		<li>this DPA;</li>
		<li>the <a href="/legal/terms">Terms of Service</a>; and</li>
		<li>any other document, including our Privacy Policy and any order form.</li>
	</ol>

	<p>
		<strong>Changes.</strong> We may update this DPA where a change in law, in the Service,
		or in our sub-processors requires it. We will give at least 30 days' notice of a change
		that materially reduces the protections in it, by the same mechanism as a sub-processor
		change, and you may terminate the affected part of the Service without penalty if you
		object. The "Last updated" date at the top of this page identifies the current version;
		a superseded version is available on request at
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>.
	</p>

	<p>
		<strong>Severance and survival.</strong> If any provision of this DPA is held invalid,
		the rest continues in force. The obligations in sections 5, 6, 9, 11, 12, 13, 14 and 16
		survive termination for as long as we hold any of your personal data.
	</p>

	<p>
		<strong>Governing law.</strong> This DPA is governed by the law that governs the Terms
		of Service — <Fact value={OPERATOR.governingLaw} label="governing law and venue" /> —
		except that the SCCs are governed as stated in <a href="#transfers">section 14</a>.
	</p>

	<h2 id="annex-i">Annex I — Details of the processing</h2>

	<h3 id="annex-i-parties">A. List of parties</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr><th>Role</th><th>Details</th></tr>
			</thead>
			<tbody>
				<tr>
					<td><strong>Data exporter / Controller</strong></td>
					<td>
						The Customer — the organisation that has accepted the Terms of Service and
						uses the Service. Activities relevant to the transfer: operating its
						accounts-payable function. Role: controller, or processor where the Customer
						processes on behalf of its own client. Contact details and signature: as held
						in the Customer's account, or as recorded on a countersigned copy.
					</td>
				</tr>
				<tr>
					<td><strong>Data importer / Processor</strong></td>
					<td>
						{OPERATOR.serviceName}, operated by {OPERATOR.controllerDescription},
						registered as <Fact value={OPERATOR.legalEntity} label="registered legal entity" />,
						of <Fact value={OPERATOR.postalAddress} label="postal address" />. Activities
						relevant to the transfer: providing the accounts-payable platform described in
						the Agreement. Role: processor. Contact:
						<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="annex-i-description">B. Description of the processing</h3>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr><th>Item</th><th>Detail</th></tr>
			</thead>
			<tbody>
				<tr>
					<td><strong>Categories of data subjects</strong></td>
					<td>
						<ul>
							<li>
								The Customer's own personnel who use the Service — AP clerks, AP
								managers, approvers, finance and CFO users, administrators, and
								employees who submit expenses or are named on an expense report.
							</li>
							<li>
								Staff and individual contacts of the Customer's suppliers, named on
								vendor records, invoices, purchase orders, contracts, statements and
								supplier chat.
							</li>
							<li>
								<strong>Sole traders and other individual suppliers</strong>, whose
								business contact and banking data is personal data about them.
							</li>
							<li>
								Supplier-portal users — individuals at a supplier whom the Customer
								invites to submit invoices and view payments.
							</li>
							<li>
								Beneficial owners and other individuals identified for sanctions, KYC
								or tax screening.
							</li>
							<li>
								Any other individual named in a document the Customer uploads or a
								free-text field the Customer completes.
							</li>
						</ul>
					</td>
				</tr>
				<tr>
					<td><strong>Categories of personal data</strong></td>
					<td>
						<ul>
							<li>
								<strong>Identity and contact</strong> — supplier legal and trading
								names, postal addresses, telephone numbers, email addresses; personnel
								names, work email addresses, job titles and roles.
							</li>
							<li>
								<strong>Government identifiers</strong> — tax identification numbers
								including EIN, SSN, VAT registration numbers and other TINs, and
								uploaded W-9 and W-8 tax forms.
							</li>
							<li>
								<strong>Financial account data</strong> — bank account and routing
								numbers, IBAN, sort code, account holder name; virtual-card metadata.
								Full card numbers are never stored by us: a reveal is a single-use call
								to the issuer and we keep only the last four digits.
							</li>
							<li>
								<strong>Beneficial ownership and screening</strong> — beneficial-owner
								details and the results of sanctions, watchlist and risk screening.
							</li>
							<li>
								<strong>Transaction records</strong> — invoices and line items, purchase
								orders, goods receipts, credit memos, payments, payment runs, discount
								offers and the approval history attached to each.
							</li>
							<li>
								<strong>Expense data</strong> — employee expense claims, reports,
								pre-approvals, corporate-card transactions and uploaded receipts.
							</li>
							<li>
								<strong>Contracts</strong> — contract records and uploaded contract
								documents, including the individuals named in them.
							</li>
							<li>
								<strong>Authentication data</strong> — login identifiers, password
								hashes, MFA enrolment state, passkey credentials, session records with
								sign-in IP and a coarse device label.
							</li>
							<li>
								<strong>Communications</strong> — supplier chat messages and
								attachments, notification content, and email sent through the Service.
							</li>
							<li>
								<strong>Audit records</strong> — actor identity, action, record
								identifier, timestamp and what changed. For ordinary business fields
								the entry records the before and after values, because that is what
								makes the trail useful. For the restricted ones it does not: a change
								to a tax identifier or a bank detail is recorded as a change to that
								field with last-four values only, so no full account number, tax
								identifier or card number enters the audit trail.
							</li>
						</ul>
					</td>
				</tr>
				<tr>
					<td><strong>Sensitive data</strong></td>
					<td>
						None intended. The Customer undertakes not to load Article 9 special-category
						data or Article 10 criminal-offence data (see
						<a href="#controller-obligations">section 15</a>). Bank details and government
						tax identifiers are not special categories, but they carry a high risk of
						financial harm, and the measures in <a href="#annex-ii">Annex II</a> treat them
						as restricted: they are minimised in the database, kept out of logs and error
						responses, and excluded from the audit trail's field values.
					</td>
				</tr>
				<tr>
					<td><strong>Frequency of processing</strong></td>
					<td>Continuous, for the duration of the Agreement.</td>
				</tr>
				<tr>
					<td><strong>Nature and purpose</strong></td>
					<td>
						Hosting and operating a multi-tenant accounts-payable platform: ingesting
						invoices, extracting their fields, matching them to purchase orders and
						receipts, routing them for approval, screening suppliers, scheduling and
						recording payments, issuing virtual cards, managing expenses and contracts,
						reporting, and maintaining the audit trail — all on the Customer's
						instructions and for the Customer's purposes only.
					</td>
				</tr>
				<tr>
					<td><strong>Duration of processing</strong></td>
					<td>
						The term of the Agreement, plus the deletion or return period in
						<a href="#deletion">section 13</a>. Within the term there is <strong>no
						automatic expiry</strong>: a configurable retention sweep exists but is
						<strong>off unless a deployment enables it</strong>, and it covers only two
						record classes — invoices and the audit log. Enabled, it <em>archives</em>
						overdue terminal invoices by stamping a marker; it hard-deletes nothing, and
						it never touches an audit row, which is append-only. The configurable window
						defaults to 84 months (7 years), the common tax and SOX baseline, but that
						window only bites once the sweep is turned on. Everything else persists until
						the Customer deletes it or this Addendum's deletion-on-termination clause
						applies.
					</td>
				</tr>
				<tr>
					<td><strong>Transfers to sub-processors</strong></td>
					<td>
						Subject matter, nature and duration as set out in
						<a href="#annex-iii">Annex III</a> and the register at
						<a href="/legal/sub-processors">/legal/sub-processors</a>.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="annex-i-authority">C. Competent supervisory authority</h3>

	<p>
		For the purposes of Clause 13 of the SCCs, the competent supervisory authority is the
		one determined by the establishment of the data exporter — for a Customer established
		in the EEA, the authority of that Member State; where the exporter is not established
		in the EEA but has appointed an Article 27 representative, the authority of the Member
		State in which that representative is established; otherwise the authority of the
		Member State in which the data subjects whose data is transferred are located.
	</p>

	<h2 id="annex-ii">Annex II — Technical and organisational measures</h2>

	<p>
		These are the measures we implement under Article 32 and Clause 8.6 of the SCCs. They
		are written specifically, against the actual system, so that you can verify them rather
		than take them on trust. Where a measure is available but off by default, or is a
		commitment about how we will operate rather than a running fact, the entry says so.
	</p>

	<div class="table-scroll" tabindex="0">
		<table>
			<thead>
				<tr><th>Measure</th><th>What is implemented</th></tr>
			</thead>
			<tbody>
				<tr>
					<td><strong>Separation of customers' data</strong></td>
					<td>
						Each tenant has its <strong>own database</strong>
						(<code>feoh_&lt;slug&gt;</code>) and its own key prefix in object storage.
						Every request resolves its tenant through a <strong>single chokepoint</strong>
						in the backend (<code>app/tenant.py</code>) which
						<strong>cross-checks the organisation claim in the caller's signed token
						against the tenant being requested</strong> and refuses with a 403 if they
						disagree. A forged or swapped tenant header — or a spoofed host on a custom
						domain — therefore cannot widen access on its own. Constructing a tenant
						connection outside that chokepoint is prohibited by the codebase's own rules
						and caught in review.
					</td>
				</tr>
				<tr>
					<td><strong>Access control and authorisation</strong></td>
					<td>
						Role-based access control with four system roles (administrator, AP manager,
						AP clerk, CFO), plus a <strong>granular permission layer</strong> over the
						fraud-sensitive duties — approving an invoice, approving a payment run,
						executing a payment, voiding a payment, approving a supplier bank-detail
						change, blocking a supplier, managing users — so an organisation can split
						those duties across custom roles. Permissions are enforced server-side on
						every affected endpoint, not merely hidden in the interface.
					</td>
				</tr>
				<tr>
					<td><strong>Segregation of duties</strong></td>
					<td>
						The person who created or materially edited a payable cannot approve it, and
						the same set of implicated actors — plus whoever raised a fraud or duplicate
						flag — cannot clear the exception that is blocking its payment. They can
						escalate it, which keeps the payment blocked. This is enforced in the service
						layer at the single chokepoint every approval and clearing path shares.
					</td>
				</tr>
				<tr>
					<td><strong>Authentication of users</strong></td>
					<td>
						Passwords are hashed with <strong>bcrypt over an HMAC-SHA256 pre-hash</strong>,
						so a long or high-entropy password is not silently truncated to bcrypt's 72-byte
						limit; the hash runs off the request event loop so it cannot be used to stall the
						service. <strong>TOTP multi-factor authentication and WebAuthn passkeys</strong>
						are supported, with step-up re-authentication required to change an
						authentication factor. Per-account login failure budgets throttle guessing.
						Enterprise customers can require SSO over OIDC or SAML 2.0 against their own
						identity provider, with SCIM 2.0 provisioning and de-provisioning. Sessions can
						be listed and revoked by the account holder, and logout revokes the token
						through a server-side blocklist.
					</td>
				</tr>
				<tr>
					<td><strong>Integrity of the record (append-only audit)</strong></td>
					<td>
						Status transitions on invoices, payments, approvals and suppliers write audit
						rows, and the audit table is <strong>append-only enforced in the database
						itself</strong>: a row-level trigger rejects every delete and every update other
						than the shipper's dispatch stamp, so no path through the application — and no
						administrator acting through it — can rewrite history without the database
						refusing. We would rather state the control's edge than overstate it: being a
						row trigger, it does not fire on a table-level <code>TRUNCATE</code>, and a role
						holding ownership of the database could disable it. That is the gap the
						write-once archival below exists to close. Approval actions are recorded
						against an identity for non-repudiation.
					</td>
				</tr>
				<tr>
					<td><strong>Write-once archival of audit events</strong></td>
					<td>
						A shipper can copy audit events to write-once sinks — an S3 bucket with Object
						Lock, or CloudWatch Logs. Our infrastructure code provisions that bucket with
						Object Lock in <strong>compliance mode</strong> and a seven-year default
						retention. The shipper relies on that bucket default rather than stamping a
						retention period onto each object, and at start-up it verifies that the bucket
						has Object Lock enabled, that its default retention rule is compliance mode
						rather than governance mode, and that the period is at least seven years. It
						refuses to start if any of the three is not true, so a bucket built some other
						way stops the deployment rather than quietly weakening the archive.
						<strong>The capability is available and configurable, not always on</strong>:
						shipping is disabled by default and the default adapter is an in-process mock,
						so it is enabled per deployment. We describe it this way deliberately rather
						than implying every deployment ships to WORM storage today.
					</td>
				</tr>
				<tr>
					<td><strong>Controls on the money path</strong></td>
					<td>
						A change to a supplier's bank details is <strong>dual control</strong>: it is
						raised as a change request and takes effect only when a second, separately
						permissioned person approves it — the standard defence against invoice-redirection
						fraud. Suppliers are screened against sanctions and watchlists before payment
						where a screening provider is enabled; duplicate, fraud-flag and reconciliation
						exceptions block a payment run while open. Amounts are exact decimals throughout,
						never floating point, and operations that move money are idempotent at the API
						boundary.
					</td>
				</tr>
				<tr>
					<td><strong>Data minimisation and pseudonymisation</strong></td>
					<td>
						Bank numbers, card numbers and tax identifiers are minimised in the database to
						last-four forms wherever the full value is not required; full card numbers are
						never persisted by us. Personal and banking data is kept out of application logs
						and out of HTTP error bodies as an enforced project invariant, with automated
						tests guarding it. Audit entries record before and after values for ordinary
						business fields, but reduce the restricted ones — tax identifiers, bank
						details — to last-four form, so the trail shows that a bank field changed
						without carrying the number. Supplier enrichment lookups mask the tax
						identifier before any external call. Erasure
						replaces identifiers with non-identifying tombstones rather than blanks, so a
						record can be correlated to its erasure without revealing what it held.
					</td>
				</tr>
				<tr>
					<td><strong>Secrets management</strong></td>
					<td>
						Deployed secrets are <strong>encrypted with sops under an AWS KMS key</strong>
						and held in a private repository, outside the application source repository,
						which is public. No secret has a hardcoded fallback: an unset secret means the
						feature is off and fails closed, never a default that silently works.
						Continuous integration fails if an encrypted secret file is ever committed to
						the application repository, and the KMS key has annual rotation enabled.
					</td>
				</tr>
				<tr>
					<td><strong>Security of integrations</strong></td>
					<td>
						Every inbound webhook handler <strong>verifies the provider's HMAC
						signature</strong> and <strong>deduplicates by event id</strong> through shared
						helpers, and returns an empty 204 on every rejection path so responses cannot be
						used to enumerate. Outbound URLs supplied by a user are checked against
						server-side request forgery. Every external integration is an adapter with a
						local default that fails closed when no credential is present, so a
						misconfiguration does not become an unintended disclosure.
					</td>
				</tr>
				<tr>
					<td><strong>Encryption</strong></td>
					<td>
						The Service is designed to be served only over HTTPS, and the infrastructure
						code provisions the TLS certificate for the platform domain and its tenant
						subdomains. Every storage bucket it defines is
						configured with KMS-managed server-side encryption, versioning, public-access
						blocking, server-access logging and — for the invoice and audit archives —
						S3 Object Lock. <strong>The AWS workload stack (database, API runtime, CDN) is
						not yet deployed</strong>, so these describe how the Service is built and how it
						will be operated rather than a running production estate. We commit not to
						process a customer's production personal data other than over an encrypted
						transport and on encrypted-at-rest storage.
					</td>
				</tr>
				<tr>
					<td><strong>Data-subject tooling</strong></td>
					<td>
						Subject-access export and erasure are built into the product, restricted to
						administrators in your own tenant, scoped so a subject in another tenant is
						neither visible nor erasable, and audited. See
						<a href="#data-subject-rights">section 9</a>, including its three stated limits.
					</td>
				</tr>
				<tr>
					<td><strong>Retention enforcement</strong></td>
					<td>
						Per-record-class retention windows are configurable per organisation and
						enforced by an audited sweep that <strong>archives rather than deletes</strong>,
						never touches an audit row, and records a manifest of what it did. The default
						window is 84 months. <strong>The sweep is off by default</strong> and covers
						invoices and the audit log only — we list it here as an available control, not
						as one running on your tenant unless it has been enabled.
					</td>
				</tr>
				<tr>
					<td><strong>Availability and restoration</strong></td>
					<td>
						Backups are nightly per-database dumps — the control plane and every tenant
						database separately — versioned, encrypted with a managed key, and
						lifecycle-expired on a defined cycle (90 days as configured). Restoration is
						per-database and scripted: a single tenant can be restored from its own dump
						without replaying the whole estate, though the Service is out of use while a
						restore runs. A written backup and disaster-recovery procedure exists and is
						maintained alongside the code. For what that granularity means when you ask us
						to delete, see <a href="#deletion">section 13</a>.
					</td>
				</tr>
				<tr>
					<td><strong>Secure development and change control</strong></td>
					<td>
						Changes go through version control and review, with a security review pass on
						anything touching authentication, tenant isolation, the money path, webhooks or
						personal data. An automated test suite runs in continuous integration and
						includes explicit guards for the invariants above — tenant isolation, password
						hashing, non-blocking handling of sensitive operations, and the exclusion of
						personal data from logs. Dependencies are monitored and updated, and
						infrastructure changes are made through version-controlled infrastructure code.
					</td>
				</tr>
				<tr>
					<td><strong>Personnel</strong></td>
					<td>
						Access limited to those who need it, under confidentiality obligations that
						survive the engagement, as described in
						<a href="#confidentiality">section 6</a>.
					</td>
				</tr>
				<tr>
					<td><strong>Incident handling</strong></td>
					<td>
						A written breach-response procedure covers detection, the awareness timestamp,
						containment, notification to affected controllers without undue delay, evidence
						preservation using the immutable audit trail, and a post-incident review. The
						notification obligations it implements are those in
						<a href="#breach">section 11</a>.
					</td>
				</tr>
				<tr>
					<td><strong>Measures for sub-processors</strong></td>
					<td>
						As described in <a href="#sub-processor-flow-down">section 8</a>: written
						contracts imposing no less protective obligations, and our full liability for
						their performance.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h3 id="annex-ii-not-claimed">Measures we do not claim</h3>

	<p>
		A list of security measures is only useful if the omissions are visible. These are the
		things a procurement questionnaire commonly asks for that we do <strong>not</strong>
		have today. If any of them is a hard requirement for you, you should know before you
		sign rather than after.
	</p>

	<ul>
		<li>
			<strong>No SOC 2 or ISO 27001 certification.</strong> Neither is held, and no
			report exists to produce under <a href="#audit">section 12</a>. A SOC 2 Type II
			examination is an intention, not an achievement.
		</li>
		<li>
			<strong>No third-party penetration test.</strong> No external penetration test or
			red-team exercise has been carried out.
		</li>
		<li>
			<strong>No 24/7 security operations centre and no continuous security
			monitoring.</strong> We are not staffed for round-the-clock monitoring, and we do
			not claim an average detection time.
		</li>
		<li>
			<strong>No data-residency guarantee.</strong> See
			<a href="#residency">section 14</a>. The setting exists; the routing does not.
		</li>
		<li>
			<strong>In-product erasure does not reach uploaded documents.</strong> The erasure
			function redacts the databases; deleting a stored invoice, receipt, contract or tax
			form from object storage is done by us on request. See
			<a href="#data-subject-rights">section 9</a>.
		</li>
		<li>
			<strong>In-product erasure does not revoke a registered passkey or an issued
			session record.</strong> Access itself stops on the next request, but the stored
			authenticator credential survives the automated path, and the record of a session
			already issued persists until it expires. Destroying the credential is done by us
			on request. See <a href="#data-subject-rights">section 9</a>.
		</li>
		<li>
			<strong>Write-once audit archival is off by default.</strong> The capability exists
			and is configurable per deployment; it is not on everywhere.
		</li>
	</ul>

	<h2 id="annex-iii">Annex III — Sub-processors and transfer mechanisms</h2>

	<p>
		The authorised sub-processors, what each processes, the categories of data it can
		receive and where it processes are maintained at
		<strong><a href="/legal/sub-processors">/legal/sub-processors</a></strong>, which is
		incorporated into this DPA and into Annex III of the SCCs by reference. That page also
		marks which entries are engaged today and which are merely available until an operator
		or a tenant enables them.
	</p>

	<p>
		<strong>Three sub-processors are engaged in every deployed environment, by design, before
		the Controller configures anything</strong>: the infrastructure provider that hosts the
		Service; <strong>Anthropic</strong>, which receives uploaded invoice documents for
		extraction (see <a href="#ai-processing">Artificial intelligence and machine learning</a>);
		and <strong>hCaptcha</strong>, which receives the IP address of anyone using the public
		signup form and without which the Service refuses to start. Every other entry becomes
		active only when a credential is configured. Local development runs entirely on a
		contributor's own machine with no third party at all.
	</p>

	<p>
		For each sub-processor outside the EEA, the United Kingdom or Switzerland that receives
		personal data originating there, the transfer basis is an adequacy decision where one
		covers it, and otherwise the SCCs (Module Three for the onward transfer) with the UK
		Addendum where UK data is involved, completed as set out in
		<a href="#transfers">section 14</a>. Where a provider's own data processing agreement
		and sub-processing schedule govern, the register records that, and we will provide the
		relevant terms on request.
	</p>

	<h2 id="contact">18. How to contact us about this DPA</h2>

	<p>
		Data-protection questions, instructions, data-subject requests and objections to a
		sub-processor: <a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>.
	</p>

	<p>
		Security incidents and vulnerability reports:
		<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>.
	</p>

	<p>
		Execution of a countersigned copy, a negotiated DPA, the standalone UK IDTA, or any
		question about precedence with your own paper:
		<a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>.
	</p>

	<p>
		Related documents: the <a href="/legal/privacy">Privacy Policy</a> (what we hold as a
		controller in our own right), the <a href="/legal/terms">Terms of Service</a> (which
		this DPA forms part of), the <a href="/legal/sub-processors">Sub-processors</a> register
		(incorporated here), and the <a href="/legal/cookies">Cookie Notice</a>.
	</p>
</LegalPage>
