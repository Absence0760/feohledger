<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { formatDate } from '$lib/utils/time';
	import { auth } from '$lib/stores/auth.svelte';
	import PageHeader from '$lib/components/ui/PageHeader.svelte';
	import DataTable from '$lib/components/ui/DataTable.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import SecretReveal from '$lib/components/ui/SecretReveal.svelte';
	import FilterChips from '$lib/components/ui/FilterChips.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import RowLink from '$lib/components/ui/RowLink.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '$lib/components/ui/badgeTone';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { isRowOpenClick } from '$lib/utils/rowNav';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import {
		listWebhookSubscriptions,
		createWebhookSubscription,
		updateWebhookSubscription,
		rotateWebhookSecret,
		deleteWebhookSubscription,
		listWebhookDeliveries,
		redeliverWebhookDelivery
	} from '$lib/api/webhooks';
	import {
		WEBHOOK_EVENT_TYPES,
		type WebhookSubscription,
		type WebhookSubscriptionCreated,
		type WebhookSecretRotated,
		type WebhookDelivery
	} from '$lib/types/webhooks';
	import {
		OVERLAP_CHOICES,
		OVERLAP_DEFAULT_MINUTES,
		isOverlapLive
	} from '$lib/utils/webhookRotation';

	// RBAC: the backend gates every /api/webhooks endpoint to admin only and
	// 403s the rest. Wait for `auth.user` to resolve before redirecting so we
	// don't bounce before /me lands (api-keys / billing document the same race).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	// ── Subscriptions ────────────────────────────────────────────────────────
	// $derived so the column headers re-render when the locale changes.
	let SUB_COLUMNS = $derived([
		{ label: m('admin.webhooks.sub.col.name') },
		{ label: m('admin.webhooks.sub.col.targetUrl') },
		{ label: m('admin.webhooks.sub.col.events') },
		{ label: m('admin.webhooks.sub.col.secret') },
		{ label: m('admin.webhooks.sub.col.created') },
		{ label: m('admin.webhooks.sub.col.status') },
		{ class: 'actions-col' }
	]);

	let subs = $state<WebhookSubscription[]>([]);
	let subsLoading = $state(true);
	let subsError = $state<string | null>(null);

	// Create flow.
	let creating = $state(false);
	let newName = $state('');
	let newUrl = $state('');
	let newEvents = $state<Set<string>>(new Set(['invoice.approved']));
	let saving = $state(false);

	// One-time secret reveal (after a successful create). `SecretReveal` owns the
	// copy affordance + the shown-once warning; this page only holds the value
	// long enough to render it and drops it on close.
	let minted = $state<WebhookSubscriptionCreated | null>(null);

	// Edit flow.
	let editing = $state<WebhookSubscription | null>(null);
	let editName = $state('');
	let editUrl = $state('');
	let editEvents = $state<Set<string>>(new Set());
	let editActive = $state(true);
	let editSaving = $state(false);

	// Delete confirm (armed two-click on the row action).
	let confirmDeleteId = $state<string | null>(null);

	// ── Secret rotation ──────────────────────────────────────────────────────
	// Rotating mints a replacement signing secret while KEEPING the subscription
	// id — and therefore its whole delivery history. That matters because the
	// only other route off a leaked secret is Delete + re-create, which CASCADEs
	// the delivery log away: recovering from a leak would mean destroying the
	// record of what had been delivered. Delete must not be the easier
	// affordance during an incident.
	//
	// Confirm-then-act via a dialog rather than the armed two-click the Delete
	// row action uses: the rotation needs an overlap choice, and the picker has
	// to live somewhere. The dialog IS the confirmation step.
	let rotating = $state<WebhookSubscription | null>(null);
	let rotateOverlap = $state(OVERLAP_DEFAULT_MINUTES);
	let rotateSaving = $state(false);

	// The replacement secret, shown exactly once — same contract as `minted`.
	let rotated = $state<WebhookSecretRotated | null>(null);

	// Subscription id → the instant the retiring secret stops signing, so an
	// admin can see a rotation is mid-flight rather than guessing.
	//
	// In memory only, and deliberately: `GET /api/webhooks` does not return
	// `previous_secret_expires_at`, so a reload has nothing to rebuild this
	// from. It still earns its place — the window matters exactly while the
	// admin is on this page pasting the new secret into their receiver. The
	// durable fix is that field on the list response; tracked in
	// docs/followups.md. Never holds a secret, only an expiry timestamp.
	// The overlap window comes off the listed row (`previous_secret_expires_at`
	// on GET /api/webhooks), so it survives a reload — which matters precisely
	// when the admin has navigated away to paste the new secret into their
	// receiver. Never a secret, only an expiry timestamp.
	//
	// The badge must disappear on its own when the window elapses, and a bare
	// Date.now() read isn't reactive — so tick a clock while any window is open.
	// The interval is self-terminating: once no row has a live window the
	// effect re-runs and clears it rather than ticking for the page's life.
	let clock = $state(Date.now());
	$effect(() => {
		if (!subs.some((s) => isOverlapLive(s.previous_secret_expires_at, clock))) return;
		const t = setInterval(() => (clock = Date.now()), 30_000);
		return () => clearInterval(t);
	});

	/** The live overlap expiry for a subscription, or null if none is running. */
	function overlapActiveUntil(sub: WebhookSubscription): string | null {
		const until = sub.previous_secret_expires_at;
		return until && isOverlapLive(until, clock) ? until : null;
	}

	// Short date + time: a 24-hour window can end tomorrow, so a bare clock time
	// would be ambiguous about which day the old secret dies.
	const OVERLAP_TIME_OPTS: Intl.DateTimeFormatOptions = {
		month: 'short',
		day: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	};

	function formatOverlapEnd(iso: string): string {
		return formatDate(iso, '', OVERLAP_TIME_OPTS);
	}

	// Two INDEPENDENT lists on this page — subscriptions and their delivery log
	// — so each gets its OWN sequencer (a shared counter would let a delivery
	// refresh mark the subscription reload un-committable and blank that table).
	// Every mutation re-fetches through these loaders rather than editing a row
	// in place, so neither needs `supersedeInFlight()`. See
	// `frontend/CLAUDE.md` § Sequencing list fetches.
	const subsSequence = createRequestSequencer();
	const deliveriesSequence = createRequestSequencer();

	async function loadSubs() {
		const token = subsSequence.start();
		subsLoading = true;
		subsError = null;
		try {
			const rows = await listWebhookSubscriptions();
			// Superseded by a newer load — discard rather than clobber. Create,
			// rotate and delete each re-fetch, so two can be in flight at once.
			if (!subsSequence.canCommit(token)) return;
			subs = rows;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: only the newest request owns
			// the error banner — a stale failure would replace a good table with
			// one.
			if (!subsSequence.isCurrentRequest(token)) return;
			subsError = e instanceof Error ? e.message : m('admin.webhooks.subsLoadFailed');
		} finally {
			if (subsSequence.isCurrentRequest(token)) subsLoading = false;
		}
	}

	function openCreate() {
		newName = '';
		newUrl = '';
		newEvents = new Set(['invoice.approved']);
		creating = true;
	}

	function toggleNewEvent(evt: string) {
		const next = new Set(newEvents);
		if (next.has(evt)) next.delete(evt);
		else next.add(evt);
		newEvents = next;
	}

	async function handleCreate() {
		const name = newName.trim();
		const url = newUrl.trim();
		if (!name || !url || newEvents.size === 0) return;
		saving = true;
		try {
			const created = await createWebhookSubscription({
				name,
				target_url: url,
				event_types: [...newEvents]
			});
			creating = false;
			// Show the secret exactly once. Keep the list fresh too.
			minted = created;
			await loadSubs();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.webhooks.toast.createFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	function dismissMinted() {
		// Drop the secret from memory the moment the reveal closes.
		minted = null;
	}

	function openEdit(sub: WebhookSubscription) {
		editing = sub;
		editName = sub.name;
		editUrl = sub.target_url;
		editEvents = new Set(sub.event_types);
		editActive = sub.active;
	}

	function toggleEditEvent(evt: string) {
		const next = new Set(editEvents);
		if (next.has(evt)) next.delete(evt);
		else next.add(evt);
		editEvents = next;
	}

	async function handleEdit() {
		if (!editing) return;
		const name = editName.trim();
		const url = editUrl.trim();
		if (!name || !url || editEvents.size === 0) return;
		editSaving = true;
		try {
			await updateWebhookSubscription(editing.id, {
				name,
				target_url: url,
				event_types: [...editEvents],
				active: editActive
			});
			editing = null;
			toast(m('admin.webhooks.toast.updated'), 'success');
			await loadSubs();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.webhooks.toast.updateFailed'), 'error');
		} finally {
			editSaving = false;
		}
	}

	function closeRotate() {
		// Closing does NOT cancel an in-flight rotation — by the time we could
		// react the backend has already minted the replacement, and the reveal is
		// the ONLY place it is ever shown. Dismissing mid-request would strand the
		// admin with a rotated secret they never saw and a receiver still on the
		// old one, so the dialog holds until the request settles.
		if (rotateSaving) return;
		rotating = null;
	}

	function openRotate(sub: WebhookSubscription) {
		// Un-arm a pending Delete — the window-click handler that normally does
		// this ignores clicks inside `.row-action`, and leaving Delete armed
		// behind the rotate dialog is a loaded gun.
		confirmDeleteId = null;
		rotating = sub;
		rotateOverlap = OVERLAP_DEFAULT_MINUTES;
	}

	async function handleRotate() {
		if (!rotating) return;
		const subId = rotating.id;
		rotateSaving = true;
		try {
			const result = await rotateWebhookSecret(subId, rotateOverlap);
			rotating = null;
			// Show the replacement exactly once.
			rotated = result;
			// Re-list: the row now carries the new secret's prefix AND the
			// authoritative overlap expiry (or null on a hard cutover), so the
			// pill is driven by server state rather than a local guess.
			await loadSubs();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.webhooks.toast.rotateFailed'), 'error');
		} finally {
			rotateSaving = false;
		}
	}

	function dismissRotated() {
		// Drop the secret from memory the moment the reveal closes.
		rotated = null;
	}

	async function handleDelete(id: string) {
		try {
			await deleteWebhookSubscription(id);
			toast(m('admin.webhooks.toast.deleted'), 'success');
			await loadSubs();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.webhooks.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
		}
	}

	// ── Deliveries ───────────────────────────────────────────────────────────
	// $derived so the column headers re-render when the locale changes.
	let DELIVERY_COLUMNS = $derived([
		{ label: m('admin.webhooks.delivery.col.event') },
		{ label: m('admin.webhooks.delivery.col.eventId') },
		{ label: m('admin.webhooks.delivery.col.attempts') },
		{ label: m('admin.webhooks.delivery.col.response') },
		{ label: m('admin.webhooks.delivery.col.lastAttempt') },
		{ label: m('admin.webhooks.delivery.col.status') },
		{ class: 'actions-col' }
	]);

	const DELIVERY_STATUSES = ['pending', 'delivered', 'failed', 'dead'] as const;

	/**
	 * Badge tone per delivery status. Keyed off the same four values
	 * `DELIVERY_STATUSES` filters on; `WebhookDelivery.status` is a bare
	 * string, so an unknown value falls back to the flat `neutral` chip — the
	 * pill equivalent of what `deliveryStatusLabel` already does with the
	 * label.
	 *
	 * `dead` shares `danger` with `failed` and `pending` shares `warning` with
	 * a paused subscription; both pairs were already one CSS rule each, so no
	 * distinction is lost here.
	 */
	const DELIVERY_STATUS_TONES: Record<string, BadgeTone> = {
		pending: 'warning',
		delivered: 'success',
		failed: 'danger',
		dead: 'danger'
	};

	function deliveryTone(status: string): BadgeTone {
		return DELIVERY_STATUS_TONES[status] ?? 'neutral';
	}

	function deliveryStatusLabel(s: string): string {
		switch (s) {
			case 'pending':
				return m('admin.webhooks.filter.pending');
			case 'delivered':
				return m('admin.webhooks.filter.delivered');
			case 'failed':
				return m('admin.webhooks.filter.failed');
			case 'dead':
				return m('admin.webhooks.filter.dead');
			default:
				// Unknown status from the API — degrade to the raw value rather
				// than rendering blank.
				return s;
		}
	}

	let deliveries = $state<WebhookDelivery[]>([]);
	let deliveriesLoading = $state(true);
	let deliveriesError = $state<string | null>(null);
	let redeliveringId = $state<string | null>(null);

	// URL-backed status filter (so a deep link / reload preserves the view).
	const statusFilter = $derived($page.url.searchParams.get('status') ?? 'all');

	const deliveryChips = $derived([
		{ key: 'all', label: m('admin.webhooks.filter.all') },
		...DELIVERY_STATUSES.map((s) => ({ key: s, label: deliveryStatusLabel(s) }))
	]);

	function setStatusFilter(next: string) {
		const url = new URL($page.url);
		if (next === 'all') url.searchParams.delete('status');
		else url.searchParams.set('status', next);
		goto(`${url.pathname}${url.search}`, { replaceState: true, keepFocus: true, noScroll: true });
	}

	async function loadDeliveries() {
		const token = deliveriesSequence.start();
		deliveriesLoading = true;
		deliveriesError = null;
		try {
			const rows = await listWebhookDeliveries({
				status: statusFilter === 'all' ? undefined : statusFilter,
				pageSize: 50
			});
			// Superseded by a newer load — discard rather than clobber. Two fast
			// status-chip clicks otherwise let the first filter's response land
			// last and fill the table with rows the active chip excludes.
			if (!deliveriesSequence.canCommit(token)) return;
			deliveries = rows;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: only the newest request reports.
			if (!deliveriesSequence.isCurrentRequest(token)) return;
			deliveriesError = e instanceof Error ? e.message : m('admin.webhooks.deliveriesLoadFailed');
		} finally {
			if (deliveriesSequence.isCurrentRequest(token)) deliveriesLoading = false;
		}
	}

	async function handleRedeliver(d: WebhookDelivery) {
		redeliveringId = d.id;
		try {
			await redeliverWebhookDelivery(d.id);
			toast(m('admin.webhooks.toast.requeued'), 'success');
			await loadDeliveries();
		} catch (e) {
			// 409 when the delivery is already delivered — surface the backend
			// message rather than crashing.
			toast(e instanceof Error ? e.message : m('admin.webhooks.toast.redeliverFailed'), 'error');
		} finally {
			redeliveringId = null;
		}
	}

	function canRedeliver(d: WebhookDelivery): boolean {
		return d.status === 'failed' || d.status === 'dead';
	}

	// ── Lifecycle ────────────────────────────────────────────────────────────
	$effect(() => {
		// Only fetch once we know the role is allowed (avoids a guaranteed 403
		// for a non-admin before the redirect fires).
		if (userLoaded && allowed) loadSubs();
	});

	$effect(() => {
		// Re-fetch deliveries whenever the role resolves or the status filter
		// (URL-backed) changes. Reading `statusFilter` registers the dependency.
		const s = statusFilter;
		void s;
		if (userLoaded && allowed) loadDeliveries();
	});

	function handleWindowClick(e: MouseEvent) {
		if (confirmDeleteId && !(e.target as HTMLElement).closest('.row-action')) {
			confirmDeleteId = null;
		}
	}

</script>

<svelte:window onclick={handleWindowClick} />

<PageHeader title={m('admin.webhooks.title')}>
	{#snippet actions()}
		<button class="btn-primary" onclick={openCreate}>{m('admin.webhooks.createWebhook')}</button>
	{/snippet}

	<p class="page-hint">
		{m('admin.webhooks.hintPre')}
		<code>X-Webhook-Signature</code> {m('admin.webhooks.hintPost')}
	</p>

	<section aria-labelledby="subs-heading">
		<h2 id="subs-heading" class="section-heading">{m('admin.webhooks.subscriptions')}</h2>
		{#if subsLoading}
			<p class="state" data-testid="webhooks-loading">{m('admin.webhooks.loadingSubs')}</p>
		{:else if subsError}
			<div class="state error" data-testid="webhooks-error" role="alert">
				<p>{subsError}</p>
				<button type="button" class="btn-cancel" onclick={loadSubs}>{m('admin.webhooks.retry')}</button>
			</div>
		{:else}
			<DataTable
				columns={SUB_COLUMNS}
				isEmpty={subs.length === 0}
				empty={m('admin.webhooks.subsEmpty')}
			>
				{#snippet body()}
					{#each subs as sub (sub.id)}
						{@const overlapEnds = overlapActiveUntil(sub)}
						<tr
							class="clickable"
							class:row-muted={!sub.active}
							onclick={(e) => {
								if (isRowOpenClick(e)) openEdit(sub);
							}}
						>
							<td>
								<RowLink onclick={() => openEdit(sub)} ariaLabel={m('admin.webhooks.editAria', { name: sub.name })}>
									{sub.name}
								</RowLink>
							</td>
							<td class="url-cell" title={sub.target_url}>{sub.target_url}</td>
							<td class="events-cell">{sub.event_types.join(', ')}</td>
							<td class="mono">
								{sub.secret_prefix}…
								{#if overlapEnds}
									<!-- The wrapper carries what the pill itself cannot: the
									     test id, the 6px of placement, and the escape from
									     the surrounding `.mono` cell's font. -->
									<span class="overlap-pill-wrap" data-testid="overlap-pill">
										<Badge
											tone="warning"
											variant="overlap-pill"
											title={m('admin.webhooks.overlapTitle')}
										>
											{m('admin.webhooks.overlapPill', { time: formatOverlapEnd(overlapEnds) })}
										</Badge>
									</span>
								{/if}
							</td>
							<td>{formatDate(sub.created_at)}</td>
							<td>
								{#if sub.active}
									<Badge tone="success" variant="status-pill active">
										{m('admin.webhooks.statusActive')}
									</Badge>
								{:else}
									<Badge tone="warning" variant="status-pill paused">
										{m('admin.webhooks.statusInactive')}
									</Badge>
								{/if}
							</td>
							<td class="actions">
								<RowAction
									ariaLabel={m('admin.webhooks.rotateAria', { name: sub.name })}
									onclick={(e) => {
										e.stopPropagation();
										openRotate(sub);
									}}
								>
									{m('admin.webhooks.row.rotate')}
								</RowAction>
								<RowAction
									variant="danger"
									armed={confirmDeleteId === sub.id}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmDeleteId === sub.id) {
											handleDelete(sub.id);
										} else {
											confirmDeleteId = sub.id;
										}
									}}
								>
									{confirmDeleteId === sub.id ? m('admin.webhooks.row.confirm') : m('admin.webhooks.row.delete')}
								</RowAction>
							</td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>
		{/if}
	</section>

	<section aria-labelledby="deliveries-heading">
		<h2 id="deliveries-heading" class="section-heading">{m('admin.webhooks.deliveries')}</h2>

		<FilterChips
			chips={deliveryChips}
			active={statusFilter}
			onchange={setStatusFilter}
		/>

		{#if deliveriesLoading}
			<p class="state" data-testid="deliveries-loading">{m('admin.webhooks.loadingDeliveries')}</p>
		{:else if deliveriesError}
			<div class="state error" data-testid="deliveries-error" role="alert">
				<p>{deliveriesError}</p>
				<button type="button" class="btn-cancel" onclick={loadDeliveries}>{m('admin.webhooks.retry')}</button>
			</div>
		{:else}
			<DataTable
				columns={DELIVERY_COLUMNS}
				isEmpty={deliveries.length === 0}
				empty={m('admin.webhooks.deliveriesEmpty')}
			>
				{#snippet body()}
					{#each deliveries as d (d.id)}
						<tr>
							<td>{d.event_type}</td>
							<td class="mono">{d.event_id}</td>
							<td>{d.attempt_count}</td>
							<td>{d.response_code ?? '—'}</td>
							<td>{formatDate(d.last_attempt_at)}</td>
							<td>
								<Badge tone={deliveryTone(d.status)} variant="status-pill {d.status}">
									{deliveryStatusLabel(d.status)}
								</Badge>
							</td>
							<td class="actions">
								{#if canRedeliver(d)}
									<RowAction
										disabled={redeliveringId === d.id}
										onclick={() => handleRedeliver(d)}
									>
										{redeliveringId === d.id ? m('admin.webhooks.row.redelivering') : m('admin.webhooks.row.redeliver')}
									</RowAction>
								{/if}
							</td>
						</tr>
					{/each}
				{/snippet}
			</DataTable>
		{/if}
	</section>
</PageHeader>

<!-- Create webhook modal -->
<Modal open={creating} ariaLabel={m('admin.webhooks.create.aria')} width="md" onclose={() => (creating = false)}>
	<h2>{m('admin.webhooks.create.heading')}</h2>
	<p class="modal-hint">
		{m('admin.webhooks.create.hintPre')} <strong>{m('admin.webhooks.create.hintOnce')}</strong>{m('admin.webhooks.create.hintPost')}
	</p>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleCreate();
		}}
	>
		<label>
			<span>{m('admin.webhooks.field.name')} <em class="required">*</em></span>
			<input
				type="text"
				bind:value={newName}
				required
				maxlength="120"
				placeholder={m('admin.webhooks.field.namePlaceholder')}
			/>
		</label>
		<label>
			<span>{m('admin.webhooks.field.targetUrl')} <em class="required">*</em></span>
			<input
				type="url"
				bind:value={newUrl}
				required
				maxlength="2048"
				placeholder={m('admin.webhooks.field.targetUrlPlaceholder')}
			/>
		</label>
		<fieldset class="events-field">
			<legend>{m('admin.webhooks.field.events')} <em class="required">*</em></legend>
			{#each WEBHOOK_EVENT_TYPES as evt (evt)}
				<label class="checkbox-line">
					<input
						type="checkbox"
						checked={newEvents.has(evt)}
						onchange={() => toggleNewEvent(evt)}
					/>
					<span class="mono">{evt}</span>
				</label>
			{/each}
		</fieldset>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (creating = false)}>{m('common.cancel')}</button>
			<button
				type="submit"
				class="btn-primary"
				disabled={!newName.trim() || !newUrl.trim() || newEvents.size === 0 || saving}
			>
				{saving ? m('admin.webhooks.create.creating') : m('admin.webhooks.create.create')}
			</button>
		</div>
	</form>
</Modal>

<!-- One-time signing-secret reveal (create) -->
<SecretReveal
	open={minted !== null}
	ariaLabel={m('admin.webhooks.reveal.aria')}
	heading={m('admin.webhooks.reveal.heading')}
	warningStrong={m('admin.webhooks.reveal.warningStrong')}
	warning={m('admin.webhooks.reveal.warning')}
	secret={minted?.signing_secret ?? ''}
	testId="minted-secret"
	copyLabel={m('admin.webhooks.reveal.copy')}
	copiedLabel={m('admin.webhooks.reveal.copied')}
	copiedToast={m('admin.webhooks.toast.secretCopied')}
	copyFailedToast={m('admin.webhooks.toast.copyFailed')}
	doneLabel={m('admin.webhooks.reveal.done')}
	meta={minted
		? [
				{ label: m('admin.webhooks.reveal.name'), value: minted.subscription.name },
				{
					label: m('admin.webhooks.reveal.prefix'),
					value: `${minted.subscription.secret_prefix}…`,
					mono: true
				}
			]
		: []}
	onclose={dismissMinted}
/>

<!-- Rotate signing secret — confirm + overlap picker -->
<Modal
	open={rotating !== null}
	ariaLabel={m('admin.webhooks.rotate.aria')}
	width="md"
	onclose={closeRotate}
>
	{#if rotating}
		{@const reRotating = overlapActiveUntil(rotating)}
		<h2>{m('admin.webhooks.rotate.heading')}</h2>
		<p class="modal-hint">{m('admin.webhooks.rotate.hint', { name: rotating.name })}</p>
		{#if reRotating}
			<!-- The backend keeps ONE previous-secret slot, so rotating again now
			     evicts the secret that window was protecting: a receiver still on
			     the original is cut off immediately, whatever window is chosen. -->
			<div class="cutover-warning" role="alert" data-testid="rerotate-warning">
				{m('admin.webhooks.rotate.reRotateWarning', { time: formatOverlapEnd(reRotating) })}
			</div>
		{/if}
		<form
			onsubmit={(e) => {
				e.preventDefault();
				handleRotate();
			}}
		>
			<fieldset class="events-field">
				<legend>{m('admin.webhooks.rotate.overlapLegend')}</legend>
				<p class="field-hint">{m('admin.webhooks.rotate.overlapHint')}</p>
				{#each OVERLAP_CHOICES as choice (choice.minutes)}
					<label class="checkbox-line">
						<input
							type="radio"
							name="overlap-minutes"
							checked={rotateOverlap === choice.minutes}
							onchange={() => (rotateOverlap = choice.minutes)}
						/>
						<span>{m(choice.labelKey)}</span>
					</label>
				{/each}
			</fieldset>
			{#if rotateOverlap === 0}
				<div class="cutover-warning" role="alert" data-testid="cutover-warning">
					{m('admin.webhooks.rotate.cutoverWarning')}
				</div>
			{/if}
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeRotate} disabled={rotateSaving}
					>{m('common.cancel')}</button
				>
				<button type="submit" class="btn-primary" disabled={rotateSaving}>
					{rotateSaving ? m('admin.webhooks.rotate.rotating') : m('admin.webhooks.rotate.rotate')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<!-- One-time signing-secret reveal (rotation) -->
<SecretReveal
	open={rotated !== null}
	ariaLabel={m('admin.webhooks.rotated.aria')}
	heading={m('admin.webhooks.rotated.heading')}
	warningStrong={m('admin.webhooks.rotated.warningStrong')}
	warning={m('admin.webhooks.rotated.warning')}
	secret={rotated?.signing_secret ?? ''}
	testId="rotated-secret"
	copyLabel={m('admin.webhooks.reveal.copy')}
	copiedLabel={m('admin.webhooks.reveal.copied')}
	copiedToast={m('admin.webhooks.toast.secretCopied')}
	copyFailedToast={m('admin.webhooks.toast.copyFailed')}
	doneLabel={m('admin.webhooks.reveal.done')}
	meta={rotated
		? [
				{ label: m('admin.webhooks.reveal.name'), value: rotated.subscription.name },
				{
					label: m('admin.webhooks.reveal.prefix'),
					value: `${rotated.subscription.secret_prefix}…`,
					mono: true
				}
			]
		: []}
	onclose={dismissRotated}
>
	{#snippet note()}
		{#if rotated}
			<p class="overlap-note" data-testid="rotation-overlap-note">
				{rotated.previous_secret_expires_at
					? m('admin.webhooks.rotated.overlapNote', {
							time: formatOverlapEnd(rotated.previous_secret_expires_at)
						})
					: m('admin.webhooks.rotated.cutoverNote')}
			</p>
		{/if}
	{/snippet}
</SecretReveal>

<!-- Edit webhook modal -->
<Modal open={editing !== null} ariaLabel={m('admin.webhooks.edit.aria')} width="md" onclose={() => (editing = null)}>
	{#if editing}
		<h2>{m('admin.webhooks.edit.heading')}</h2>
		<form
			onsubmit={(e) => {
				e.preventDefault();
				handleEdit();
			}}
		>
			<label>
				<span>{m('admin.webhooks.field.name')} <em class="required">*</em></span>
				<input type="text" bind:value={editName} required maxlength="120" />
			</label>
			<label>
				<span>{m('admin.webhooks.field.targetUrl')} <em class="required">*</em></span>
				<input type="url" bind:value={editUrl} required maxlength="2048" />
			</label>
			<fieldset class="events-field">
				<legend>{m('admin.webhooks.field.events')} <em class="required">*</em></legend>
				{#each WEBHOOK_EVENT_TYPES as evt (evt)}
					<label class="checkbox-line">
						<input
							type="checkbox"
							checked={editEvents.has(evt)}
							onchange={() => toggleEditEvent(evt)}
						/>
						<span class="mono">{evt}</span>
					</label>
				{/each}
			</fieldset>
			<label class="checkbox-line standalone">
				<input type="checkbox" bind:checked={editActive} />
				<span>{m('admin.webhooks.field.active')}</span>
			</label>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={() => (editing = null)}>{m('common.cancel')}</button>
				<button
					type="submit"
					class="btn-primary"
					disabled={!editName.trim() || !editUrl.trim() || editEvents.size === 0 || editSaving}
				>
					{editSaving ? m('admin.webhooks.edit.saving') : m('admin.webhooks.edit.save')}
				</button>
			</div>
		</form>
	{/if}
</Modal>

<style>
	.page-hint {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.85rem;
		max-width: 720px;
	}

	.page-hint code {
		background: var(--surface-2);
		/* Not inherited from .page-hint: --text-muted on --surface-2 is 4.34:1,
		   below the 4.5:1 bar. A code literal is the emphasized token in the
		   sentence anyway, so muted was also backwards. */
		color: var(--text);
		padding: 1px 5px;
		border-radius: 4px;
		font-size: 0.8em;
	}

	.section-heading {
		margin: 0.5rem 0 0.25rem;
		font-size: 1.05rem;
	}

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.state.error {
		color: #f06464;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}

	.url-cell,
	.events-cell {
		max-width: 320px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* A paused subscription's row is de-emphasised by the shared `.row-muted`
	 * recipe in app.css (a muted colour token). It used to be `opacity: 0.6`
	 * on these cells, which composited the row's status pill down to 2.93:1
	 * and any muted cell to 2.77:1 on --surface. */

	/* "A rotation is mid-flight" signal on the secret cell. The pill itself is
	   `<Badge tone="warning">` — amber, matching a `pending` delivery, because
	   it's a transient state and not an error. Left here is only what the
	   caller owns: the placement, and the escape from the surrounding `.mono`
	   cell (a monospace secret prefix is the point of that cell; the pill's
	   prose is not). Font-family inherits into the badge. */
	.overlap-pill-wrap {
		display: inline-flex;
		margin-left: 6px;
		font-family:
			-apple-system,
			BlinkMacSystemFont,
			'Segoe UI',
			Roboto,
			sans-serif;
	}

	.field-hint {
		margin: 0 0 0.5rem;
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	/* Hard cutover is the destructive option — deliveries fail until the
	   receiver holds the new secret — so it gets the red treatment, not amber. */
	.cutover-warning {
		background: rgba(240, 70, 70, 0.12);
		border: 1px solid rgba(240, 70, 70, 0.35);
		color: #f06464;
		border-radius: 8px;
		padding: 0.75rem 1rem;
		font-size: 0.85rem;
		margin: 0.75rem 0 0;
	}

	.overlap-note {
		margin: 0 0 0.75rem;
		color: var(--text-muted);
		font-size: 0.8rem;
	}

	.events-field {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.5rem 0.75rem 0.6rem;
		margin: 0.5rem 0;
	}

	.events-field legend {
		font-size: 0.85rem;
		padding: 0 0.35rem;
	}

	.checkbox-line {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.2rem 0;
		font-size: 0.9rem;
	}

	.checkbox-line.standalone {
		margin-top: 0.5rem;
	}
</style>
