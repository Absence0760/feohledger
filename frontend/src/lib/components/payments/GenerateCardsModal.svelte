<script lang="ts">
	/**
	 * "Generate card" on the payments Cards tab — mints a virtual card directly
	 * for each invoice the reader adds (`POST /api/cards/generate`), outside a
	 * payment run.
	 *
	 * **This moves money**: a minted card is spendable. The server runs every
	 * gate a run's card leg does and answers per invoice — `items` for the cards
	 * this call minted, `skipped` for every other invoice with a coded reason
	 * (decisions §342). The result view states both, each skip through
	 * `cardGenerateSkipText` (the shared refusal registry), so "0 cards" is
	 * never left unexplained.
	 *
	 * Who sees the action is the page's call (the endpoint's `payment.execute`
	 * plus a tenant that dispatches payments); a whole-call refusal (cards off,
	 * the 409 `invoice_locked`) is stated in place, localized, and the dialog
	 * stays open so nothing the reader picked is lost.
	 */
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import InvoicePicker from '#lib/components/ui/InvoicePicker.svelte';
	import { tick } from 'svelte';
	import { ApiError } from '#lib/api.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { errorMessage } from '#lib/utils/apiError.ts';
	import type { InvoicePickerOption } from '#lib/utils/invoicePicker.ts';
	import { cardGenerateSkipText, generateCards, listCardableInvoices } from '#lib/api/cards.ts';
	import {
		cardGenerateSkipRows,
		type CardGenerateResponse
	} from '#lib/types/cardGenerate.ts';

	type Props = {
		onclose: () => void;
		/** Fired once a call minted at least one card, so the page reloads its list. */
		ongenerated: () => void;
	};

	let { onclose, ongenerated }: Props = $props();

	let pickerValue = $state('');
	let pickerOption = $state<InvoicePickerOption | null>(null);
	let chosen = $state<InvoicePickerOption[]>([]);
	let generating = $state(false);
	let error = $state('');
	let result = $state<CardGenerateResponse | null>(null);
	let doneButton = $state<HTMLButtonElement | null>(null);

	// Closing mid-call would throw away the answer — including the skips that
	// say why an invoice got no card — while the cards are being minted.
	function close() {
		if (!generating) onclose();
	}

	const invoiceNumbers = $derived(new Map(chosen.map((o) => [o.id, o.invoice_number])));
	const skipRows = $derived(result ? cardGenerateSkipRows(result, invoiceNumbers) : []);

	function addChosen() {
		const option = pickerOption;
		if (!option) return;
		if (!chosen.some((o) => o.id === option.id)) chosen = [...chosen, option];
		pickerValue = '';
		pickerOption = null;
	}

	function removeChosen(id: string) {
		chosen = chosen.filter((o) => o.id !== id);
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (chosen.length === 0 || generating) return;
		generating = true;
		error = '';
		try {
			const res = await generateCards(chosen.map((o) => o.id));
			result = res;
			if (res.items.length > 0) ongenerated();
			// The submit button the reader pressed is gone with the form; land
			// focus on the result's own control so the keyboard is not dropped
			// onto the page behind the dialog.
			await tick();
			doneButton?.focus();
		} catch (err) {
			if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
				// A refusal of the whole call: cards off, a record-only tenant,
				// the permission, or `invoice_locked` (which the server answers
				// only when nothing was minted). Nothing was issued.
				error = errorMessage(err, m('payments.cards.generateDialog.failed'));
			} else {
				// A dropped connection or a server error mid-call: the mint
				// commits per invoice, so some cards may exist. Re-read the list
				// and say so; a retry is safe (an issued invoice answers
				// `card_skip_already_carded`).
				error = m('payments.cards.generateDialog.uncertain');
				ongenerated();
			}
		} finally {
			generating = false;
		}
	}
</script>

<Modal
	ariaLabel={m('payments.cards.generateDialog.title')}
	title={m('payments.cards.generateDialog.title')}
	onclose={close}
>
	{#if result}
		<div role="status" data-testid="card-generate-result">
			{#if result.items.length > 0}
				<h3 class="gc-heading">
					{m('payments.cards.generateDialog.minted', { n: result.items.length })}
				</h3>
				<ul class="gc-list" data-testid="card-generate-minted">
					{#each result.items as card (card.id)}
						<li>
							<span class="gc-number">{invoiceNumbers.get(card.invoice_id) ?? card.invoice_id}</span>
							<Badge tone="accent">
								{card.card_provider}{card.last_four ? ` •••• ${card.last_four}` : ''}
							</Badge>
							<Money amount={card.amount_limit} currency={card.currency} mono />
						</li>
					{/each}
				</ul>
			{/if}
			{#if skipRows.length > 0}
				<h3 class="gc-heading">
					{m('payments.cards.generateDialog.skipped', { n: skipRows.length })}
				</h3>
				<ul class="gc-list" data-testid="card-generate-skipped">
					{#each skipRows as row (row.invoiceId)}
						<li data-code={row.skip.code}>
							<span class="gc-number">{row.invoiceNumber ?? row.invoiceId}</span>
							<span class="gc-reason">{cardGenerateSkipText(row.skip, m)}</span>
						</li>
					{/each}
				</ul>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-primary" bind:this={doneButton} onclick={onclose}>
					{m('payments.cards.generateDialog.close')}
				</button>
			</div>
		</div>
	{:else}
		<form onsubmit={submit}>
			<p class="gc-intro">{m('payments.cards.generateDialog.intro')}</p>
			<div class="gc-add">
				<InvoicePicker
					bind:value={pickerValue}
					load={listCardableInvoices}
					label={m('payments.cards.generateDialog.invoice')}
					placeholder={m('payments.cards.generateDialog.placeholder')}
					emptyText={m('payments.cards.generateDialog.empty')}
					hint={m('payments.cards.generateDialog.hint')}
					testid="card-generate-invoice"
					onselect={(option) => (pickerOption = option)}
				/>
				<button
					type="button"
					class="btn-cancel"
					data-testid="card-generate-add"
					disabled={!pickerOption}
					onclick={addChosen}
				>
					{m('payments.cards.generateDialog.add')}
				</button>
			</div>

			{#if chosen.length > 0}
				<ul class="gc-list" data-testid="card-generate-chosen">
					{#each chosen as option (option.id)}
						<li>
							<span class="gc-number">{option.invoice_number}</span>
							<Money amount={option.amount} currency={option.currency} mono />
							<button
								type="button"
								class="gc-remove"
								aria-label={m('payments.cards.generateDialog.removeAria', {
									number: option.invoice_number
								})}
								onclick={() => removeChosen(option.id)}
							>
								{m('payments.cards.generateDialog.remove')}
							</button>
						</li>
					{/each}
				</ul>
			{/if}

			{#if error}
				<p class="gc-error" role="alert" data-testid="card-generate-error">{error}</p>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" disabled={generating} onclick={close}>
					{m('common.cancel')}
				</button>
				<button
					type="submit"
					class="btn-primary"
					data-testid="card-generate-submit"
					disabled={chosen.length === 0 || generating}
				>
					{generating
						? m('payments.cards.generateDialog.generating')
						: m('payments.cards.generateDialog.submit', { n: chosen.length })}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	.gc-intro {
		margin: 0 0 1rem;
		color: var(--text-muted);
		font-size: 0.875rem;
	}

	.gc-add {
		display: flex;
		align-items: flex-end;
		gap: 0.5rem;
		flex-wrap: wrap;
	}

	.gc-add :global(.invoice-picker) {
		flex: 1 1 14rem;
		min-width: 0;
	}

	.gc-heading {
		margin: 0.75rem 0 0.5rem;
		font-size: 0.875rem;
		font-weight: 600;
	}

	.gc-list {
		list-style: none;
		margin: 0.75rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
	}

	.gc-list li {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		padding: 0.375rem 0;
		border-bottom: 1px solid var(--border);
		font-size: 0.875rem;
	}

	.gc-number {
		font-variant-numeric: tabular-nums;
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.gc-reason {
		flex: 1 1 100%;
		color: var(--text-muted);
	}

	.gc-remove {
		margin-left: auto;
		min-height: 24px;
		min-width: 24px;
		padding: 0.125rem 0.5rem;
		background: none;
		border: 1px solid var(--border);
		border-radius: 6px;
		color: var(--text);
		font-size: 0.8125rem;
		cursor: pointer;
	}

	.gc-remove:hover {
		background: var(--bg);
	}

	.gc-error {
		margin: 0.75rem 0 0;
		color: var(--danger);
		font-size: 0.875rem;
	}
</style>
