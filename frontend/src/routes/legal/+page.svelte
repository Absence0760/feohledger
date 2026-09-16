<script lang="ts">
	import { CONTACT, LAST_UPDATED, OPERATOR } from '$lib/legal/operator';
	import { LEGAL_PAGES } from '$lib/legal/pages';

	/**
	 * Index of the published legal document set.
	 *
	 * Deliberately not a `LegalPage` — it is a directory, not a document, so it
	 * carries no last-updated stamp of its own and no pending-facts notice
	 * (each document renders its own). The list comes from `lib/legal/pages.ts`
	 * so adding a document never means remembering to link it here.
	 */
</script>

<svelte:head>
	<title>Legal · {OPERATOR.serviceName}</title>
</svelte:head>

<div class="legal-index">
	<header>
		<h1>Legal</h1>
		<p class="lede">
			The agreements and notices that govern {OPERATOR.serviceName}.
			{LAST_UPDATED} is the date this set was most recently reviewed and republished
			together — it does not mean every document's text changed that day, only that
			each is current as of it.
		</p>
	</header>

	<ul class="docs">
		{#each LEGAL_PAGES as page (page.path)}
			<li>
				<a href={page.path}>
					<span class="doc-title">{page.title}</span>
					<span class="doc-blurb">{page.blurb}</span>
				</a>
			</li>
		{/each}
	</ul>

	<section class="cross-ref" aria-labelledby="cross-ref-heading">
		<h2 id="cross-ref-heading">Looking for refunds or cancellation terms?</h2>
		<p>
			There is no separate refunds or cancellation policy — this index lists every
			document we publish, and that isn't one of them. Those terms live inside the
			<a href="/legal/terms#fees">Terms of Service's fees section</a> (clauses 5.7–5.8,
			covering payment, taxes and non-payment) and its
			<a href="/legal/terms#termination">termination section</a> (clause 10, covering
			cancellation, data export and deletion).
		</p>
	</section>

	<section class="contact" aria-labelledby="contact-heading">
		<h2 id="contact-heading">Getting in touch</h2>
		<p>
			For privacy questions, data-subject requests or erasure requests, write to
			<a href="mailto:{CONTACT.privacy}">{CONTACT.privacy}</a>. For contract and
			DPA matters, <a href="mailto:{CONTACT.legal}">{CONTACT.legal}</a>. To report
			a security vulnerability,
			<a href="mailto:{CONTACT.security}">{CONTACT.security}</a>.
		</p>
		<p class="note">
			If your organisation uses {OPERATOR.serviceName} and your question is about
			an invoice, a payment or your own supplier record, that data belongs to
			that organisation and they hold it — contact them first. The
			<a href="/legal/privacy#roles">Privacy Policy</a> explains the split.
		</p>
	</section>
</div>

<style>
	.legal-index {
		max-width: 46rem;
		margin: 0 auto;
		padding: 48px 16px 64px;
		color: var(--text);
	}

	h1 {
		margin: 0 0 8px;
		font-size: 2.25rem;
		line-height: 1.2;
	}

	.lede {
		margin: 0 0 40px;
		color: var(--text-muted);
		font-size: 1.0625rem;
		line-height: 1.6;
	}

	.docs {
		margin: 0;
		padding: 0;
		list-style: none;
		display: grid;
		gap: 12px;
	}

	.docs a {
		display: block;
		padding: 16px 20px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
		color: var(--text);
		text-decoration: none;
	}

	.docs a:hover,
	.docs a:focus-visible {
		border-color: var(--accent);
	}

	.doc-title {
		display: block;
		margin-bottom: 4px;
		font-size: 1.0625rem;
		font-weight: 600;
		color: var(--accent-on-tint);
	}

	.doc-blurb {
		display: block;
		color: var(--text-muted);
		font-size: 0.9375rem;
		line-height: 1.5;
	}

	.cross-ref {
		margin-top: 48px;
		padding-top: 24px;
		border-top: 1px solid var(--border);
	}

	.cross-ref h2 {
		margin: 0 0 12px;
		font-size: 1.125rem;
	}

	.cross-ref p {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.9375rem;
		line-height: 1.7;
	}

	.cross-ref a {
		color: var(--accent-on-tint);
	}

	.contact {
		margin-top: 40px;
		padding-top: 24px;
		border-top: 1px solid var(--border);
	}

	.contact h2 {
		margin: 0 0 12px;
		font-size: 1.125rem;
	}

	.contact p {
		margin: 0 0 12px;
		line-height: 1.7;
	}

	.contact a {
		color: var(--accent-on-tint);
	}

	.note {
		color: var(--text-muted);
		font-size: 0.9375rem;
	}
</style>
