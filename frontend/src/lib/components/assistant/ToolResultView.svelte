<script lang="ts">
	import Money from '#lib/components/ui/Money.svelte';
	import StatusBadge from '#lib/components/ui/StatusBadge.svelte';
	import SpendBarChart from '#lib/components/assistant/SpendBarChart.svelte';
	import { formatMoney } from '#lib/utils/money.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { InvoiceStatus } from '#lib/types/invoice.ts';
	import { invoiceStatusLabelKey } from '#lib/types/invoice.ts';
	import type {
		ToolInvocation,
		VendorSpendResult,
		ForecastResult,
		InvoiceListResult,
		PendingApprovalsResult,
		TextSearchResult
	} from '#lib/types/assistant.ts';
	import { formatDate } from '#lib/utils/time.ts';

	let { invocation }: { invocation: ToolInvocation } = $props();

	// $derived so titles re-render when the locale changes.
	let TOOL_TITLES = $derived<Record<string, string>>({
		get_vendor_spend: m('assistant.tool.vendorSpend'),
		get_payment_forecast: m('assistant.tool.paymentForecast'),
		list_invoices: m('assistant.tool.invoices'),
		list_pending_approvals: m('assistant.tool.pendingApprovals'),
		find_invoices_by_text: m('assistant.tool.matchingInvoices')
	});

	let title = $derived(TOOL_TITLES[invocation.tool] ?? invocation.tool);

	// Narrowed views of `result` per tool. The cast is safe because the page
	// only routes a tool's own result to its branch via `invocation.tool`.
	let result = $derived(invocation.result as Record<string, unknown> | null);

	function numOf(v: unknown): number {
		const n = typeof v === 'string' ? parseFloat(v) : Number(v);
		return Number.isFinite(n) ? n : 0;
	}

	function fmtDate(d?: string | null): string {
		return formatDate(d, '—', { month: 'short', day: 'numeric', year: '2-digit' });
	}

	// A tool status string maps onto the known InvoiceStatus union for the
	// badge; an unrecognised value still renders the badge label fallback.
	function asStatus(s: string): InvoiceStatus {
		return (invoiceStatusLabelKey(s) ? s : 'new') as InvoiceStatus;
	}

	let spend = $derived(
		invocation.tool === 'get_vendor_spend' ? (result as unknown as VendorSpendResult | null) : null
	);
	let forecast = $derived(
		invocation.tool === 'get_payment_forecast'
			? (result as unknown as ForecastResult | null)
			: null
	);
	let invoiceList = $derived(
		invocation.tool === 'list_invoices' ? (result as unknown as InvoiceListResult | null) : null
	);
	let approvals = $derived(
		invocation.tool === 'list_pending_approvals'
			? (result as unknown as PendingApprovalsResult | null)
			: null
	);
	let textSearch = $derived(
		invocation.tool === 'find_invoices_by_text'
			? (result as unknown as TextSearchResult | null)
			: null
	);

	let spendBars = $derived(
		spend
			? spend.vendors.map((v) => ({
					label: v.vendor_name,
					value: numOf(v.amount),
					amountLabel: formatMoney(v.amount, { currency: spend!.currency, whole: true }),
					sub: `${numOf(v.share_pct).toFixed(1)}%`
				}))
			: []
	);
	let forecastBars = $derived(
		forecast
			? forecast.buckets.map((b) => ({
					label: fmtDate(b.period),
					value: numOf(b.amount),
					amountLabel: formatMoney(b.amount, { currency: forecast!.currency, whole: true }),
					sub: m('assistant.tool.invCount', { count: b.count })
				}))
			: []
	);
</script>

<div class="tool-result" data-tool={invocation.tool}>
	{#if invocation.error}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
		</div>
		<p class="tool-err">{m('assistant.tool.error', { error: invocation.error })}</p>
	{:else if spend}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
			<span class="tool-meta">
				{m('assistant.tool.meta', {
					label: spend.period_label,
					total: formatMoney(spend.total_spend, { currency: spend.currency, whole: true })
				})}
			</span>
		</div>
		<SpendBarChart bars={spendBars} />
	{:else if forecast}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
			<span class="tool-meta">
				{m('assistant.tool.meta', {
					label: forecast.horizon_label,
					total: formatMoney(forecast.total, { currency: forecast.currency, whole: true })
				})}
			</span>
		</div>
		<SpendBarChart bars={forecastBars} />
	{:else if invoiceList}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
			<span class="tool-meta">{m('assistant.tool.totalCount', { total: invoiceList.total })}</span>
		</div>
		{#if invoiceList.items.length === 0}
			<p class="tool-empty">{m('assistant.tool.noInvoicesMatched')}</p>
		{:else}
			<table class="mini-table">
				<thead>
					<tr><th>{m('assistant.tool.col.invoice')}</th><th>{m('assistant.tool.col.vendor')}</th><th class="num">{m('assistant.tool.col.amount')}</th><th>{m('assistant.tool.col.status')}</th><th>{m('assistant.tool.col.due')}</th></tr>
				</thead>
				<tbody>
					{#each invoiceList.items as row (row.id)}
						<tr>
							<td class="mono">{row.invoice_number}</td>
							<td>{row.vendor_name}</td>
							<td class="num"><Money amount={row.amount} currency={row.currency} mono /></td>
							<td><StatusBadge status={asStatus(row.status)} /></td>
							<td>{fmtDate(row.due_date)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{:else if approvals}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
			<span class="tool-meta">{m('assistant.tool.totalCount', { total: approvals.total })}</span>
		</div>
		{#if approvals.items.length === 0}
			<p class="tool-empty">{m('assistant.tool.nothingPending')}</p>
		{:else}
			<table class="mini-table">
				<thead>
					<tr><th>{m('assistant.tool.col.invoice')}</th><th>{m('assistant.tool.col.vendor')}</th><th class="num">{m('assistant.tool.col.amount')}</th><th>{m('assistant.tool.col.waitingSince')}</th></tr>
				</thead>
				<tbody>
					{#each approvals.items as row (row.invoice_id)}
						<tr>
							<td class="mono">{row.invoice_number}</td>
							<td>{row.vendor_name}</td>
							<td class="num"><Money amount={row.amount} currency={row.currency} mono /></td>
							<td>{fmtDate(row.waiting_since)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{:else if textSearch}
		<div class="tool-head">
			<span class="tool-title">{title}</span>
			<span class="tool-meta">{m('assistant.tool.matchCount', { count: textSearch.matches.length })}</span>
		</div>
		{#if textSearch.matches.length === 0}
			<p class="tool-empty">{m('assistant.tool.noMatchingInvoices')}</p>
		{:else}
			<div class="match-list">
				{#each textSearch.matches as match (match.invoice_id)}
					<div class="match-card">
						<div class="match-head">
							<span class="match-vendor">{match.vendor_name ?? m('assistant.tool.unknownVendor')}</span>
							<span class="match-sim">{m('assistant.tool.percentMatch', { percent: (match.similarity * 100).toFixed(0) })}</span>
						</div>
						<p class="match-snippet">{match.snippet}</p>
					</div>
				{/each}
			</div>
		{/if}
	{:else}
		<!-- Unrecognised tool: render the structured payload as fallback JSON. -->
		<div class="tool-head">
			<span class="tool-title">{title}</span>
		</div>
		<pre class="tool-json">{JSON.stringify(invocation.result, null, 2)}</pre>
	{/if}
</div>

<style>
	.tool-result {
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 14px 16px;
		background: var(--surface);
		margin-top: 10px;
	}
	.tool-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 10px;
	}
	.tool-title {
		font-size: 0.82rem;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text);
	}
	.tool-meta {
		font-size: 0.76rem;
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.tool-err {
		color: var(--danger);
		font-size: 0.85rem;
		margin: 0;
	}
	.tool-empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0;
	}
	.mini-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.mini-table th {
		text-align: left;
		font-weight: 600;
		color: var(--text-muted);
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		padding: 4px 8px;
		border-bottom: 1px solid var(--border);
	}
	.mini-table td {
		padding: 6px 8px;
		border-bottom: 1px solid rgba(128, 128, 128, 0.12);
	}
	.mini-table tr:last-child td {
		border-bottom: none;
	}
	.mini-table .num {
		text-align: right;
	}
	.mono {
		font-variant-numeric: tabular-nums;
		font-family: var(--font-mono);
	}
	.match-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.match-card {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 8px 12px;
	}
	.match-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 4px;
	}
	.match-vendor {
		font-weight: 600;
		font-size: 0.84rem;
	}
	.match-sim {
		font-size: 0.74rem;
		color: var(--accent);
		font-variant-numeric: tabular-nums;
	}
	.match-snippet {
		margin: 0;
		font-size: 0.82rem;
		color: var(--text-muted);
		line-height: 1.4;
	}
	.tool-json {
		margin: 0;
		font-size: 0.76rem;
		background: rgba(128, 128, 128, 0.08);
		border-radius: 6px;
		padding: 10px;
		overflow-x: auto;
		max-height: 280px;
	}
</style>
