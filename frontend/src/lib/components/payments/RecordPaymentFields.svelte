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
	 *
	 * `methodFixed`, when set, replaces the select with that sentence, read
	 * only: the ERP's breakdown over several rails is the record whatever one
	 * rail a person would pick (decisions §396, §413), so no picker is offered
	 * and `method` is not sent.
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
		/** Shown, read only, in place of the rail picker. */
		methodFixed?: string | null;
	};

	let {
		method = $bindable(),
		reference = $bindable(),
		paidOn = $bindable(),
		maxDate,
		idPrefix,
		testid = 'record',
		methodFixed = null
	}: Props = $props();

	function methodLabel(value: string): string {
		const key = paymentMethodLabelKey(value);
		return key ? m(key) : value;
	}
</script>

{#if methodFixed}
	<!-- Read only: text, not a disabled select, so it stays readable and is
	     announced as a fact rather than an unavailable control. -->
	<div class="fixed-field" role="group" aria-labelledby="{idPrefix}-method-label">
		<span class="fixed-label" id="{idPrefix}-method-label">{m('payments.recordOutside.method')}</span>
		<span class="fixed-value" data-testid="{testid}-method-fixed">{methodFixed}</span>
	</div>
{:else}
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
{/if}
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
	.fixed-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	/* The modal's own label recipe (`app.css` `.modal label > span`). */
	.fixed-label {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.fixed-value {
		font-size: 0.85rem;
		color: var(--text);
	}

	.field-hint {
		margin: -8px 0 0;
		font-size: 0.75rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
</style>
