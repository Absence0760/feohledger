<script lang="ts">
	import type { Catalog, PunchoutSession } from '#lib/types/catalog.ts';
	import { punchoutStatusLabelKey, punchoutStatusTone } from '#lib/types/catalog.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import Money from '#lib/components/ui/Money.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import {
		startPunchout,
		getPunchoutSession,
		convertPunchoutSession
	} from '#lib/api/catalogs.ts';

	let {
		catalog,
		onclose,
		onconverted
	}: {
		catalog: Catalog;
		onclose: () => void;
		// Called with the new requisition id once the cart is converted.
		onconverted: (requisitionId: string) => void;
	} = $props();

	// Buyers (admin / ap_manager / ap_clerk) may start + convert; cfo is read-only.
	const canAct = $derived(auth.hasAnyRole('admin', 'ap_manager', 'ap_clerk'));

	let session = $state<PunchoutSession | null>(null);
	let starting = $state(false);
	let refreshing = $state(false);
	let converting = $state(false);

	// The session status is a message key, not an English literal — an
	// unrecognised value from the API renders raw rather than blank.
	const statusLabel = (s: string) => {
		const key = punchoutStatusLabelKey(s);
		return key ? m(key) : s;
	};

	const returned = $derived(session?.status === 'returned');
	const converted = $derived(session?.status === 'converted');

	async function start() {
		starting = true;
		try {
			const res = await startPunchout(catalog.id);
			// Pull the full session so the modal can render the cart once it returns.
			session = await getPunchoutSession(res.session_id);
			// Open the supplier start page in a new tab — the buyer shops there.
			if (res.start_url) window.open(res.start_url, '_blank', 'noopener');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('catalogs.punchout.toast.startFailed'), 'error');
		} finally {
			starting = false;
		}
	}

	async function refresh() {
		if (!session) return;
		refreshing = true;
		try {
			session = await getPunchoutSession(session.id);
			if (session.status !== 'returned' && session.status !== 'converted') {
				toast(m('catalogs.punchout.toast.noCartYet'), 'info');
			}
		} catch (err) {
			toast(err instanceof Error ? err.message : m('catalogs.punchout.toast.refreshFailed'), 'error');
		} finally {
			refreshing = false;
		}
	}

	async function convert() {
		if (!session) return;
		converting = true;
		try {
			const res = await convertPunchoutSession(session.id);
			session = await getPunchoutSession(session.id);
			toast(
				res.created
					? m('catalogs.punchout.toast.converted', { number: res.requisition_number })
					: m('catalogs.punchout.toast.alreadyConverted', { number: res.requisition_number }),
				'success'
			);
			onconverted(res.requisition_id);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('catalogs.punchout.toast.convertFailed'), 'error');
		} finally {
			converting = false;
		}
	}
</script>

<Modal open ariaLabel={m('catalogs.punchout.aria')} title={m('catalogs.punchout.title', { name: catalog.name })} width="md" {onclose}>
	<div class="punchout-body">
		{#if !session}
			<p class="muted">
				{m('catalogs.punchout.intro')}
			</p>
			{#if canAct}
				<button class="btn-primary" onclick={start} disabled={starting}>
					{starting ? m('catalogs.punchout.starting') : m('catalogs.punchout.start')}
				</button>
			{:else}
				<p class="muted">{m('catalogs.punchout.noRole')}</p>
			{/if}
		{:else}
			<div class="status-row">
				<span class="label">{m('catalogs.punchout.status')}</span>
				<Badge tone={punchoutStatusTone(session.status)} variant={session.status}>
					{statusLabel(session.status)}
				</Badge>
			</div>

			{#if session.start_url && !converted}
				<p class="muted">
					{m('catalogs.punchout.shoppingPrompt')}
					<a href={session.start_url} target="_blank" rel="noopener">{m('catalogs.punchout.reopen')}</a>.
				</p>
			{/if}

			{#if !returned && !converted}
				<button class="btn-secondary" onclick={refresh} disabled={refreshing}>
					{refreshing ? m('catalogs.punchout.checking') : m('catalogs.punchout.refresh')}
				</button>
			{/if}

			{#if (returned || converted) && session.cart_items.length > 0}
				<table class="cart">
					<thead>
						<tr>
							<th>{m('catalogs.punchout.col.item')}</th>
							<th class="right">{m('catalogs.punchout.col.qty')}</th>
							<th class="right">{m('catalogs.punchout.col.unitPrice')}</th>
						</tr>
					</thead>
					<tbody>
						{#each session.cart_items as it, i (i)}
							<tr>
								<td>{it.description}{#if it.sku}<span class="sku"> · {it.sku}</span>{/if}</td>
								<td class="right">{it.quantity ?? '—'}</td>
								<td class="right">
									{#if it.unit_price != null}
										<Money amount={it.unit_price} currency={it.currency} />
									{:else}—{/if}
								</td>
							</tr>
						{/each}
					</tbody>
					<tfoot>
						<tr>
							<td colspan="2" class="right"><strong>{m('catalogs.punchout.cartTotal')}</strong></td>
							<td class="right">
								<strong><Money amount={session.cart_total ?? 0} currency={session.currency} /></strong>
							</td>
						</tr>
					</tfoot>
				</table>
			{/if}

			{#if converted && session.converted_requisition_id}
				<p class="muted">{m('catalogs.punchout.convertedNote')}</p>
			{/if}
		{/if}

		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={onclose}>{m('catalogs.punchout.close')}</button>
			{#if returned && canAct}
				<button type="button" class="btn-primary" onclick={convert} disabled={converting}>
					{converting ? m('catalogs.punchout.converting') : m('catalogs.punchout.convert')}
				</button>
			{/if}
		</div>
	</div>
</Modal>

<style>
	.punchout-body {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.muted {
		color: var(--text-muted);
		font-size: 0.86rem;
		margin: 0;
	}
	.status-row {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.label {
		font-size: 0.78rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
	}
	/* The session pill is `<Badge>` now — the grey base plus its two tinted
	   variants were a third spelling of tones the palette already names. The
	   tone per status lives beside the labels in `types/catalog`; `.status-row`
	   above still owns the placement. */
	.cart {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.86rem;
	}
	.cart th,
	.cart td {
		padding: 6px 8px;
		border-bottom: 1px solid var(--border);
		text-align: left;
	}
	.cart .right {
		text-align: right;
	}
	.cart .sku {
		color: var(--text-muted);
	}
</style>
