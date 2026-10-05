<!--
	The per-field reasons an e-invoice dialect refused an invoice, one row each.

	Rendered from BOTH the e-invoice download failure and the PEPPOL send
	failure — the same `[{loc, type, msg}]` 422 body reaches each, so the two
	used to carry identical markup. Extracted so the localization decision below
	is made in exactly one place.

	Each row prefers the LOCALIZED sentence for the rule the validator named,
	and falls back to the server's own English when there is no key for that
	code — the tolerant half of decisions.md §95's replacement: an unknown code
	degrades to the raw sentence (which still leads with the rule id), never to
	a blank row. The rule id and the field path stay on screen either way: the
	id is what a receiving Access Point's own validator will name, and the path
	is what says WHICH line or tax row is at fault.
-->
<script lang="ts">
	import { einvoiceRuleMessageKey, type EInvoiceValidationIssue } from '#lib/api/einvoice.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { issues }: { issues: EInvoiceValidationIssue[] } = $props();
</script>

<ul class="einvoice-issues">
	{#each issues as issue (issue.field + issue.message)}
		{@const key = einvoiceRuleMessageKey(issue)}
		<li>
			{key ? m(key) : issue.message}
			<code class="einvoice-issue-field">
				{issue.field}{#if key}&nbsp;· {issue.code}{/if}
			</code>
		</li>
	{/each}
</ul>
<p class="einvoice-error-hint">{m('invoices.modal.einvoice.invalidHint')}</p>

<style>
	.einvoice-issues {
		margin: 8px 0 0;
		padding-left: 18px;
		font-size: 0.78rem;
		line-height: 1.5;
		color: var(--text);
	}

	.einvoice-issue-field {
		display: block;
		margin-top: 1px;
		font-family: var(--font-mono);
		font-size: 0.7rem;
		color: var(--text-muted);
	}

	.einvoice-error-hint {
		margin: 8px 0 0;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}
</style>
