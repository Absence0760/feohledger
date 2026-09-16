<script lang="ts">
	import LegalPage from '$lib/legal/LegalPage.svelte';
	import { resetConsent } from '$lib/components/ConsentBanner.svelte';
	import { CONTACT, OPERATOR } from '$lib/legal/operator';

	// Withdrawal has to be as easy as consent. Describing "clear your browser's
	// site data" as the mechanism was accurate but not equivalent — so the
	// notice now carries the control instead of instructions for one.
	let reopened = $state(false);

	// The button is inert until the component has mounted: its whole job is to
	// touch localStorage and reopen the banner, neither of which exists before
	// hydration. This page's markup renders ahead of that (the legal routes are
	// deliberately served without waiting on the tenant probe), so without the
	// gate there is a real window where the control looks ready, takes a click,
	// and silently does nothing. Disabling it until `ready` closes that window
	// and gives the e2e spec a truthful signal to wait on rather than a sleep.
	let ready = $state(false);
	$effect(() => {
		ready = true;
	});

	function changeChoice() {
		resetConsent();
		reopened = true;
	}
</script>

<LegalPage
	title="Cookie Notice"
	intro="{OPERATOR.serviceName} does not set cookies. This notice explains what the app stores in your browser instead, why that storage is still covered by the same law that governs cookies, and how to control the one category of it that is optional."
>
	<h2 id="summary">1. In short</h2>
	<p>
		{OPERATOR.serviceName} sets no cookies. Everything the app keeps in your browser is <strong
			>Web Storage</strong
		>
		— <code>localStorage</code> or <code>sessionStorage</code> — never an HTTP cookie. There are six
		such items in total, listed in full in the table below. Four of them exist only to keep you signed
		in or to remember a choice you made (your language, which legal entity you're viewing); none of
		them exist for advertising, and none of them exist for analytics or tracking, because {OPERATOR.serviceName}
		does not run any analytics or tracking code anywhere in the app today. The consent banner you may
		see offers a category reserved for that kind of storage in case it is ever added — nothing is loaded
		under it right now.
	</p>
	<p>
		For the wider picture of what personal data {OPERATOR.serviceName} holds and why, see the <a
			href="/legal/privacy">Privacy Policy</a
		>.
	</p>

	<h2 id="scope">2. Why this notice exists even though there are no cookies</h2>
	<p>
		The EU ePrivacy Directive (2002/58/EC, Article 5(3), implemented in national law such as the
		UK's Privacy and Electronic Communications Regulations) is not written around cookies
		specifically. It governs <em
			>storing information, or gaining access to information already stored, in the terminal
			equipment of a subscriber or user</em
		> — any technique that writes to or reads from your device counts, whether it is a cookie, a browser
		database, or a device fingerprint. <code>localStorage</code> and <code>sessionStorage</code> are
		both terminal-equipment storage in exactly that sense, so the same rule that would apply to a cookie
		applies to them: storage that is strictly necessary to provide the service you asked for needs no
		consent, and anything beyond that needs consent before it is set. This notice exists to describe
		that storage honestly, rather than to claim the law doesn't apply because the storage mechanism
		isn't a cookie.
	</p>

	<h2 id="inventory">3. Everything stored in your browser</h2>
	<p>
		This is the complete list. Nothing else is written to <code>localStorage</code> or
		<code>sessionStorage</code>
		by the {OPERATOR.serviceName} application, and the application sets no cookies at all.
	</p>
	<div class="table-scroll">
		<table>
			<thead>
				<tr>
					<th scope="col">Key</th>
					<th scope="col">Where</th>
					<th scope="col">Purpose</th>
					<th scope="col">Category</th>
					<th scope="col">How long it lasts</th>
				</tr>
			</thead>
			<tbody>
				<tr>
					<td><code>auth_token</code></td>
					<td>localStorage</td>
					<td>
						Holds your signed-in JWT for the main {OPERATOR.serviceName} app, so requests to the
						API can prove who you are.
					</td>
					<td>Strictly necessary</td>
					<td>
						Until you log out, your session token expires, or the app receives an unauthorized
						response from the API and clears it automatically.
					</td>
				</tr>
				<tr>
					<td><code>portal_auth_token</code></td>
					<td>localStorage</td>
					<td>
						The equivalent signed-in token for the supplier portal, used by vendor contacts
						submitting or reviewing their own invoices, rather than by internal AP staff.
					</td>
					<td>Strictly necessary</td>
					<td>
						Same as <code>auth_token</code> above — cleared on logout, expiry, or an unauthorized
						response.
					</td>
				</tr>
				<tr>
					<td><code>selected_entity_id:&lt;tenant&gt;</code></td>
					<td>localStorage</td>
					<td>
						Remembers which legal entity (subsidiary) you last selected, in an organization with
						more than one, so the app can send it back to the API as the entity-scoping header on
						your next visit. The key is stamped with your organization so switching organizations
						never carries one company's selection into another.
					</td>
					<td>Functional / necessary for the feature it supports</td>
					<td>
						Until you pick a different entity, or choose the consolidated "all entities" view,
						which removes it. No automatic expiry.
					</td>
				</tr>
				<tr>
					<td><code>feoh_locale</code></td>
					<td>localStorage</td>
					<td>Remembers the display language you picked for the interface.</td>
					<td>Functional / necessary for the feature it supports</td>
					<td>Until you change your language again. No automatic expiry.</td>
				</tr>
				<tr>
					<td><code>feoh_consent_choice</code></td>
					<td>localStorage</td>
					<td>
						Records the choice you made in the storage-consent banner itself, so the banner does
						not ask again on every visit.
					</td>
					<td>
						Necessary — and specifically exempt under ePrivacy Art 5(3), since recording a
						consent choice cannot itself require consent.
					</td>
					<td>
						Until you clear this site's browser data, at which point the banner reappears and
						asks again.
					</td>
				</tr>
				<tr>
					<td><code>mfa_challenge</code></td>
					<td>sessionStorage</td>
					<td>
						Holds a short-lived reference to your in-progress multi-factor sign-in, between the
						password step and the second-factor step.
					</td>
					<td>Strictly necessary</td>
					<td>
						Removed as soon as you complete or cancel the second factor; <code
							>sessionStorage</code
						> also clears itself the moment the browser tab closes, whether or not that happens.
					</td>
				</tr>
			</tbody>
		</table>
	</div>

	<h2 id="categories">4. Strictly necessary vs. optional storage — and today, nothing is optional</h2>
	<p>
		Strictly necessary storage is storage without which the feature you invoked cannot work at
		all — you ask to sign in, so the app must hold a token somewhere to keep you signed in.
		ePrivacy Art 5(3) exempts this category from needing consent, and the token and MFA-challenge
		rows above fall into it plainly.
	</p>
	<p>
		The entity selection and language preference sit a step further out: nothing about the app
		breaks without them, but the alternative is that you would re-pick your entity and your
		language on every single page load. We treat both as necessary to the feature you have
		explicitly invoked — choosing an entity, choosing a language — rather than as optional, and
		neither is gated behind the consent banner: refusing non-essential storage still lets you
		switch entities and languages normally.
	</p>
	<p>
		The banner also shows a category for optional, non-essential storage, currently labelled
		"Analytics (optional)." As of this notice, <strong>nothing is loaded under that category</strong
		>: {OPERATOR.serviceName} runs no analytics, advertising, or tracking code of any kind, first-party
		or third-party, on any page. The category exists so that consent can be collected before such code
		is ever added — not because it is active today. If that changes, this notice and the banner will
		both be updated in the same change that adds it, and the new storage will only ever be set for
		someone who has actively accepted the category going forward, never retroactively for someone who
		accepted before it existed.
	</p>

	<h2 id="consent">5. Giving, refusing, and withdrawing consent</h2>
	<p>
		The first time you visit, a banner offers two choices, presented at the same size and in the
		same place: <strong>"Reject non-essential"</strong> and <strong>"Accept all."</strong> Refusing
		is exactly as easy as accepting — one click either way, with neither option styled to stand
		out over the other or hidden behind an extra menu. A "Manage" link lets you read what each
		category covers before you choose. Whichever you pick is recorded in
		<code>feoh_consent_choice</code>, and the banner does not ask again until that record is gone.
	</p>
	<p>
		<strong>To withdraw or change a choice you already made, use the button below.</strong> It
		forgets the stored choice and brings the banner straight back, so changing your mind costs
		the same single click that recording it did. Nothing else in the table above is touched —
		you stay signed in.
	</p>
	<p>
		<button type="button" class="consent-reset" disabled={!ready} onclick={changeChoice}>
			Change your privacy choice
		</button>
		{#if reopened}
			<span class="consent-reset-note" role="status">
				Your stored choice has been cleared — the banner is showing again.
			</span>
		{/if}
	</p>
	<p>
		Clearing this site's browser data does the same thing, if you would rather: it deletes
		<code>feoh_consent_choice</code> along with everything else in the table above, and the banner
		reappears on your next visit. A private or incognito window never picks up a stored choice in
		the first place.
	</p>

	<h2 id="refuse">6. What happens if you refuse</h2>
	<p>
		Nothing in the product changes if you choose "Reject non-essential." You can sign in, upload
		and review invoices, run approvals, and use the supplier portal exactly as before, because
		every piece of storage that makes those things work is in the strictly-necessary or functional
		rows above, not behind your choice. The only storage your choice actually governs is whatever
		might one day load under the optional category described in Section 4 — and since nothing
		loads under it today, refusing is, honestly, a no-op recorded for the future. It will start
		mattering, and will be honored, the day an optional category actually begins loading
		something.
	</p>

	<h2 id="gpc-dnt">7. Global Privacy Control and Do Not Track</h2>
	<p>
		{OPERATOR.serviceName} does not currently detect or act on the <strong
			>Global Privacy Control</strong
		>
		(GPC) signal or the legacy <strong>Do Not Track</strong> (DNT) browser header — there is no code
		in the app that reads either one. We say this plainly rather than claim support that doesn't exist.
		In practice this has no effect on you today: nothing beyond strictly necessary and functional storage
		is ever set, with or without a GPC/DNT signal present, so there is currently nothing for either signal
		to opt you out of. If an optional storage category is ever activated (Section 4), honoring GPC as
		an automatic opt-out will be built as part of that same change, not added afterward.
	</p>

	<h2 id="third-party">8. Third-party and white-label storage</h2>
	<p>
		The supplier portal — the separate sign-in your vendors use to submit and track their own
		invoices — runs on the same static frontend and uses the identical storage model described
		here: a JWT in localStorage (<code>portal_auth_token</code>), no cookies, no analytics.
		Everything in this notice applies to it too.
	</p>
	<p>
		If your organization has enabled white-label branding, or serves {OPERATOR.serviceName} from
		its own custom domain, that configuration changes how the app looks and where it's hosted, not
		what it stores — the same keys in the table above, under whichever domain you're viewing it
		from. If your organization has separately connected a third-party service of its own choosing
		outside {OPERATOR.serviceName} — for example, its own marketing site, chat widget, or single
		sign-on provider — that third party's own site or service may set its own cookies or storage
		under its own domain, governed by its own notice. This document describes only what the {OPERATOR.serviceName}
		application itself stores in your browser.
	</p>

	<h2 id="changes">9. Changes to this notice, and contact</h2>
	<p>
		If the storage {OPERATOR.serviceName} sets ever changes — a new key, a new purpose, or the
		optional category in Section 4 going from reserved to active — we will update this notice in
		the same change and move the "Last updated" date above accordingly. This notice is one of the
		published legal documents linked at the bottom of this page; the <a href="/legal/privacy"
			>Privacy Policy</a
		> covers the wider question of what personal data is held and why.
	</p>
	<p>
		Questions about this notice or your storage choices can go to <a href={`mailto:${CONTACT.privacy}`}
			>{CONTACT.privacy}</a
		>.
	</p>
</LegalPage>

<style>
	/* The withdrawal control. Styled as a real button rather than a text link,
	   because a control that withdraws consent should not be less prominent
	   than the ones that give it. */
	.consent-reset {
		font: inherit;
		font-size: 0.9375rem;
		padding: 8px 16px;
		border: 1px solid var(--accent-strong);
		border-radius: 6px;
		background: var(--accent-strong);
		color: #fff;
		font-weight: 600;
		cursor: pointer;
	}

	.consent-reset:hover:not(:disabled) {
		filter: brightness(1.08);
	}

	.consent-reset:disabled {
		opacity: 0.6;
		cursor: default;
	}

	.consent-reset-note {
		margin-left: 12px;
		font-size: 0.9375rem;
		color: var(--success-on-tint);
	}
</style>
