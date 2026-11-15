<script lang="ts">
	/**
	 * How the ERP says a payment was made, rail by rail, and what the record
	 * does with that split. Shared by the exception queue's "Record the ERP's
	 * payment" and the payment queue's "Record as paid" (decisions §413, §426),
	 * both fed from the same server data: the row's kept `payment_parts` and
	 * the server's verdict on them (`stands`). Nothing is summed here; whether
	 * the parts add up is the server's call (`frontend/docs/money-formatting.md`).
	 *
	 * The caller renders it only when `spansSeveralRails(parts)`: one rail is
	 * just the method, which the form already shows.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import Money from '#lib/components/ui/Money.svelte';
	import { paymentMethodLabelKey, type PaymentMethod } from '#lib/types/payment.ts';
	import { splitNoteKey, type RailPartView } from '#lib/types/erpReport.ts';

	type Props = {
		parts: readonly RailPartView[];
		/** The ISO 4217 code the ERP stated the payment in. */
		currency: string | null;
		/** The ERP's name, or the reader's words for "your ERP". */
		erp: string;
		/** The server's verdict: true stands, false dropped, null not judged. */
		stands: boolean | null | undefined;
		/** Prefix for the element ids this renders, unique per page. */
		idPrefix: string;
		/** `data-testid` prefix. */
		testid: string;
	};

	let { parts, currency, erp, stands, idPrefix, testid }: Props = $props();

	function railLabel(rail: PaymentMethod | null): string {
		const key = rail ? paymentMethodLabelKey(rail) : null;
		return key ? m(key) : m('exceptions.erpPayment.railNotStated');
	}
</script>

<div class="erp-split" data-testid="{testid}-split">
	<h4 id="{idPrefix}-split-title">{m('exceptions.erpPayment.splitTitle', { erp })}</h4>
	<ul aria-labelledby="{idPrefix}-split-title">
		{#each parts as part, i (i)}
			<li data-testid="{testid}-split-part">
				<span>{railLabel(part.rail)}</span>
				<Money amount={part.amount} {currency} mono />
			</li>
		{/each}
	</ul>
	<p class="modal-hint" data-testid="{testid}-split-note">{m(splitNoteKey(stands))}</p>
</div>

<style>
	.erp-split {
		margin: 0 0 12px;
	}

	h4 {
		margin: 0 0 2px;
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
	}

	ul {
		list-style: none;
		margin: 0 0 6px;
		padding: 0;
	}

	li {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		padding: 4px 0;
		border-bottom: 1px solid var(--border);
		font-size: 0.85rem;
		color: var(--text);
	}
</style>
