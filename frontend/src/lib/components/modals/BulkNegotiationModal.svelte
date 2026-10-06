<script lang="ts">
	/**
	 * Propose a vendor-wide early-payment discount —
	 * `POST /api/discounts/bulk-negotiate`.
	 *
	 * The endpoint shipped fully built with no caller at all. Wiring it follows
	 * the shape `docs/decisions.md` §42 set for the corridor-quote optimizer:
	 * reach the largest slice that decides nothing nobody has decided. Here
	 * that is the whole endpoint — it creates ONE vendor-scoped offer at status
	 * `offered` and moves no money; accepting it is still a separate decision
	 * behind its own (CFO-inclusive) gate, and the payment run still funds it.
	 *
	 * Two things about this form are load-bearing:
	 *
	 * 1. **No base amount is shown before the server computes one.** The offer's
	 *    `base_amount` is the summed open balance of the vendor's invoices in
	 *    the caller's entity, and only the server can total it. Re-deriving it
	 *    here from a second query would give the page a figure that can quietly
	 *    disagree with the one booked — the same reason an invoice header is
	 *    never recomputed from its lines. So the form states the RULE in words,
	 *    and the created offer is the first sighting of the figure
	 *    (`docs/decisions.md` §34: never render a number nobody computed).
	 *
	 * 2. **Submitting is a two-click arm-then-commit.** The proposal covers
	 *    every open invoice the vendor has, at a percentage, against a base the
	 *    proposer cannot see. That is exactly the shape that deserves the
	 *    repo's existing armed-confirm idiom (`RowAction armed`,
	 *    `BulkDeleteButton`) rather than a single click.
	 */
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Modal from '#lib/components/ui/Modal.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { scaleMoney } from '#lib/utils/money.ts';
	import { bulkNegotiateDiscount } from '#lib/api/discounts.ts';
	import VendorPicker from '#lib/components/ui/VendorPicker.svelte';
	import type { VendorOption } from '#lib/api/vendors.ts';
	import type { DiscountOffer } from '#lib/types/discounts.ts';
	import {
		isTierStarted,
		normalizeTierDays,
		normalizeTierPercent,
		type BulkTierInput
	} from '#lib/types/discounts.ts';

	let {
		onclose,
		oncreated
	}: {
		onclose: () => void;
		oncreated: (offer: DiscountOffer) => void;
	} = $props();

	// `_WRITE_ROLES` on `backend/app/api/discounts.py` — admin / ap_manager.
	// Deliberately NARROWER than the page's `canDecide`: a CFO may accept or
	// decline an offer a supplier put on the table, but proposing one to a
	// supplier is an AP operation. The trigger button is gated on the same
	// predicate, so this is belt-and-braces for a directly-mounted modal.
	const canPropose = $derived(auth.isManager);

	let vendorId = $state('');
	let tiers = $state<BulkTierInput[]>([{ days: '', percent: '' }]);
	let validUntil = $state('');
	let notes = $state('');

	let saving = $state(false);
	let armed = $state(false);
	// Persistent, not a toast: the 409 ("this vendor has no open invoices to
	// negotiate against") is the whole answer to why the form did nothing, and
	// it has to survive long enough to be read and acted on.
	let formError = $state<string | null>(null);
	let created = $state<DiscountOffer | null>(null);

	/** The option the picker committed, kept for the confirm + success copy —
	 *  which names the supplier the offer is about. The picker is the only thing
	 *  that has it now: the page no longer loads a vendor list to look it up in,
	 *  and a lookup over a truncated one is exactly what was wrong before. */
	let selectedVendor = $state<VendorOption | null>(null);

	function addTier() {
		tiers = [...tiers, { days: '', percent: '' }];
		armed = false;
	}

	function removeTier(idx: number) {
		tiers = tiers.filter((_, i) => i !== idx);
		if (tiers.length === 0) tiers = [{ days: '', percent: '' }];
		armed = false;
	}

	/** Rows the user actually filled in, normalized for the wire. A row left
	 *  entirely blank is not an error — it is an unused slot. */
	const startedTiers = $derived(tiers.filter(isTierStarted));
	const payloadTiers = $derived(
		startedTiers
			.map((t) => ({ days: normalizeTierDays(t.days), percent: normalizeTierPercent(t.percent) }))
			.filter((t): t is { days: number; percent: string } => t.days !== null && t.percent !== null)
	);
	/** A started row we could NOT read. Never silently dropped — sending fewer
	 *  tiers than were typed would propose a different offer than the one on
	 *  screen. */
	const hasUnreadableTier = $derived(payloadTiers.length !== startedTiers.length);
	const duplicateDays = $derived(
		new Set(payloadTiers.map((t) => t.days)).size !== payloadTiers.length
	);

	const canSubmit = $derived(
		canPropose &&
			!!vendorId &&
			payloadTiers.length > 0 &&
			!hasUnreadableTier &&
			!duplicateDays &&
			!saving
	);

	// Any edit disarms: the confirm has to belong to the values on screen when
	// it was armed, not to whatever they became afterwards.
	function onEdit() {
		armed = false;
		formError = null;
	}

	async function submit() {
		if (!canSubmit) return;
		if (!armed) {
			armed = true;
			return;
		}
		saving = true;
		formError = null;
		try {
			const offer = await bulkNegotiateDiscount({
				vendorId,
				tiers: payloadTiers,
				validUntil: validUntil || null,
				notes: notes.trim() || null
			});
			created = offer;
			armed = false;
			oncreated(offer);
		} catch (err) {
			// 409 (no open invoices) / 404 (vendor out of entity scope) / 422 all
			// arrive carrying the backend's own explanation.
			formError =
				err instanceof Error && err.message ? err.message : m('discounts.bulk.createFailed');
		} finally {
			saving = false;
		}
	}

	/** Exact saving for one tier of the CREATED offer — one HALF_UP rounding of
	 *  base × percent ÷ 100, the same `scaleMoney` call the offers table uses,
	 *  so the two cannot disagree. */
	function tierSaving(offer: DiscountOffer, percent: number) {
		return scaleMoney(offer.base_amount, percent, { divideBy: 100 });
	}
</script>

<Modal
	open
	ariaLabel={m('discounts.bulk.aria')}
	title={m('discounts.bulk.title')}
	width="md"
	{onclose}
>
	{#if created}
		<!-- Result. The base amount is the point of this panel: it is the first
		     and only place the proposer learns what the server actually summed. -->
		<div class="bulk-result" data-testid="bulk-negotiate-result">
			<p class="modal-hint">
				{m('discounts.bulk.createdFor', {
					vendor: created.vendor_name ?? selectedVendor?.name ?? created.id.slice(0, 8)
				})}
			</p>
			<div class="bulk-base">
				<span class="bulk-base-label">{m('discounts.bulk.baseLabel')}</span>
				<span class="bulk-base-value" data-testid="bulk-negotiate-base">
					<Money amount={created.base_amount} currency={created.currency} />
				</span>
			</div>
			<p class="bulk-note">{m('discounts.bulk.baseExplain')}</p>

			{#if created.tiers.length > 0}
				<ul class="bulk-tier-list">
					{#each [...created.tiers].sort((a, b) => a.days - b.days) as tier (tier.days)}
						<li>
							<span>{m('discounts.modal.tierOption', { percent: tier.percent, days: tier.days })}</span>
							<span class="bulk-tier-save">
								{m('discounts.modal.save')}
								<Money amount={tierSaving(created, tier.percent)} currency={created.currency} />
							</span>
						</li>
					{/each}
				</ul>
			{/if}
		</div>
		<div class="modal-footer">
			<button type="button" class="btn-primary" onclick={onclose}>
				{m('discounts.bulk.done')}
			</button>
		</div>
	{:else}
		<form
			onsubmit={(e) => {
				e.preventDefault();
				submit();
			}}
		>
			<p class="modal-hint">{m('discounts.bulk.intro')} <HelpTip term="dynamic-discounting" /></p>

			<div class="form-grid">
				<VendorPicker
					bind:value={vendorId}
					label={m('discounts.bulk.vendor')}
					placeholder={m('discounts.bulk.selectVendor')}
					required
					disabled={!canPropose}
					testid="bulk-negotiate-vendor"
					onselect={(option) => {
						selectedVendor = option;
						onEdit();
					}}
				/>
				<label>
					<span>{m('discounts.bulk.validUntil')}</span>
					<input
						type="date"
						bind:value={validUntil}
						oninput={onEdit}
						disabled={!canPropose}
						data-testid="bulk-negotiate-valid-until"
					/>
				</label>
			</div>

			<!-- No end date means the offer has no horizon, so the optimizer can
			     never rank it (it comes back on `unrankable` with a null APR).
			     Said out loud rather than defaulted for the user — picking a date
			     on their behalf would be inventing a term the supplier never
			     agreed to. -->
			{#if !validUntil}
				<p class="bulk-note" data-testid="bulk-negotiate-no-horizon">
					{m('discounts.bulk.noValidUntil')}
				</p>
			{/if}

			<div class="bulk-tiers">
				<div class="bulk-tiers-title">{m('discounts.bulk.tiersTitle')}</div>
				<p class="bulk-note">{m('discounts.bulk.tiersHint')}</p>

				<div class="tier-head">
					<span>{m('discounts.bulk.colDays')}</span>
					<span>{m('discounts.bulk.colPercent')}</span>
					<span></span>
				</div>
				{#each tiers as tier, idx (idx)}
					<div class="tier-row">
						<!-- `value` + `oninput`, never `bind:value`: Svelte's two-way
						     binding on `type="number"` hands back a NUMBER, and the
						     percent has to reach the wire as the exact decimal text
						     it was typed as. Same read-as-string pattern the
						     statement-reconciliation line editor uses. -->
						<input
							type="number"
							min="0"
							max="365"
							step="1"
							value={tier.days}
							oninput={(e) => {
								tier.days = e.currentTarget.value;
								onEdit();
							}}
							placeholder="10"
							aria-label={m('discounts.bulk.daysAria', { n: idx + 1 })}
							disabled={!canPropose}
						/>
						<input
							type="number"
							min="0.01"
							max="99.99"
							step="0.01"
							value={tier.percent}
							oninput={(e) => {
								tier.percent = e.currentTarget.value;
								onEdit();
							}}
							placeholder="2.00"
							aria-label={m('discounts.bulk.percentAria', { n: idx + 1 })}
							disabled={!canPropose}
						/>
						<button
							type="button"
							class="tier-remove"
							onclick={() => removeTier(idx)}
							aria-label={m('discounts.bulk.removeTierAria', { n: idx + 1 })}
							disabled={!canPropose}
						>
							×
						</button>
					</div>
				{/each}
				{#if canPropose}
					<button type="button" class="tier-add" onclick={addTier}>
						{m('discounts.bulk.addTier')}
					</button>
				{/if}
			</div>

			{#if hasUnreadableTier}
				<p class="bulk-warn" role="alert" data-testid="bulk-negotiate-tier-invalid">
					{m('discounts.bulk.tierInvalid')}
				</p>
			{:else if duplicateDays}
				<p class="bulk-warn" role="alert" data-testid="bulk-negotiate-tier-duplicate">
					{m('discounts.bulk.tierDuplicate')}
				</p>
			{/if}

			<label class="bulk-notes">
				<span>{m('discounts.bulk.notes')}</span>
				<input
					type="text"
					maxlength="500"
					bind:value={notes}
					oninput={onEdit}
					disabled={!canPropose}
				/>
			</label>

			{#if formError}
				<div class="bulk-error" role="alert" data-testid="bulk-negotiate-error">
					<strong>{m('discounts.bulk.errorTitle')}</strong>
					<span>{formError}</span>
				</div>
			{/if}

			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={onclose}>
					{m('common.cancel')}
				</button>
				{#if canPropose}
					<button
						type="submit"
						class="btn-primary"
						class:armed
						disabled={!canSubmit}
						data-testid="bulk-negotiate-submit"
					>
						{#if saving}
							{m('discounts.bulk.proposing')}
						{:else if armed}
							{m('discounts.bulk.confirm', {
								vendor: selectedVendor?.name ?? m('discounts.bulk.thisVendor')
							})}
						{:else}
							{m('discounts.bulk.propose')}
						{/if}
					</button>
				{/if}
			</div>
		</form>
	{/if}
</Modal>

<style>
	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}
	.form-grid label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.form-grid input,
	.form-grid select {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}
	.form-grid input:disabled,
	.form-grid select:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.bulk-note {
		font-size: 0.78rem;
		color: var(--text-muted);
		margin: 10px 0 0;
	}

	.bulk-tiers {
		margin-top: 16px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.bulk-tiers-title {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
	}
	.tier-head,
	.tier-row {
		display: grid;
		grid-template-columns: 1fr 1fr 32px;
		gap: 8px;
		align-items: center;
	}
	.tier-head {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.tier-row input {
		padding: 6px 8px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.84rem;
	}
	.tier-remove {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		border-radius: 5px;
		height: 30px;
		cursor: pointer;
		font-size: 1rem;
		line-height: 1;
	}
	.tier-remove:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}
	.tier-remove:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.tier-add {
		align-self: flex-start;
		margin-top: 2px;
		border: 1px dashed var(--border);
		background: transparent;
		color: var(--text-muted);
		border-radius: 5px;
		padding: 5px 12px;
		cursor: pointer;
		font-family: inherit;
		font-size: 0.8rem;
	}
	.tier-add:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.bulk-warn {
		margin: 12px 0 0;
		font-size: 0.8rem;
		color: var(--danger);
	}

	.bulk-notes {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 14px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.bulk-notes input {
		padding: 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	.bulk-error {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin-top: 14px;
		padding: 10px 12px;
		border: 1px solid rgba(224, 64, 64, 0.4);
		border-radius: 6px;
		background: rgba(224, 64, 64, 0.08);
		font-size: 0.82rem;
		color: var(--text);
	}
	.bulk-error strong {
		color: var(--danger);
		font-size: 0.8rem;
	}

	/* Armed confirm — the same red "one more click commits this" cue
	   `RowAction armed` and `BulkDeleteButton` use. */
	.btn-primary.armed {
		background: var(--danger);
		border-color: var(--danger);
	}

	.bulk-result {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.bulk-base {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-top: 6px;
	}
	.bulk-base-label {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.bulk-base-value {
		font-size: 1.3rem;
		font-weight: 600;
		color: var(--text);
	}
	.bulk-tier-list {
		list-style: none;
		margin: 14px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.bulk-tier-list li {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		font-size: 0.84rem;
		color: var(--text);
		padding: 6px 10px;
		border: 1px solid var(--border);
		border-radius: 5px;
	}
	.bulk-tier-save {
		color: var(--text-muted);
		white-space: nowrap;
	}
</style>
