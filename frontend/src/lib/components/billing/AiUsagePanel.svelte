<script lang="ts">
	// This month's AI-read invoices against the plan allowance (decisions §253):
	// the meter, the overage so far and its month-end projection, the spending
	// cap (editable by an admin on a plan that bills overage), and the pause
	// state. Counts and money come from the server exactly — money is rendered
	// from its decimal string through <Money>, never re-computed here.
	import KpiCard from '#lib/components/ui/KpiCard.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { setBillingSpendCap } from '#lib/api/billing.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatMoney } from '#lib/utils/money.ts';
	import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
	import {
		aiUsagePercent,
		aiUsageTone,
		parseSpendCapInput,
		type BillingAiUsage
	} from '#lib/types/billing.ts';

	type Props = {
		usage: BillingAiUsage | null;
		/** The subscription response is still in flight. */
		pending?: boolean;
		/** Only an admin may change the cap (the endpoint refuses everyone else). */
		canEditCap?: boolean;
		/** Called with the re-priced usage after a cap change. */
		onUsageChange?: (usage: BillingAiUsage) => void;
	};

	let { usage, pending = false, canEditCap = false, onUsageChange }: Props = $props();

	/** A count in the reader's locale (1,234 / 1.234 / 1 234). */
	function formatNumber(n: number): string {
		return new Intl.NumberFormat(getActiveFormatLocale()).format(n);
	}

	const percent = $derived(aiUsagePercent(usage));
	const tone = $derived(aiUsageTone(usage));
	const billsOverage = $derived(usage?.overage_unit_price != null && usage?.included != null);

	let capInput = $state('');
	let capError = $state<string | null>(null);
	let capSaving = $state(false);
	let capSaved = $state(false);

	// Seed the field from the server's value whenever it changes — after a save
	// too, so the field shows what was STORED ("2.5" comes back as "2.50").
	$effect(() => {
		capInput = usage?.spend_cap ?? '';
	});

	async function saveCap(raw: string) {
		capError = null;
		capSaved = false;
		const parsed = parseSpendCapInput(raw);
		if (!parsed.ok) {
			capError = m('billing.ai.cap.invalid');
			return;
		}
		capSaving = true;
		try {
			const res = await setBillingSpendCap(parsed.value);
			onUsageChange?.(res.ai_usage);
			capSaved = true;
		} catch (e) {
			capError = e instanceof Error ? e.message : m('billing.ai.cap.saveFailed');
		} finally {
			capSaving = false;
		}
	}
</script>

<section class="ai-usage" aria-labelledby="ai-usage-heading" data-testid="billing-ai-usage">
	<h3 id="ai-usage-heading">
		{m('billing.ai.heading')}
		{#if usage}<span class="period">({usage.period})</span>{/if}
	</h3>

	{#if usage?.paused}
		<div class="paused" role="status" data-testid="billing-ai-paused">
			<strong>{m('billing.ai.paused.title')}</strong>
			<p>
				{usage.pause_reason === 'spend_cap_reached'
					? m('billing.ai.paused.cap')
					: m('billing.ai.paused.allowance')}
			</p>
		</div>
	{/if}

	{#if usage && usage.included !== null}
		<div class="meter-head">
			<span data-testid="billing-ai-used">
				{m('billing.ai.usedOf', {
					used: formatNumber(usage.used),
					included: formatNumber(usage.included)
				})}
			</span>
			<span class="pct">{percent}%</span>
		</div>
		<div
			class="track tone-{tone}"
			role="meter"
			aria-label={m('billing.ai.meterLabel')}
			aria-valuemin="0"
			aria-valuemax={usage.included}
			aria-valuenow={Math.min(usage.used, usage.included)}
			aria-valuetext={m('billing.ai.usedOf', {
				used: formatNumber(usage.used),
				included: formatNumber(usage.included)
			})}
		>
			<div class="fill" style="width:{percent ?? 0}%"></div>
		</div>
	{:else if usage}
		<p class="note">{m('billing.ai.unmetered', { used: formatNumber(usage.used) })}</p>
	{/if}

	{#if usage && usage.included !== null}
		<p class="note">
			{#if billsOverage}
				{m('billing.ai.overagePrice', {
					price: formatMoney(usage.overage_unit_price, { currency: usage.currency })
				})}
			{:else}
				{m('billing.ai.pausesAtLimit')}
			{/if}
		</p>
	{/if}

	{#if billsOverage || pending}
		<div class="kpi-row">
			<KpiCard
				value={usage && billsOverage
					? formatMoney(usage.overage_amount, { currency: usage.currency })
					: null}
				label={m('billing.ai.overageSoFar')}
				sub={usage && billsOverage
					? m('billing.ai.overageUnits', { count: usage.overage_units })
					: null}
				{pending}
			/>
			<KpiCard
				value={usage && billsOverage
					? formatMoney(usage.projected_overage_amount, { currency: usage.currency })
					: null}
				label={m('billing.ai.projected')}
				{pending}
			/>
			<KpiCard
				value={usage && billsOverage
					? usage.spend_cap === null
						? m('billing.ai.cap.none')
						: formatMoney(usage.spend_cap, { currency: usage.currency })
					: null}
				label={m('billing.ai.cap.label')}
				{pending}
			/>
		</div>
	{/if}

	{#if usage && billsOverage && canEditCap}
		<form
			class="cap-form"
			onsubmit={(e) => {
				e.preventDefault();
				saveCap(capInput);
			}}
		>
			<label for="ai-spend-cap">{m('billing.ai.cap.inputLabel', { currency: usage.currency })}</label>
			<div class="cap-row">
				<input
					id="ai-spend-cap"
					type="text"
					inputmode="decimal"
					autocomplete="off"
					placeholder={m('billing.ai.cap.placeholder')}
					bind:value={capInput}
					aria-describedby="ai-spend-cap-help"
					aria-invalid={capError ? 'true' : undefined}
					data-testid="billing-ai-cap-input"
				/>
				<button type="submit" class="btn-primary" disabled={capSaving} data-testid="billing-ai-cap-save">
					{capSaving ? m('billing.ai.cap.saving') : m('billing.ai.cap.save')}
				</button>
				{#if usage.spend_cap !== null}
					<button
						type="button"
						class="btn-cancel"
						disabled={capSaving}
						onclick={() => saveCap('')}
						data-testid="billing-ai-cap-remove"
					>
						{m('billing.ai.cap.remove')}
					</button>
				{/if}
			</div>
			<p id="ai-spend-cap-help" class="note">{m('billing.ai.cap.help')}</p>
			{#if capError}
				<p class="cap-error" role="alert">{capError}</p>
			{:else if capSaved}
				<p class="cap-saved" role="status">{m('billing.ai.cap.saved')}</p>
			{/if}
		</form>
	{:else if usage && billsOverage && usage.spend_cap !== null}
		<p class="note">
			{m('billing.ai.cap.readOnly')}
			<Money amount={usage.spend_cap} currency={usage.currency} />
		</p>
	{/if}
</section>

<style>
	.ai-usage {
		margin-top: 1.5rem;
		padding-top: 1.25rem;
		border-top: 1px solid var(--border);
	}

	.ai-usage h3 {
		margin: 0 0 0.75rem;
	}

	.period {
		font-weight: 400;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.paused {
		background: var(--warning-tint);
		color: var(--warning-on-tint);
		border-radius: 8px;
		padding: 0.75rem 1rem;
		margin-bottom: 1rem;
	}

	.paused p {
		margin: 0.25rem 0 0;
	}

	.meter-head {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		font-variant-numeric: tabular-nums;
	}

	.pct {
		color: var(--text-muted);
	}

	.track {
		height: 8px;
		margin-top: 0.4rem;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 4px;
		overflow: hidden;
	}

	.fill {
		height: 100%;
		background: var(--accent);
	}

	.tone-warning .fill {
		background: var(--warning-on-tint);
	}

	.tone-danger .fill {
		background: var(--danger);
	}

	.note {
		margin: 0.75rem 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.kpi-row {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 1rem;
		margin-top: 1rem;
	}

	.cap-form {
		margin-top: 1rem;
	}

	.cap-form label {
		display: block;
		font-weight: 600;
		margin-bottom: 0.35rem;
	}

	.cap-row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem;
		align-items: center;
	}

	.cap-row input {
		min-width: 0;
		width: 12rem;
		max-width: 100%;
		padding: 0.5rem 0.75rem;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
		color: var(--text);
		font-variant-numeric: tabular-nums;
	}

	.cap-error {
		margin: 0.5rem 0 0;
		color: var(--danger);
	}

	.cap-saved {
		margin: 0.5rem 0 0;
		color: var(--success);
	}
</style>
