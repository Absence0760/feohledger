<script lang="ts" module>
	/** Date + time, because a schedule's whole point is the time-of-day it
	 *  fires. Locale-driven via `formatDate`. */
	const DATETIME_OPTS: Intl.DateTimeFormatOptions = {
		year: 'numeric',
		month: 'short',
		day: 'numeric',
		hour: 'numeric',
		minute: '2-digit'
	};
</script>

<script lang="ts">
	import { ApiError } from '#lib/api.ts';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import Badge from '#lib/components/ui/Badge.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { createRequestSequencer } from '#lib/utils/requestSequence.ts';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import {
		createScheduledReport,
		deleteScheduledReport,
		listScheduledReports,
		updateScheduledReport
	} from '#lib/api/scheduledReports.ts';
	import type { ScheduledReport, ScheduledReportCreate } from '#lib/types/scheduledReport.ts';
	import {
		cadenceLabelKey,
		droppedRecipientCount,
		healthTone,
		HEALTH_LABEL_KEYS,
		humaniseKey,
		reportTypeLabelKey,
		retryCountFromError,
		scheduleHealth,
		showsRunError,
		AUTO_DISABLE_FAILURE_COUNT
	} from './scheduledReportDisplay';

	/**
	 * Scheduled-report admin panel.
	 *
	 * The runner (`backend/app/services/scheduled_reports.py`) shipped complete
	 * with NO CRUD surface, so a `ScheduledReport` row could only be created by
	 * hand-written SQL and `list_due_schedules` returned `[]` on every tick
	 * forever. This is that surface.
	 *
	 * Self-fetching, like its sibling `ByEntityBreakdown` — it owns one list and
	 * one sequencer, and the host page passes it nothing.
	 *
	 * **No entity selector, deliberately.** `X-Entity-ID` rides every request
	 * from `api.ts` but the scheduled-report routes do not honour it: a schedule
	 * is whole-tenant by construction. There is nothing here for an entity
	 * scope to mean, so offering one would be a lie about what gets emailed.
	 *
	 * **RBAC.** Read is admin + cfo (the host route is gated the same way);
	 * every mutation is admin ONLY, matching the backend, so a CFO sees the
	 * table and none of the create / edit / enable / delete controls rather than
	 * clicking into a 403.
	 *
	 * **Degrading when the endpoints are absent.** A 404 from the LIST call is
	 * unambiguous — a list route cannot 404 on data — so it means the surface
	 * isn't mounted in this deployment. That renders one quiet line instead of
	 * an error state, with no toast: an operator on an older backend should see
	 * "not available here", not a failure they are expected to chase.
	 */

	// The two vocabularies come off the RESPONSE, never a hardcoded list here:
	// they are the runner's own registries, so a report type added on the
	// backend becomes selectable in this form with no frontend change. An
	// unknown key still renders (humanised) rather than silently disappearing.
	let schedules = $state<ScheduledReport[]>([]);
	let reportTypes = $state<string[]>([]);
	let cadences = $state<string[]>([]);

	let loading = $state(true);
	let errored = $state(false);
	let loadError = $state<string | null>(null);
	/** The endpoints are not mounted in this deployment (list returned 404). */
	let unavailable = $state(false);

	const canEdit = $derived(auth.isAdmin);

	// One list, one sequencer. Every mutation re-lists through `load()`, and a
	// create/enable/delete can overlap a refresh, so the counter is what keeps a
	// stale snapshot from clobbering a newer one.
	const fetchSequence = createRequestSequencer();

	let COLUMNS = $derived([
		{ label: m('scheduledReports.col.name') },
		{ label: m('scheduledReports.col.report') },
		{ label: m('scheduledReports.col.cadence') },
		{ label: m('scheduledReports.col.recipients') },
		{ label: m('scheduledReports.col.nextRun') },
		{ label: m('scheduledReports.col.lastRun') },
		{ label: m('scheduledReports.col.status') },
		{ class: 'actions-col' }
	]);

	async function load() {
		const token = fetchSequence.start();
		loading = true;
		try {
			const res = await listScheduledReports();
			if (!fetchSequence.canCommit(token)) return;
			schedules = res.schedules;
			reportTypes = res.report_types;
			cadences = res.cadences;
			errored = false;
			loadError = null;
			unavailable = false;
		} catch (e) {
			// `isCurrentRequest`, not `canCommit`: only the newest request owns
			// the failure state — a stale error must not replace a good table.
			if (!fetchSequence.isCurrentRequest(token)) return;
			if (e instanceof ApiError && e.status === 404) {
				// Not mounted in this deployment. Not an error the reader can act
				// on, so: no toast, no red, no retry button.
				unavailable = true;
				errored = false;
				loadError = null;
				return;
			}
			errored = true;
			loadError = e instanceof Error ? e.message : m('scheduledReports.loadFailed');
		} finally {
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	$effect(() => {
		// `load()` handles every rejection itself, so there is no fire-and-forget
		// promise to swallow here.
		load();
	});

	// ── Display ──────────────────────────────────────────────────────────────
	// `m()` is read inside these so labels re-render on a locale switch. The
	// humanised fallback keeps an unknown backend key visible.
	function typeLabel(type: string): string {
		const key = reportTypeLabelKey(type);
		return key ? m(key) : humaniseKey(type);
	}

	function cadenceLabel(cadence: string): string {
		const key = cadenceLabelKey(cadence);
		return key ? m(key) : humaniseKey(cadence);
	}

	// ── Create / edit form ───────────────────────────────────────────────────
	let editing = $state<ScheduledReport | null>(null);
	let creating = $state(false);
	let saving = $state(false);

	let fName = $state('');
	let fType = $state('');
	let fCadence = $state('');
	let fRecipients = $state('');
	let fPeriodDays = $state(30);
	let fEnabled = $state(true);
	let fNextRun = $state('');
	/** What `fNextRun` was seeded with, so an untouched field is never re-sent
	 *  (a `datetime-local` is minute-precision — echoing it back would silently
	 *  shift a stored second-precision instant on every save). */
	let nextRunSeed = $state('');

	const formOpen = $derived(creating || editing !== null);

	/** Split on newline / comma / semicolon. Deliberately does NOT de-duplicate:
	 *  the backend owns that (case-insensitively), and doing it here too would
	 *  hide the fact that the saved list differs from the typed one. */
	function parseRecipients(raw: string): string[] {
		return raw
			.split(/[\n,;]+/)
			.map((s) => s.trim())
			.filter(Boolean);
	}

	/** ISO instant → the `YYYY-MM-DDTHH:mm` a `datetime-local` input wants, in
	 *  the reader's own timezone. */
	function toLocalInput(iso: string | null): string {
		if (!iso) return '';
		const d = new Date(iso);
		if (Number.isNaN(d.getTime())) return '';
		const pad = (n: number) => String(n).padStart(2, '0');
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}

	function openCreate() {
		confirmDeleteId = null;
		fName = '';
		fType = reportTypes[0] ?? '';
		fCadence = cadences[0] ?? '';
		fRecipients = '';
		fPeriodDays = 30;
		fEnabled = true;
		fNextRun = '';
		nextRunSeed = '';
		editing = null;
		creating = true;
	}

	function openEdit(s: ScheduledReport) {
		// Un-arm a pending Delete — the window-click handler ignores clicks
		// inside `.row-action`, and leaving Delete armed behind a dialog is a
		// loaded gun.
		confirmDeleteId = null;
		fName = s.name;
		fType = s.report_type;
		fCadence = s.cadence;
		// Rendered from the row, i.e. from the server's SAVED list — never from
		// whatever was typed last time, which may have carried duplicates.
		fRecipients = s.recipients.join('\n');
		fPeriodDays = s.period_days;
		fEnabled = s.enabled;
		fNextRun = toLocalInput(s.next_run_at);
		nextRunSeed = fNextRun;
		creating = false;
		editing = s;
	}

	function closeForm() {
		if (saving) return;
		creating = false;
		editing = null;
	}

	const formValid = $derived(
		fName.trim().length > 0 &&
			fType.length > 0 &&
			fCadence.length > 0 &&
			parseRecipients(fRecipients).length > 0
	);

	/**
	 * Replace one row from a server response.
	 *
	 * This is the whole point of point (1): the backend de-dupes recipients
	 * case-insensitively instead of rejecting them, so re-rendering the list the
	 * user TYPED would show a list that was never saved. `load()` re-lists right
	 * after anyway; the in-place write just closes the window where the stale
	 * row is on screen.
	 */
	function upsert(saved: ScheduledReport) {
		fetchSequence.supersedeInFlight();
		const at = schedules.findIndex((s) => s.id === saved.id);
		if (at === -1) schedules = [saved, ...schedules];
		else schedules = schedules.map((s) => (s.id === saved.id ? saved : s));
	}

	/** Say so when the backend removed duplicates, so a shortened list doesn't
	 *  read as data loss. */
	function reportDedupe(submitted: string[], saved: ScheduledReport) {
		const dropped = droppedRecipientCount(submitted, saved.recipients);
		if (dropped > 0) toast(m('scheduledReports.toast.deduped', { n: dropped }), 'info');
	}

	async function handleSubmit() {
		const recipients = parseRecipients(fRecipients);
		if (!formValid) return;
		saving = true;
		try {
			// An empty "first run" means "now", i.e. it fires on the next runner
			// tick — the same thing omitting it means on create. On an edit the
			// field is only sent when the reader actually changed it (see
			// `nextRunSeed`); clearing it counts as a change and re-pins to now.
			const nextRunIso =
				fNextRun.trim() !== ''
					? new Date(fNextRun).toISOString()
					: editing
						? new Date().toISOString()
						: undefined;
			const changedNextRun = fNextRun !== nextRunSeed;

			let saved: ScheduledReport;
			if (editing) {
				saved = await updateScheduledReport(editing.id, {
					name: fName.trim(),
					report_type: fType,
					cadence: fCadence,
					recipients,
					period_days: fPeriodDays,
					enabled: fEnabled,
					...(changedNextRun ? { next_run_at: nextRunIso } : {})
				});
			} else {
				const body: ScheduledReportCreate = {
					name: fName.trim(),
					report_type: fType,
					cadence: fCadence,
					recipients,
					period_days: fPeriodDays,
					enabled: fEnabled,
					...(nextRunIso ? { next_run_at: nextRunIso } : {})
				};
				saved = await createScheduledReport(body);
			}
			const wasCreate = editing === null;
			creating = false;
			editing = null;
			upsert(saved);
			reportDedupe(recipients, saved);
			toast(
				m(wasCreate ? 'scheduledReports.toast.created' : 'scheduledReports.toast.updated'),
				'success'
			);
			await load();
		} catch (e) {
			// `api.ts` already ran the FastAPI `detail` through `formatApiDetail`,
			// which renders a 422 as `field: msg` off `loc` + `msg` ONLY — it
			// never reads `input`, which on a recipients failure is the submitted
			// address list. So this surfaces the backend's own wording verbatim
			// and cannot leak an address into a toast.
			toast(
				e instanceof Error
					? e.message
					: m(editing ? 'scheduledReports.toast.updateFailed' : 'scheduledReports.toast.createFailed'),
				'error'
			);
		} finally {
			saving = false;
		}
	}

	// ── Enable / pause ───────────────────────────────────────────────────────
	let busyId = $state<string | null>(null);

	/**
	 * `PATCH {enabled: true}` re-enables AND clears `last_run_status` /
	 * `last_run_error`, so recovering an auto-disabled schedule is ONE call —
	 * there is no second "clear the failure" request to forget.
	 */
	async function setEnabled(s: ScheduledReport, enabled: boolean) {
		confirmDeleteId = null;
		busyId = s.id;
		try {
			const saved = await updateScheduledReport(s.id, { enabled });
			upsert(saved);
			toast(
				m(enabled ? 'scheduledReports.toast.enabled' : 'scheduledReports.toast.paused'),
				'success'
			);
			await load();
		} catch (e) {
			toast(
				e instanceof Error ? e.message : m('scheduledReports.toast.updateFailed'),
				'error'
			);
		} finally {
			busyId = null;
		}
	}

	// ── Delete (armed two-click) ─────────────────────────────────────────────
	let confirmDeleteId = $state<string | null>(null);

	function handleWindowClick(e: MouseEvent) {
		if (confirmDeleteId && !(e.target as HTMLElement).closest('.row-action')) {
			confirmDeleteId = null;
		}
	}

	async function handleDelete(id: string) {
		busyId = id;
		try {
			await deleteScheduledReport(id);
			toast(m('scheduledReports.toast.deleted'), 'success');
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('scheduledReports.toast.deleteFailed'), 'error');
		} finally {
			confirmDeleteId = null;
			busyId = null;
		}
	}
</script>

<svelte:window onclick={handleWindowClick} />

<section class="chart-card" aria-labelledby="scheduled-reports-heading" data-testid="scheduled-reports">
	<div class="sr-head">
		<h2 id="scheduled-reports-heading">{m('scheduledReports.heading')}</h2>
		{#if canEdit && !unavailable}
			<button class="btn-primary" onclick={openCreate} data-testid="new-schedule">
				{m('scheduledReports.new')}
			</button>
		{/if}
	</div>
	<p class="sr-hint">{m('scheduledReports.hint')}</p>

	{#if unavailable}
		<!-- Quiet, not red: an older backend without these routes is a
		     deployment fact, not a failure the reader can act on. -->
		<p class="sr-unavailable" data-testid="scheduled-reports-unavailable">
			{m('scheduledReports.empty.unavailable')}
		</p>
	{:else}
		{#if errored}
			<p class="sr-error" role="alert" data-testid="scheduled-reports-error">
				{loadError}
				<button type="button" class="btn-cancel" onclick={load}>{m('scheduledReports.retry')}</button>
			</p>
		{/if}
		<DataTable
			columns={COLUMNS}
			isEmpty={schedules.length === 0}
			empty={loading
				? m('common.loading')
				: errored
					? m('scheduledReports.empty.errored')
					: m('scheduledReports.empty.none')}
		>
			{#snippet body()}
				{#each schedules as s (s.id)}
					{@const health = scheduleHealth(s)}
					{@const retries = retryCountFromError(s.last_run_error)}
					<tr
						class:clickable={canEdit}
						class:sr-auto-disabled={health === 'auto_disabled'}
						data-health={health}
						onclick={(e) => {
							if (canEdit && isRowOpenClick(e)) openEdit(s);
						}}
					>
						<td>
							{#if canEdit}
								<RowLink
									onclick={() => openEdit(s)}
									ariaLabel={m('scheduledReports.editAria', { name: s.name })}
								>
									{s.name}
								</RowLink>
							{:else}
								{s.name}
							{/if}
						</td>
						<td>
							{typeLabel(s.report_type)}
							<span class="sr-sub">{m('scheduledReports.periodDays', { n: s.period_days })}</span>
						</td>
						<td>{cadenceLabel(s.cadence)}</td>
						<td>
							<!-- Rendered from the server's SAVED list, which the backend
							     de-duped case-insensitively — not from what was typed. -->
							<span title={s.recipients.join(', ')}>
								{m('scheduledReports.recipientCount', { n: s.recipients.length })}
							</span>
						</td>
						<td>{formatDate(s.next_run_at, m('scheduledReports.never'), DATETIME_OPTS)}</td>
						<td>
							{formatDate(s.last_run_at, m('scheduledReports.never'), DATETIME_OPTS)}
							{#if showsRunError(health) && s.last_run_error}
								<!-- Counts + an exception class only — the backend never puts
								     a recipient address in here. For a `partial` run this is
								     the only place that says how many did get it. -->
								<span class="sr-run-error" data-testid="run-error">{s.last_run_error}</span>
							{/if}
						</td>
						<td>
							<Badge tone={healthTone(health)} variant={health}>
								{m(HEALTH_LABEL_KEYS[health])}
							</Badge>
							{#if health === 'auto_disabled'}
								<!-- The failure this panel exists to make visible: it stopped
								     emailing and nobody decided that. Distinct from a paused
								     schedule in colour, in wording, and by carrying its own
								     explanation. -->
								<span class="sr-auto-note" data-testid="auto-disabled-note">
									{m('scheduledReports.autoDisabledNotice', {
										n: retries ?? AUTO_DISABLE_FAILURE_COUNT
									})}
								</span>
							{/if}
						</td>
						<td class="actions">
							{#if canEdit}
								{#if s.enabled}
									<RowAction
										disabled={busyId === s.id}
										ariaLabel={m('scheduledReports.pauseAria', { name: s.name })}
										onclick={(e) => {
											e.stopPropagation();
											setEnabled(s, false);
										}}
									>
										{m('scheduledReports.row.pause')}
									</RowAction>
								{:else}
									<RowAction
										variant={health === 'auto_disabled' ? 'accent' : 'default'}
										disabled={busyId === s.id}
										ariaLabel={m(
											health === 'auto_disabled'
												? 'scheduledReports.reEnableAria'
												: 'scheduledReports.enableAria',
											{ name: s.name }
										)}
										onclick={(e) => {
											e.stopPropagation();
											setEnabled(s, true);
										}}
									>
										{m(
											health === 'auto_disabled'
												? 'scheduledReports.row.reEnable'
												: 'scheduledReports.row.enable'
										)}
									</RowAction>
								{/if}
								<RowAction
									variant="danger"
									armed={confirmDeleteId === s.id}
									disabled={busyId === s.id}
									ariaLabel={m('scheduledReports.deleteAria', { name: s.name })}
									onclick={(e) => {
										e.stopPropagation();
										if (confirmDeleteId === s.id) handleDelete(s.id);
										else confirmDeleteId = s.id;
									}}
								>
									{confirmDeleteId === s.id
										? m('scheduledReports.row.confirm')
										: m('scheduledReports.row.delete')}
								</RowAction>
							{/if}
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</section>

<Modal
	open={formOpen}
	ariaLabel={editing ? m('scheduledReports.modal.editAria') : m('scheduledReports.modal.createAria')}
	title={editing ? m('scheduledReports.modal.editTitle') : m('scheduledReports.modal.createTitle')}
	width="md"
	onclose={closeForm}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			handleSubmit();
		}}
	>
		<label>
			<span>{m('scheduledReports.field.name')} <em class="required">*</em></span>
			<input
				type="text"
				bind:value={fName}
				required
				maxlength="120"
				placeholder={m('scheduledReports.field.namePlaceholder')}
			/>
		</label>
		<label>
			<span>{m('scheduledReports.field.reportType')} <em class="required">*</em></span>
			<!-- Options come from the list response, so a report type the backend
			     gains appears here with no frontend change. -->
			<select bind:value={fType} required>
				{#each reportTypes as t (t)}
					<option value={t}>{typeLabel(t)}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>{m('scheduledReports.field.cadence')} <em class="required">*</em></span>
			<select bind:value={fCadence} required>
				{#each cadences as c (c)}
					<option value={c}>{cadenceLabel(c)}</option>
				{/each}
			</select>
		</label>
		<label>
			<span>{m('scheduledReports.field.recipients')} <em class="required">*</em></span>
			<textarea
				rows="4"
				bind:value={fRecipients}
				placeholder={m('scheduledReports.field.recipientsPlaceholder')}
				aria-describedby="sr-hint-recipients"
			></textarea>
			<!-- `<small>`, not `<span>`: the global `.modal label > span` rule is
			     the uppercase field CAPTION, and it out-specifies a scoped
			     override — a hint written as a span would render shouting.

			     `aria-describedby`, not a bare child: a `<small>` inside the
			     `<label>` is folded into the control's accessible NAME, so a
			     screen reader announced the whole sentence as the field's name
			     and two fields stopped being distinguishable by it (the Period
			     hint contains the word "report", which collided with the Report
			     select). A hint is a DESCRIPTION — announced after the name, and
			     skippable. aria-hidden with aria-describedby, not one or the other: a node inside
			     the `<label>` is part of the control's accessible NAME, and
			     `aria-describedby` only ADDS a description — it does not remove the
			     hint from the name. `aria-hidden` drops it from the name computation
			     while `aria-describedby` still resolves its text, so the field is
			     announced as "Period (days)" and the hint follows as a description. -->
			<small id="sr-hint-recipients" class="sr-field-hint" aria-hidden="true">
				{m('scheduledReports.field.recipientsHint')}
			</small>
		</label>
		<label>
			<span>{m('scheduledReports.field.periodDays')}</span>
			<input
				type="number"
				min="1"
				max="366"
				bind:value={fPeriodDays}
				aria-describedby="sr-hint-period"
			/>
			<small id="sr-hint-period" class="sr-field-hint" aria-hidden="true">
				{m('scheduledReports.field.periodDaysHint')}
			</small>
		</label>
		<label>
			<span>{m('scheduledReports.field.nextRun')}</span>
			<input
				type="datetime-local"
				bind:value={fNextRun}
				aria-describedby="sr-hint-next-run"
			/>
			<small id="sr-hint-next-run" class="sr-field-hint" aria-hidden="true">
				{m('scheduledReports.field.nextRunHint')}
			</small>
		</label>
		<label class="sr-checkbox">
			<input type="checkbox" bind:checked={fEnabled} />
			<span>{m('scheduledReports.field.enabled')}</span>
		</label>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={closeForm}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={!formValid || saving}>
				{saving
					? m(editing ? 'common.saving' : 'scheduledReports.modal.creating')
					: m(editing ? 'common.save' : 'scheduledReports.modal.create')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.sr-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 4px;
	}
	.sr-head h2 {
		font-size: 1rem;
		margin: 0;
	}
	.sr-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 16px;
	}
	.sr-unavailable {
		color: var(--text-muted);
		font-size: 0.88rem;
		margin: 0;
		padding: 16px 0;
	}
	.sr-error {
		color: var(--danger);
		font-weight: 500;
		font-size: 0.88rem;
		display: flex;
		align-items: center;
		gap: 10px;
		margin: 0 0 12px;
	}
	.sr-sub,
	.sr-run-error,
	.sr-auto-note {
		display: block;
		font-size: 0.75rem;
		color: var(--text-muted);
	}
	/* The runner's own message about a partial / failed send. Muted, because
	   the Status badge already carries the signal — this is the detail. */
	.sr-run-error {
		max-width: 30ch;
		overflow-wrap: anywhere;
	}
	.sr-auto-note {
		color: var(--danger);
		max-width: 34ch;
		margin-top: 4px;
	}
	/* A schedule that stopped emailing by itself gets a left rule as well as a
	   danger badge — a paused one (neutral chip, no rule) can't be mistaken
	   for it at a glance. */
	.sr-auto-disabled td:first-child {
		box-shadow: inset 3px 0 0 var(--danger);
	}
	.sr-field-hint {
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	/* The global `.modal label` is a column with an uppercase caption; a
	   checkbox wants its label beside the box instead. */
	.sr-checkbox {
		flex-direction: row;
		align-items: center;
		gap: 8px;
	}
	.sr-checkbox > span {
		text-transform: none;
		letter-spacing: normal;
	}
</style>
