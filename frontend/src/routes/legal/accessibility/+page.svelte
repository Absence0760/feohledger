<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';

	/**
	 * The published accessibility statement.
	 *
	 * The substance is not written here — it is derived from
	 * `docs/accessibility-vpat.md` (the criterion-by-criterion self-assessment)
	 * and `docs/accessibility.md` (the internal conformance statement). Those
	 * two are the source of truth; this page is the customer-facing rendering
	 * of them, and a change to a conformance claim belongs in all three.
	 *
	 * Why it is published rather than left in `docs/`: the European
	 * Accessibility Act makes a dated, public statement an obligation for an
	 * in-scope service, and a B2B procurement questionnaire asks for one by
	 * name. A statement that lives only in a repository is not published.
	 *
	 * The tone to keep: this document's value is that it admits what is not yet
	 * verified. A statement claiming full conformance behind a self-assessment
	 * with an outstanding manual pass would be the same class of error as
	 * claiming a certification — see `tests-e2e/legal/pages.spec.ts`.
	 */
</script>

<LegalPage
	title="Accessibility Statement"
	intro="{OPERATOR.serviceName} is built to be usable by people who navigate with a keyboard, a screen reader, a magnifier, switch access or voice control. This statement says which standard we hold ourselves to, what we have verified, what we have not verified yet, and how to tell us when something blocks you."
>
	<h2 id="commitment">1. Our commitment</h2>
	<p>
		Accounts payable is work people do all day, and for many of them it is work they do
		with assistive technology. We treat an accessibility barrier in {OPERATOR.serviceName}
		as a defect in the product — not as an enhancement request — and we fix it at the
		source rather than documenting a workaround.
	</p>
	<p>
		This statement is a <strong>self-assessment</strong>. No third party has audited
		{OPERATOR.serviceName} for accessibility conformance, and we do not claim a
		certification. What we can offer instead is specificity: the sections below name the
		standard, the surfaces, the automated guards that run on every change, and the
		criteria we have deliberately not yet claimed.
	</p>

	<h2 id="scope">2. What this statement covers</h2>
	<p>Three end-user surfaces, all of them in scope:</p>
	<ul>
		<li>
			<strong>The web application</strong> — the invoice, approval, vendor, payment,
			exception, analytics and administration screens used by finance staff, plus the
			marketing, signup and sign-in pages and these legal documents.
		</li>
		<li>
			<strong>The supplier portal</strong> — the separate surface a supplier signs into
			to submit an invoice, check a payment, or read this page.
		</li>
		<li>
			<strong>The mobile application</strong> — the iOS and Android app covering the
			approval workflow, document capture and notifications.
		</li>
	</ul>
	<p>
		Out of scope, because they are not ours to make conformant: pages hosted by a third
		party that your organisation chooses to connect — an identity provider's own hosted
		sign-in screen, a payment provider's card-capture page — and documents your
		organisation uploads. Section 7 covers both, because "out of scope" should not mean
		"not our problem".
	</p>

	<h2 id="standard">3. The standard we hold ourselves to</h2>
	<p>
		Our conformance target is the
		<a href="https://www.w3.org/TR/WCAG22/" rel="noopener noreferrer"
			>Web Content Accessibility Guidelines (WCAG) 2.2, Level AA</a
		>, across all three surfaces.
	</p>
	<p>
		That target was chosen because it satisfies the instruments a customer is most likely
		to be asking about. EN 301 549, the European harmonised standard that the
		<strong>European Accessibility Act</strong> (Directive (EU) 2019/882) is assessed
		against, takes its web and software requirements from WCAG; targeting 2.2 Level AA
		meets or exceeds the version it incorporates. In the United States, Section 508 and
		the Americans with Disabilities Act are read against the same guidelines, and in the
		United Kingdom the Equality Act 2010's reasonable-adjustment duty is measured against
		them in practice.
	</p>
	<p>
		<strong>Our current status against that target is
		<em>partially conformant</em></strong>: the great majority of success criteria are met
		and guarded automatically, and a small number are not yet <em>claimed</em> because the
		evidence for them requires a human using real assistive technology, which we have not
		finished. Section 6 lists exactly which ones and why. We would rather publish a
		partial claim that is true than a full claim that is convenient.
	</p>

	<h2 id="verification">4. How we verify it</h2>
	<p>
		Accessibility here is a continuously enforced property, not a periodic review. Four
		automated guards run on every change before it can merge:
	</p>
	<ul>
		<li>
			<strong>Automated rule checking.</strong> The
			<a href="https://github.com/dequelabs/axe-core" rel="noopener noreferrer">axe-core</a>
			engine runs against the working application — the dashboard, invoice list and detail,
			vendors, payments, exceptions, reports, billing, the administration section, both
			sign-in surfaces and every one of these legal documents — at the full Level A and
			Level AA rule set for WCAG 2.0, 2.1 and 2.2. Any violation fails the build.
		</li>
		<li>
			<strong>A contrast scan over the stylesheets themselves.</strong> Rule checking only
			sees what a tested page happens to render, so a separate scan resolves every colour
			pair in the application through the design tokens — including pairs that exist only
			after compositing, such as a translucent badge tint over a surface. This is what
			covers a screen no automated walkthrough happens to visit.
		</li>
		<li>
			<strong>A structural navigability check.</strong> The bypass ("skip to content") link,
			named page regions, a single top-level heading per page, no forced tab order, single-column
			reflow at 320 pixels with no sideways page scrolling, and focus that is trapped inside
			an open dialog and returned to where it came from when the dialog closes.
		</li>
		<li>
			<strong>Mobile semantics tests.</strong> On iOS and Android, automated checks assert
			that controls expose a name, a role and a state to the platform accessibility service,
			that tap targets meet the minimum size, and that text meets contrast.
		</li>
	</ul>
	<p>
		On top of those, keyboard-only walkthroughs of the core invoice → approve → pay flow
		are part of our review process. The layer that is <em>not</em> finished is the
		device-based screen-reader pass described in the next section but one.
	</p>

	<h2 id="features">5. What is in place today</h2>
	<ul>
		<li>
			<strong>Everything is operable from the keyboard.</strong> A row you can click is a
			real link you can tab to and activate, not a click handler on a table row. Dialogs
			trap focus while open, close on <code>Esc</code>, and return focus to the control that
			opened them.
		</li>
		<li>
			<strong>Focus is always visible</strong>, on every interactive element, and the
			indicator itself meets the contrast minimum.
		</li>
		<li>
			<strong>Nothing requires a drag or a gesture.</strong> The one surface built around
			dragging — the workflow builder — gives every step explicit <em>Move up</em> and
			<em>Move down</em> buttons, so the same reordering is available to a keyboard, a
			switch or a single pointer. No path-based or multi-finger gesture is required anywhere.
		</li>
		<li>
			<strong>Meaning is never carried by colour alone.</strong> Invoice status, validation
			errors and warnings all carry text.
		</li>
		<li>
			<strong>Targets are at least 24 by 24 pixels</strong>, including the row-selection
			checkbox in dense tables, which carries a full-size hit area without changing the
			layout.
		</li>
		<li>
			<strong>Text scales and pages reflow.</strong> Content reaches 200% zoom and 320
			pixels of width without loss, in a single column, with no horizontal page scrolling.
			A wide data table scrolls within its own region — and that region is itself
			focusable, so it can be panned with arrow keys rather than only with a mouse.
		</li>
		<li>
			<strong>Status messages are announced without stealing focus</strong>, so a background
			result is heard rather than pulling you out of what you were typing.
		</li>
		<li>
			<strong>Motion respects your system setting.</strong> If your operating system asks
			for reduced motion, transitions are reduced.
		</li>
		<li>
			<strong>Sign-in works with a password manager.</strong> Fields carry the right
			autofill hints, pasting is never blocked, and no step imposes a memory or puzzle test.
		</li>
	</ul>

	<h2 id="limitations">6. What we have not yet verified</h2>
	<p>
		These are criteria where we have <em>no evidence of a defect</em> but also no
		completed human verification. Automated tooling cannot assert them — they turn on
		judgements like whether an announcement is understandable in context, or whether a
		focused control is visually obscured by a sticky header at a particular scroll
		position. Until the device pass described below is signed off, we do not claim them:
	</p>
	<ul>
		<li><strong>Focus Not Obscured (2.4.11)</strong> — that a focused control is never hidden behind a sticky header or an open panel.</li>
		<li><strong>Focus Order (2.4.3)</strong> through the most complex widgets — the workflow builder and the multi-step dialogs.</li>
		<li><strong>Link Purpose in Context (2.4.4)</strong> — a complete audit of how every link's text reads when heard on its own.</li>
		<li><strong>Content on Hover or Focus (1.4.13)</strong> — verification of every tooltip and popover surface.</li>
		<li><strong>Consistent Help (3.2.6)</strong> — that a help affordance appears in the same relative position on every screen.</li>
		<li><strong>Redundant Entry (3.3.7)</strong> — a complete pass over every multi-step flow for information we ask for twice.</li>
		<li><strong>Accessible Authentication (3.3.8)</strong> — confirmation that no sign-in or verification step imposes a cognitive-function test without an alternative.</li>
	</ul>
	<p>
		<strong>The outstanding work is a manual screen-reader pass on real devices</strong> —
		VoiceOver, NVDA and TalkBack — against a written checklist we maintain for the
		purpose. It needs physical assistive-technology hardware, which is why it cannot run
		in our automated pipeline. When it is signed off, the criteria above move to a
		verified status and this statement is updated and re-dated.
	</p>
	<p>
		If you need the full criterion-by-criterion report — the accessibility conformance
		report in the VPAT<sup>®</sup> format that procurement teams ask for — write to
		<a href="mailto:{CONTACT.support}">{CONTACT.support}</a> and we will send the current
		version. It is maintained alongside the code rather than commissioned for a bid.
	</p>

	<h2 id="not-ours">7. Content we do not control</h2>
	<p>
		Three things can appear inside {OPERATOR.serviceName} whose accessibility is not
		determined by us. We would rather name them than let them read as gaps in the claims
		above.
	</p>
	<ul>
		<li>
			<strong>Invoices and other documents your suppliers upload.</strong> A scanned or
			untagged PDF is not made accessible by the application displaying it. This is why
			every extracted field — supplier, dates, amounts, tax, line items — is always
			available as ordinary text beside the document preview: the data is readable even
			when the source file is not.
		</li>
		<li>
			<strong>Your organisation's own branding.</strong> A customer on a plan that allows
			custom colours can set an accent that does not meet contrast. We show the real
			contrast ratio as the colour is typed, in both places it can be set, but we do not
			refuse the choice — the brand belongs to the customer. If you are using
			{OPERATOR.serviceName} through an employer or a reseller and the colours are hard to
			read, tell us and we will raise it with them.
		</li>
		<li>
			<strong>Third-party hosted pages.</strong> If your organisation connects its own
			identity provider, the sign-in screen you see is that vendor's, under their
			conformance, not ours.
		</li>
	</ul>

	<h2 id="alternatives">8. If something blocks you</h2>
	<p>
		Tell us, and we will get you the result another way while we fix the cause. That is a
		standing offer, not a formality: the data in {OPERATOR.serviceName} is available
		through exports and through our API as well as through the screens, so there is
		usually a route to the same outcome. If an approval, a payment or a document is
		blocked by a barrier in our software, we will not leave you waiting on a release to
		get your work done.
	</p>

	<h2 id="feedback">9. Telling us about a barrier</h2>
	<p>
		Write to <a href="mailto:{CONTACT.support}">{CONTACT.support}</a> with
		<em>accessibility</em> in the subject line. It helps if you can include:
	</p>
	<ul>
		<li>the page or screen, and what you were trying to do;</li>
		<li>the assistive technology and version, and the browser or device;</li>
		<li>what happened, and what you expected instead.</li>
	</ul>
	<p>
		None of that is required — a sentence describing what stopped you is enough, and we
		would rather have an imprecise report than none. We aim to acknowledge an
		accessibility report within five working days and to tell you what we can do and when.
		A barrier that blocks a task is triaged as a defect, not as a feature request.
	</p>

	<h2 id="enforcement">10. If our response is not good enough</h2>
	<p>
		If you have reported a barrier and are not satisfied with how we handled it, you can
		escalate outside the company. Which route applies depends on where you are:
	</p>
	<ul>
		<li>
			<strong>In the European Union</strong>, the European Accessibility Act is enforced by
			a designated national authority in each member state, and each state also provides a
			complaints procedure. Your national authority's route applies to us to the extent our
			service is in scope of that state's implementation.
		</li>
		<li>
			<strong>In the United Kingdom</strong>, the Equality Act 2010's duty to make
			reasonable adjustments applies, and the Equality Advisory and Support Service can
			advise you on it.
		</li>
		<li>
			<strong>In the United States</strong>, our conformance report in VPAT<sup>®</sup>
			format is the document a Section 508 or ADA assessment normally works from, and we
			will provide it on request.
		</li>
	</ul>
	<p>
		We would much rather hear from you first — an escalation tells us about a barrier we
		already failed to fix twice.
	</p>

	<h2 id="preparation">11. How this statement was prepared</h2>
	<p>
		This statement was prepared by the team that builds {OPERATOR.serviceName}, by
		self-assessment, from the conformance report and the automated results described in
		section 4. It is reviewed whenever a claim in it changes — a criterion moving to
		verified, a new surface shipping, or a barrier being found — and the date at the top
		of this page is the date of the most recent such review. It is not an annual
		formality; a stale accessibility statement is a misleading one.
	</p>
	<p>
		For how we handle personal data, see the <a href="/legal/privacy">Privacy Policy</a>.
		For the agreement governing use of the service, see the
		<a href="/legal/terms">Terms of Service</a>.
	</p>
</LegalPage>
