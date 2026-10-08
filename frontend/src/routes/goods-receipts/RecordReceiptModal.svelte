<script lang="ts">
	/**
	 * Record a delivery against a purchase order — the 3-way matching leg.
	 *
	 * Pick a PO and the form lays out its lines with ordered / already received /
	 * outstanding, pre-filling "this delivery" with what is outstanding, because
	 * the common case is "it all came". A line with nothing outstanding, or no
	 * ordered quantity, starts BLANK: a pre-filled 0 would be posted as "expected
	 * but short", a claim the user never made. A PO the ERP synced with no lines
	 * takes free-text lines instead.
	 *
	 * Over-receipt is allowed (it happened; the matcher flags it) and said beside
	 * the line. Quantities are typed in the reader's own decimal notation and
	 * sent dot-normalised as strings (`utils/quantity`).
	 *
	 * One `Idempotency-Key` per opened form, so a double-click or a retry after a
	 * lost response replays the same receipt instead of booking it twice.
	 */
	import Modal from '#lib/components/ui/Modal.svelte';
	import SearchBox from '#lib/components/ui/SearchBox.svelte';
	import QuantityInput from '#lib/components/ui/QuantityInput.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { formatQuantityInput, isPositiveQuantity, parseQuantity } from '#lib/utils/quantity.ts';
	import {
		createGoodsReceipt,
		getReceivablePO,
		searchReceivablePOs,
		type GoodsReceiptCreateBody,
		type GoodsReceiptDetail,
		type ReceivablePO,
		type ReceivablePODetail
	} from '#lib/api/goodsReceipts.ts';
	import { isCancelledPO, remainingQuantity } from '#lib/types/goodsReceipt.ts';

	let {
		onclose,
		onrecorded
	}: {
		onclose: () => void;
		onrecorded: (receipt: GoodsReceiptDetail) => void;
	} = $props();

	/** The user's own calendar date. The server allows up to a day past its
	 *  UTC date for exactly this reason: east of UTC, "today" is already
	 *  tomorrow there. */
	function localToday(): string {
		const d = new Date();
		const mm = String(d.getMonth() + 1).padStart(2, '0');
		const dd = String(d.getDate()).padStart(2, '0');
		return `${d.getFullYear()}-${mm}-${dd}`;
	}

	const idempotencyKey = crypto.randomUUID();
	const uid = $props.id();

	let search = $state('');
	let options = $state<ReceivablePO[]>([]);
	let searching = $state(false);
	/** The search's own failure — kept apart from the submit refusal, and
	 *  cleared by the next search that succeeds. */
	let searchError = $state<string | null>(null);
	let pickedId = $state<string | null>(null);
	let po = $state<ReceivablePODetail | null>(null);
	let poLoading = $state(false);
	let poError = $state<string | null>(null);
	/** Per PO line: the raw text typed into "this delivery". */
	let quantities = $state<Record<string, string>>({});
	/** Free-text lines, for a PO that carries none of its own. `key` is stable
	 *  so removing a middle row never moves values between rows. */
	let freeLines = $state<{ key: number; description: string; quantity: string }[]>([
		{ key: 0, description: '', quantity: '' }
	]);
	let nextKey = 1;
	let receivedDate = $state(localToday());
	let grNumber = $state('');
	let saving = $state(false);
	/** The backend's refusal, inline and persistent — not a toast that fades
	 *  off a form the user is still looking at. */
	let submitError = $state<string | null>(null);

	const searchSequence = createRequestSequencer();
	const poSequence = createRequestSequencer();

	async function runSearch(text: string) {
		const token = searchSequence.start();
		searching = true;
		try {
			const page = await searchReceivablePOs(text);
			if (!searchSequence.canCommit(token)) return;
			options = page.items;
			searchError = null;
			// The pick must be one of the options on screen. Keeping a PO the new
			// search no longer lists left the select blank while the form still
			// held — and would have received against — the old PO.
			if (!page.items.some((o) => o.id === pickedId)) pickedId = page.items[0]?.id ?? null;
		} catch (err) {
			if (!searchSequence.isCurrentRequest(token)) return;
			searchError = err instanceof Error ? err.message : m('goodsReceipts.record.loadPosFailed');
		} finally {
			if (searchSequence.isCurrentRequest(token)) searching = false;
		}
	}

	// Load once on open, then debounce typing (ui-patterns § Search). The
	// teardown cancels a pending search when the term changes or the dialog
	// closes.
	let searchedOnce = false;
	$effect(() => {
		const text = search;
		if (!searchedOnce) {
			searchedOnce = true;
			void runSearch(text);
			return;
		}
		const timer = setTimeout(() => void runSearch(text), 250);
		return () => clearTimeout(timer);
	});

	$effect(() => {
		const id = pickedId;
		if (!id) {
			po = null;
			return;
		}
		void loadPo(id);
	});

	async function loadPo(id: string) {
		const token = poSequence.start();
		poLoading = true;
		poError = null;
		try {
			const detail = await getReceivablePO(id);
			if (!poSequence.canCommit(token)) return;
			po = detail;
			quantities = Object.fromEntries(
				detail.line_items.map((li) => {
					const outstanding = remainingQuantity(li);
					return [li.id, outstanding > 0 ? formatQuantityInput(outstanding) : ''];
				})
			);
			freeLines = [{ key: nextKey++, description: '', quantity: '' }];
		} catch (err) {
			if (!poSequence.isCurrentRequest(token)) return;
			po = null;
			poError = err instanceof Error ? err.message : m('goodsReceipts.record.loadPoFailed');
		} finally {
			if (poSequence.isCurrentRequest(token)) poLoading = false;
		}
	}

	const hasPoLines = $derived((po?.line_items.length ?? 0) > 0);
	const poCancelled = $derived(po !== null && isCancelledPO(po.status));

	const linesValid = $derived.by(() => {
		if (!po) return false;
		if (hasPoLines) {
			const parsed = po.line_items.map((li) => parseQuantity(quantities[li.id] ?? ''));
			return parsed.every((p) => p.kind !== 'invalid') && parsed.some(isPositiveQuantity);
		}
		const filled = freeLines.filter((l) => l.description.trim() || l.quantity.trim());
		const parsed = filled.map((l) => parseQuantity(l.quantity));
		return (
			filled.length > 0 &&
			filled.every((l, i) => l.description.trim() !== '' && parsed[i].kind === 'valid') &&
			parsed.some(isPositiveQuantity)
		);
	});

	const canSubmit = $derived(
		po !== null && !poCancelled && !poLoading && receivedDate !== '' && linesValid
	);

	function overReceived(lineId: string): boolean {
		const line = po?.line_items.find((li) => li.id === lineId);
		const parsed = parseQuantity(quantities[lineId] ?? '');
		if (!line || line.quantity === null || parsed.kind !== 'valid') return false;
		return Number(parsed.value) > remainingQuantity(line);
	}

	function normalized(raw: string): string {
		const parsed = parseQuantity(raw);
		return parsed.kind === 'valid' ? parsed.value : raw.trim();
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (!canSubmit || saving || !po) return;
		saving = true;
		submitError = null;
		const body: GoodsReceiptCreateBody = {
			po_id: po.id,
			received_date: receivedDate,
			lines: hasPoLines
				? po.line_items
						.filter((li) => (quantities[li.id] ?? '').trim() !== '')
						.map((li) => ({
							po_line_item_id: li.id,
							quantity_received: normalized(quantities[li.id])
						}))
				: freeLines
						.filter((l) => l.description.trim() || l.quantity.trim())
						.map((l) => ({
							description: l.description.trim(),
							quantity_received: normalized(l.quantity)
						}))
		};
		if (grNumber.trim()) body.gr_number = grNumber.trim();
		try {
			onrecorded(await createGoodsReceipt(body, idempotencyKey));
		} catch (err) {
			submitError = err instanceof Error ? err.message : m('goodsReceipts.toast.recordFailed');
		} finally {
			saving = false;
		}
	}
</script>

<Modal
	ariaLabel={m('goodsReceipts.record.aria')}
	title={m('goodsReceipts.record.title')}
	width="lg"
	{onclose}
>
	<form onsubmit={submit} data-testid="record-receipt-form">
		<p class="intro muted">
			{m('goodsReceipts.record.intro')}
			<HelpTip term="three-way-match" />
		</p>

		<div class="field-row">
			<div class="field">
				<span id="{uid}-search-label">{m('goodsReceipts.record.searchPo')}</span>
				<SearchBox
					bind:value={search}
					placeholder={m('goodsReceipts.record.searchPlaceholder')}
					ariaLabel={m('goodsReceipts.record.searchPo')}
					testid="receipt-po-search"
				/>
			</div>
			<label class="field">
				<span>{m('goodsReceipts.record.po')}</span>
				<select
					value={pickedId ?? ''}
					onchange={(e) => (pickedId = e.currentTarget.value || null)}
					required
					data-testid="receipt-po"
				>
					{#if options.length === 0}
						<option value="" disabled>
							{searching ? m('common.loading') : m('goodsReceipts.record.noPos')}
						</option>
					{/if}
					{#each options as option (option.id)}
						<option value={option.id}>
							{option.vendor_name ? `${option.po_number} — ${option.vendor_name}` : option.po_number}
						</option>
					{/each}
				</select>
			</label>
		</div>
		{#if searchError}
			<p class="submit-error" role="alert" data-testid="receipt-search-error">{searchError}</p>
		{:else if !searching && options.length === 0}
			<p class="hint" role="status" data-testid="receipt-no-pos">{m('goodsReceipts.record.noPos')}</p>
		{/if}

		{#if poLoading}
			<p class="muted">{m('common.loading')}</p>
		{:else if poError}
			<p class="submit-error" role="alert">{poError}</p>
		{:else if po}
			{#if poCancelled}
				<p class="notice" role="status" data-testid="receipt-po-cancelled">
					{m('goodsReceipts.record.poCancelled', { number: po.po_number })}
				</p>
			{:else if hasPoLines}
				<!-- A real <table> inside a labelled, focusable scroller (WCAG
				     1.3.1 + 2.1.1): `display:block` on the table itself strips its
				     semantics, so cells lose their column headers. -->
				<div
					class="lines-scroll"
					role="region"
					aria-label={m('goodsReceipts.record.linesRegion')}
					tabindex="0"
				>
					<table class="lines" data-testid="receipt-lines">
						<thead>
							<tr>
								<th scope="col">{m('goodsReceipts.modal.description')}</th>
								<th scope="col" class="right">{m('goodsReceipts.record.ordered')}</th>
								<th scope="col" class="right">{m('goodsReceipts.record.alreadyReceived')}</th>
								<th scope="col" class="right">{m('goodsReceipts.record.outstanding')}</th>
								<th scope="col" class="right">{m('goodsReceipts.record.thisDelivery')}</th>
							</tr>
						</thead>
						<tbody>
							{#each po.line_items as li (li.id)}
								<tr>
									<td>{li.description ?? '—'}</td>
									<td class="right mono">{li.quantity ?? '—'}</td>
									<td class="right mono">{li.quantity_received}</td>
									<td class="right mono">{li.quantity === null ? '—' : remainingQuantity(li)}</td>
									<td class="right">
										<QuantityInput
											bind:value={quantities[li.id]}
											size="sm"
											ariaLabel={m('goodsReceipts.record.quantityFor', {
												line: li.description ?? '—'
											})}
											describedBy={overReceived(li.id) ? `${uid}-over-${li.id}` : undefined}
											testid="receipt-line-quantity"
										/>
										{#if overReceived(li.id)}
											<small class="over" id="{uid}-over-{li.id}" data-testid="receipt-over">
												{m('goodsReceipts.record.overReceipt')}
											</small>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
				<small class="hint">{m('goodsReceipts.record.linesHint')}</small>
			{:else}
				<p class="hint">{m('goodsReceipts.record.noPoLines')}</p>
				{#each freeLines as line, i (line.key)}
					<div class="field-row free-line">
						<label class="field grow">
							<span>{m('goodsReceipts.record.lineDescription', { n: i + 1 })}</span>
							<input
								type="text"
								bind:value={line.description}
								maxlength="2000"
								data-testid="receipt-free-description"
							/>
						</label>
						<label class="field">
							<span>{m('goodsReceipts.record.lineQuantity', { n: i + 1 })}</span>
							<QuantityInput bind:value={line.quantity} testid="receipt-free-quantity" />
						</label>
						{#if freeLines.length > 1}
							<button
								type="button"
								class="btn-inline remove"
								onclick={() => (freeLines = freeLines.filter((l) => l.key !== line.key))}
								aria-label={m('goodsReceipts.record.removeLine', { n: i + 1 })}
							>
								&times;
							</button>
						{/if}
					</div>
				{/each}
				<button
					type="button"
					class="btn-inline"
					onclick={() =>
						(freeLines = [...freeLines, { key: nextKey++, description: '', quantity: '' }])}
					data-testid="receipt-add-line"
				>
					{m('goodsReceipts.record.addLine')}
				</button>
			{/if}

			<div class="field-row dates">
				<label class="field">
					<span>{m('goodsReceipts.record.receivedDate')}</span>
					<input type="date" bind:value={receivedDate} required data-testid="receipt-date" />
				</label>
				<label class="field">
					<span>{m('goodsReceipts.record.number')}</span>
					<input
						type="text"
						bind:value={grNumber}
						maxlength="100"
						placeholder={m('goodsReceipts.record.numberPlaceholder', { po: po.po_number })}
						aria-describedby="{uid}-number-hint"
						data-testid="receipt-number"
					/>
					<small class="hint" id="{uid}-number-hint">{m('goodsReceipts.record.numberHint')}</small>
				</label>
			</div>
		{/if}

		{#if submitError}
			<p class="submit-error" role="alert" data-testid="receipt-error">{submitError}</p>
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
			<button
				type="submit"
				class="btn-primary"
				disabled={!canSubmit || saving}
				data-testid="receipt-submit"
			>
				{saving ? m('goodsReceipts.record.saving') : m('goodsReceipts.record.submit')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.intro {
		margin: 0 0 14px;
		font-size: 0.82rem;
		line-height: 1.5;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-bottom: 14px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}
	.field-row {
		display: flex;
		gap: 12px;
		flex-wrap: wrap;
		align-items: flex-end;
	}
	.field-row .field {
		flex: 1 1 160px;
	}
	.field-row .field.grow {
		flex: 3 1 220px;
	}
	.hint {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
		line-height: 1.4;
		margin: 0 0 14px;
	}
	.lines-scroll {
		overflow-x: auto;
		margin-bottom: 6px;
	}
	.lines {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.85rem;
	}
	.lines th {
		text-align: left;
		padding: 6px 8px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
	}
	.lines td {
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		vertical-align: top;
	}
	.right {
		text-align: right;
	}
	.lines th.right {
		text-align: right;
	}
	.mono {
		font-family: 'SF Mono', 'Cascadia Code', monospace;
		font-size: 0.82rem;
	}
	.over {
		display: block;
		margin-top: 4px;
		font-size: 0.72rem;
		color: var(--warning-on-tint);
		text-align: left;
	}
	.notice {
		margin: 0 0 14px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		font-size: 0.85rem;
	}
	.btn-inline {
		background: none;
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 4px 10px;
		font-size: 0.78rem;
		font-family: inherit;
		color: var(--text);
		cursor: pointer;
		min-height: 24px;
		min-width: 24px;
		margin-bottom: 14px;
	}
	.btn-inline:hover {
		border-color: var(--accent);
		color: var(--accent);
	}
	.dates {
		margin-top: 8px;
	}
	.submit-error {
		margin: 0 0 14px;
		padding: 10px 12px;
		border: 1px solid var(--danger-strong);
		border-radius: 6px;
		color: var(--danger-on-tint);
		font-size: 0.85rem;
		line-height: 1.45;
	}
</style>
