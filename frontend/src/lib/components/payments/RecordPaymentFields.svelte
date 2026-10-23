<script lang="ts">
	/**
	 * The three facts a payment made outside FeohLedger is recorded with — how
	 * it was paid, its reference, and the paid-on date. Shared by the payment
	 * queue's "Record as paid" dialog and the exception queue's "Record the
	 * ERP's payment" (decisions §319), so the two cannot drift on what the
	 * server's `RecordErpReportedPaymentRequest` accepts.
	 *
	 * `method` may be `''`: the form was pre-filled from an ERP report that
	 * named no recordable rail, and the person must choose one — the select then
	 * opens on a "Choose…" placeholder and is `required`.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import { paymentMethodLabelKey, type PaymentMethod } from '#lib/types/payment.ts';
	import { EXTERNAL_PAYMENT_METHODS } from '#lib/types/paymentExecution.ts';

	type Props = {
		method: PaymentMethod | '';
		reference: string;
		paidOn: string;
		/** The latest paid-on date offered (the reader's own calendar day). */
		maxDate: string;
		/** Prefix for the element ids this renders, unique per page. */
		idPrefix: string;
		/** `data-testid` prefix; the payment queue's specs read `record-*`. */
		testid?: string;
	};

	let {
		method = $bindable(),
		reference = $bindable(),
		paidOn = $bindable(),
		maxDate,
		idPrefix,
		testid = 'record'
	}: Props = $props();

	function methodLabel(value: string): string {
		const key = paymentMethodLabelKey(value);
		return key ? m(key) : value;
	}
</script>

<label>
	<span>{m('payments.recordOutside.method')}</span>
	<select bind:value={method} required data-testid="{testid}-method">
		{#if method === ''}
			<option value="" disabled>{m('payments.recordOutside.methodChoose')}</option>
		{/if}
		{#each EXTERNAL_PAYMENT_METHODS as option (option)}
			<option value={option}>{methodLabel(option)}</option>
		{/each}
	</select>
</label>
<label>
	<span>{m('payments.recordOutside.reference')}</span>
	<input
		type="text"
		bind:value={reference}
		maxlength="255"
		required
		aria-describedby="{idPrefix}-reference-hint"
		data-testid="{testid}-reference"
	/>
</label>
<p class="field-hint" id="{idPrefix}-reference-hint">{m('payments.recordOutside.referenceHint')}</p>
<label>
	<span>{m('payments.recordOutside.paidOn')}</span>
	<input type="date" bind:value={paidOn} max={maxDate} required data-testid="{testid}-paid-on" />
</label>

<style>
	.field-hint {
		margin: -8px 0 0;
		font-size: 0.75rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
</style>
