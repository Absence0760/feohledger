<script lang="ts">
	import { focusTrap } from '$lib/actions/focusTrap';
	import type { Invoice, AuditSummary } from '$lib/types/invoice';
	import { INVOICE_STATUSES, INVOICE_STATUS_LABEL_KEYS } from '$lib/types/invoice';
	import { formatMoney, isNegativeAmount, isPositiveAmount } from '$lib/utils/money';
	import { invoiceStore } from '$lib/stores/invoices.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { adminStore } from '$lib/stores/admin.svelte';
	import { api, ApiError } from '$lib/api';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import RowAction from '$lib/components/ui/RowAction.svelte';
	import Money from '$lib/components/ui/Money.svelte';
	import Badge, { type BadgeTone } from '$lib/components/ui/Badge.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { invoiceWarningText } from '$lib/api/invoiceWarnings';
	import { formatDate } from '$lib/utils/time';
	import { formatList } from '$lib/utils/list';
	import type { MessageKey } from '$lib/i18n/messages';
	import type { ActiveSteps } from '$lib/stores/workflows.svelte';

	import type { AuditEntry, AuditFieldChange } from '$lib/types/audit';
	import { getInvoiceAuditLog } from '$lib/api/audit';
	import {
		getInvoiceSuggestions,
		type FieldSuggestion,
	} from '$lib/api/enrichment';
	import { routeIntercompany } from '$lib/api/invoices';
	import { listGlAccounts } from '$lib/api/glAccounts';
	import { glAccountOptionLabel, type GlAccountOption } from '$lib/types/glAccount';
	import { entityStore } from '$lib/stores/entity.svelte';

	/**
	 * Badge tone per quality-inspection verdict (the 4-way match's gate).
	 *
	 * `missing` is not an `inspection_result` — it is the "required but never
	 * recorded" case the markup renders separately — and it shares `danger`
	 * with `fail` because it has the same consequence: the 4-way match cannot
	 * be satisfied. That was already true of the CSS these tones replace (one
	 * rule covered `.fail, .missing`), so no distinction collapses here.
	 */
	const INSPECTION_TONES: Record<'pass' | 'fail' | 'partial', BadgeTone> = {
		pass: 'success',
		fail: 'danger',
		partial: 'warning'
	};
	import type { WorkflowInstanceDetail } from '$lib/types/workflowInstance';
	import { chainIsComplete, chainLevelRemaining } from '$lib/types/workflowInstance';
	import { getInvoiceWorkflowInstance } from '$lib/api/workflowInstance';
	import ApprovalChainProgress from '$lib/components/ui/ApprovalChainProgress.svelte';

	import SupplierChatThread from '$lib/components/chat/SupplierChatThread.svelte';
	import type { ChatThread, ChatTemplate } from '$lib/types/supplierChat';
	import {
		getChatThread,
		postChatMessage,
		uploadChatAttachment,
		resolveChatThread,
		reopenChatThread,
		getChatTemplates,
		getChatMentionableUsers,
		type ChatMentionCandidate,
	} from '$lib/api/supplierChat';

	import {
		E_INVOICE_FORMATS,
		downloadEInvoice,
		einvoiceFilename,
		listPeppolTransmissions,
		livePeppolTransmission,
		parseEInvoiceIssues,
		sendInvoiceOverPeppol,
		type EInvoiceFormat,
		type EInvoiceValidationIssue,
		type PeppolSendResult,
		type PeppolTransmissionSummary,
	} from '$lib/api/einvoice';
	import EInvoiceIssueList from '$lib/components/EInvoiceIssueList.svelte';

	/*
	 * There is still no HAND-WRITTEN code→prose map here, and there never will
	 * be: the one §95 deleted covered 4 codes out of dozens and drifted. The
	 * refusal rows are localized again, through a map GENERATED from the
	 * backend's own rule set — see `EInvoiceIssueList.svelte` and
	 * `api/einvoiceRuleMessages.generated.ts`. The wording lives in the locale
	 * catalogues, the mapping is derived, and CI fails when the validator gains
	 * a code the catalogue has no key for.
	 */

	/** PEPPOL failure codes the send route returns as a bare token (everything
	 *  else — a malformed participant id, an unconfigured sender — arrives as
	 *  the backend's own prose and is rendered verbatim). */
	const PEPPOL_ERROR_KEYS: Record<string, MessageKey> = {
		invoice_not_approved: 'invoices.modal.peppol.error.notApproved',
		receiver_not_registered: 'invoices.modal.peppol.error.receiverNotRegistered',
		receiver_doctype_unsupported: 'invoices.modal.peppol.error.receiverDoctypeUnsupported',
		peppol_provider_not_configured: 'invoices.modal.peppol.error.providerNotConfigured',
	};

	/** Mirrors `_PEPPOL_SENDABLE_STATUSES` in `backend/app/api/invoices.py`:
	 *  `approved` plus every post-approval state. Transmission is a
	 *  compliance-significant outbound, gated exactly like the ERP send — so
	 *  offering the control before approval could only ever 422. */
	const PEPPOL_SENDABLE_STATUSES = new Set([
		'approved',
		'sending_to_erp',
		'sent_to_erp',
		'posted_in_erp',
		'payment_scheduled',
		'paid',
		'done',
	]);

	// Render-side helper: extract the per-field before/after diff (SOX change
	// history) the backend writes onto details.changes for edit/approve events.
	function fieldChanges(entry: AuditEntry): [string, AuditFieldChange][] {
		const changes = entry.details?.changes;
		if (!changes || typeof changes !== 'object') return [];
		return Object.entries(changes as Record<string, AuditFieldChange>);
	}

	// Field + action labels are looked up reactively (via m()) so they switch
	// with the active locale. The maps below carry the i18n key per code value;
	// an unknown value falls back to the raw value (mirrors the prior ?? raw).
	const FIELD_LABEL_KEYS: Record<string, MessageKey> = {
		vendor_name: 'invoices.modal.field.vendorName',
		amount: 'invoices.modal.field.amountLabel',
		invoice_number: 'invoices.modal.field.invoiceNumberLabel',
		invoice_date: 'invoices.modal.field.invoiceDate',
		due_date: 'invoices.modal.field.dueDateLabel',
		gl_account: 'invoices.modal.field.glAccountLabel'
	};
	function fieldLabel(field: string): string {
		const key = FIELD_LABEL_KEYS[field];
		return key ? m(key) : field;
	}

	const ACTION_LABEL_KEYS: Record<string, MessageKey> = {
		'invoice.uploaded': 'invoices.modal.action.uploaded',
		'invoice.submitted_for_review': 'invoices.modal.action.submittedForReview',
		'invoice.approved': 'invoices.modal.action.approved',
		'invoice.rejected': 'invoices.modal.action.rejected',
		'invoice.resubmitted': 'invoices.modal.action.resubmitted',
		'invoice.assigned_for_review': 'invoices.modal.action.assignedForReview',
		'invoice.erp_submitted': 'invoices.modal.action.erpSubmitted',
		'invoice.extraction_dispatched': 'invoices.modal.action.extractionStarted',
		'invoice.extraction_reset': 'invoices.modal.action.extractionReset',
		'invoice.extraction_triggered': 'invoices.modal.action.extractionTriggered',
		'invoice.extraction_completed': 'invoices.modal.action.extractionCompleted',
		'invoice.extraction_failed': 'invoices.modal.action.extractionFailed',
		'invoice.completed': 'invoices.modal.action.markedComplete',
		'invoice.edited': 'invoices.modal.action.editedFields',
		'invoice.file_attached': 'invoices.modal.action.fileAttached',
		'invoice.file_replaced': 'invoices.modal.action.fileReplaced',
		'invoice.file_deleted': 'invoices.modal.action.fileDeleted',
		'audit.viewed': 'invoices.modal.action.auditViewed',
		'audit.exported': 'invoices.modal.action.auditExported',
		// Exception-queue events. They are correlation-keyed to the invoice
		// (backend services/exception_lifecycle), so they arrive in this same
		// timeline — a raised duplicate / fraud flag is why the invoice stalled,
		// and clearing it is what let it move.
		'exception.raised': 'invoices.modal.action.exceptionRaised',
		'exception.resolved': 'invoices.modal.action.exceptionResolved',
		'exception.escalated': 'invoices.modal.action.exceptionEscalated',
		'exception.dismissed': 'invoices.modal.action.exceptionDismissed',
		'exception.assigned': 'invoices.modal.action.exceptionAssigned',
		'chat_message_posted': 'invoices.modal.action.chatPosted',
		'chat_thread_resolved': 'invoices.modal.action.chatResolved',
		'chat_thread_reopened': 'invoices.modal.action.chatReopened',
	};
	function actionLabel(action: string): string {
		const key = ACTION_LABEL_KEYS[action];
		return key ? m(key) : action;
	}

	let {
		invoice,
		onclose,
		onrefresh,
		activeSteps,
	}: {
		invoice: Invoice;
		onclose: () => void;
		/** Refresh the host's invoice list. Required, not optional: the host
		 *  owns the active filters, and the store's own list loader takes no
		 *  params — calling it from here replaces the host's filtered page with
		 *  an unfiltered one and resets `lastParams`, so Load-more then
		 *  paginates a different set. See `refreshList` below. */
		onrefresh: () => void | Promise<void>;
		activeSteps: ActiveSteps;
	} = $props();

	/**
	 * Ask the host to refresh its list — with ITS filters.
	 *
	 * Nothing in this modal may reload the list through `invoiceStore` directly.
	 * The store's loader takes no params, so it silently widens the host's
	 * filtered list to every status while the status chips still claim a filter
	 * (the hazard `handleApprove` already documents), and it resets the store's
	 * `lastParams` so the next Load-more pages through the wrong result set.
	 */
	async function refreshList() {
		await onrefresh();
	}

	/* eslint-disable svelte/state-referenced-locally -- modal receives a snapshot, intentional */
	let vendor = $state(invoice.vendor);
	let invoice_number = $state(invoice.invoice_number);
	let amount = $state(invoice.amount);
	let due_date = $state(invoice.due_date);
	let status = $state(invoice.status);
	let currentFileUrl = $state(invoice.file_url);
	let po_number = $state(invoice.po_number);
	let description = $state(invoice.description);
	let vendor_address = $state(invoice.vendor_address ?? '');
	let vendor_tax_id = $state(invoice.vendor_tax_id ?? '');
	let ship_to_address = $state(invoice.ship_to_address ?? '');
	let tax_rate = $state(invoice.tax_rate);
	let payment_method = $state(invoice.payment_method ?? '');
	let gl_account = $state(invoice.gl_account ?? '');
	let cost_center = $state(invoice.cost_center ?? '');
	let payment_terms = $state(invoice.payment_terms ?? '');
	let department = $state(invoice.department ?? '');
	let project = $state(invoice.project ?? '');

	let glAccounts = $state<GlAccountOption[]>([]);

	$effect(() => {
		loadGLAccounts();
		// The entity names behind each option's `entity_id`, for the scope
		// suffix `glAccountOptionLabel` appends in the consolidated view.
		entityStore.ensureLoaded();
	});

	async function loadGLAccounts() {
		try {
			glAccounts = await listGlAccounts();
		} catch { /* non-critical */ }
	}

	/**
	 * One option's label. The bound VALUE stays `acct.code` at both GL sites
	 * below, and must: `Invoice.gl_account` / `InvoiceLineItem.gl_account` are
	 * `String(100)` columns holding the code, not FKs — unlike
	 * `Expense.gl_account_id` and the two procurement `gl_account_id` columns,
	 * which is why the expense / requisition / catalog pickers bind `g.id` and
	 * these two do not. Budget matching, the report builder, the PO-match
	 * commodity resolver, approval routing, the 1099 box map and the vendor GL
	 * priors all read that string as the code (`types/glAccount.ts` has the
	 * list). What the code cannot say on its own is WHICH subsidiary's `6000`
	 * it is when the consolidated view offers several — so the label says it.
	 */
	function glLabel(acct: GlAccountOption, withName: boolean): string {
		return glAccountOptionLabel(acct, entityStore, {
			withName,
			unknownEntity: m('glAccounts.scope.unknownEntity')
		});
	}

	/* ---------------------------------------------------------------------
	 * Advisory coding suggestions (GET /api/enrichment/invoices/{id}/suggestions)
	 *
	 * Deterministic auto-fill derived from THIS vendor's approved history — the
	 * dominant historical GL account / cost center / payment terms. Advisory
	 * means the user applies it: nothing here writes to the invoice, and even
	 * "Apply" only fills the form field, leaving the existing Save (and its
	 * audit row + optimistic-concurrency check) as the only writer.
	 *
	 * The backend suppresses a suggestion for any field the invoice already
	 * populates, so an applied suggestion can never overwrite a human's coding
	 * — but we drop the row locally on apply anyway, because the response is a
	 * snapshot and re-offering a value already sitting in the field is noise.
	 * ------------------------------------------------------------------- */
	let codingSuggestions = $state<FieldSuggestion[]>([]);
	let appliedSuggestionFields = $state<string[]>([]);

	/** Form fields a suggestion can be applied into. `payment_terms` is on this
	 *  list because the modal now edits it — a suggestion with nowhere to land
	 *  would be advice the user cannot take. */
	const SUGGESTION_FIELD_LABEL_KEYS: Record<string, MessageKey> = {
		gl_account: 'invoices.modal.field.glAccount',
		cost_center: 'invoices.modal.field.costCenter',
		payment_terms: 'invoices.modal.field.paymentTerms',
	};
	function suggestionFieldLabel(field: string): string {
		const key = SUGGESTION_FIELD_LABEL_KEYS[field];
		return key ? m(key) : field;
	}

	function applySuggestion(s: FieldSuggestion) {
		if (s.field === 'gl_account') gl_account = s.value;
		else if (s.field === 'cost_center') cost_center = s.value;
		else if (s.field === 'payment_terms') payment_terms = s.value;
		else return; // Unknown field — nothing to fill; leave the row in place.
		appliedSuggestionFields = [...appliedSuggestionFields, s.field];
		codingSuggestions = codingSuggestions.filter((x) => x.field !== s.field);
	}

	$effect(() => {
		const id = invoice.id;
		getInvoiceSuggestions(id)
			.then((res) => {
				// Only fields this form can actually fill. A suggestion for a
				// field with no input would be advice with no way to act on it.
				codingSuggestions = res.field_suggestions.filter(
					(x) => x.field in SUGGESTION_FIELD_LABEL_KEYS
				);
			})
			.catch(() => {
				// Advisory: a failed read is simply no panel, never an error the
				// user has to dismiss on top of the invoice they came to work on.
				codingSuggestions = [];
			});
	});

	/* ---------------------------------------------------------------------
	 * Inter-company routing (POST /api/invoices/{id}/route-intercompany)
	 *
	 * Generates the mirror PAYABLE under another entity of this same tenant, so
	 * it is a two-step confirm whose second label names the counterparty — the
	 * action creates a liability under books the user may not be looking at.
	 *
	 * Gated three ways, all of them real:
	 *   - role: admin / ap_manager, exactly what the router's require_roles says;
	 *   - tenancy: `entityStore.multiEntity` (>1 entity), the SAME signal the
	 *     sidebar switcher renders on — a single-entity tenant has no possible
	 *     counterparty, and the endpoint could only ever 400 ("an entity cannot
	 *     bill itself");
	 *   - state: once `intercompany_mirror_id` is set the pairing is settled.
	 *     The backend stamps a counterparty only while unrouted and returns the
	 *     SAME mirror on a repeat call, so re-offering the action would be a
	 *     control that cannot change anything. Show the routed state instead.
	 * ------------------------------------------------------------------- */
	const canRouteIntercompany = $derived(auth.isManager);
	let mirrorInvoiceId = $state<string | null>(invoice.intercompany_mirror_id);
	let counterpartyEntityId = $state<string | null>(invoice.counterparty_entity_id);
	let selectedCounterparty = $state('');
	let icArmed = $state(false);
	let icBusy = $state(false);
	let icError = $state('');

	$effect(() => {
		if (canRouteIntercompany) entityStore.ensureLoaded();
	});

	/**
	 * Candidate counterparties: this tenant's other ACTIVE entities.
	 *
	 * When the entity switcher has a specific entity selected, that IS this
	 * invoice's entity (the list it was opened from was scoped by the same
	 * `X-Entity-ID`), so it is excluded — a subsidiary cannot bill itself. Under
	 * the consolidated "All entities" view the invoice's own entity is not
	 * knowable client-side (`InvoiceResponse` carries no `entity_id`), so every
	 * active entity is offered and the backend's own 400 is surfaced verbatim
	 * rather than guessed at here.
	 */
	let counterpartyOptions = $derived(
		entityStore.entities.filter(
			(e) => e.is_active && e.id !== entityStore.selected?.id
		)
	);
	function entityName(id: string | null): string {
		if (!id) return '';
		return entityStore.entities.find((e) => e.id === id)?.name ?? id;
	}

	async function handleRouteIntercompany() {
		if (!selectedCounterparty) return;
		if (!icArmed) {
			icArmed = true;
			return;
		}
		icBusy = true;
		icError = '';
		try {
			// The response is the MIRROR, not the origin — the origin's own
			// `intercompany_mirror_id` was set by the same call, so the routed
			// state is recorded from the mirror's id and the host list refreshed.
			const mirror = await routeIntercompany(invoice.id, selectedCounterparty);
			mirrorInvoiceId = mirror.id;
			counterpartyEntityId = selectedCounterparty;
			icArmed = false;
			toast(
				m('invoices.modal.intercompany.routedToast', {
					entity: entityName(selectedCounterparty),
				}),
				'success'
			);
			await refreshList();
			await loadAuditLog();
		} catch (e) {
			// The backend's detail is PII-free and specific (self-billing,
			// unknown entity, already routed) — render it rather than inventing
			// copy that would be less accurate.
			icError = e instanceof ApiError ? e.message : m('invoices.modal.intercompany.failed');
			icArmed = false;
		} finally {
			icBusy = false;
		}
	}
	let reference_number = $state(invoice.reference_number ?? '');
	/* eslint-enable svelte/state-referenced-locally */

	let fullscreen = $state(false);
	let formPaneWidth = $state(480);
	let resizing = $state(false);

	// Lock the background page from scrolling while the modal is open, so a
	// wheel event over the PDF pane (which has no internal scroll) can't bleed
	// through to the invoice grid behind the backdrop. Restored on close.
	$effect(() => {
		const prev = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		return () => {
			document.body.style.overflow = prev;
		};
	});

	function toggleFullscreen() {
		fullscreen = !fullscreen;
		formPaneWidth = fullscreen ? 480 : 600;
	}

	let resizeOverlay: HTMLDivElement | undefined;

	function startResize(e: MouseEvent) {
		if (e.button !== 0) return;
		e.preventDefault();
		e.stopPropagation();
		resizing = true;
		const startX = e.clientX;
		const startWidth = formPaneWidth;

		// Create a full-screen transparent overlay to capture all mouse events
		// This prevents the PDF iframe from stealing them
		const overlay = document.createElement('div');
		overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;cursor:col-resize;';
		document.body.appendChild(overlay);
		resizeOverlay = overlay;

		function onMove(ev: MouseEvent) {
			const delta = startX - ev.clientX;
			formPaneWidth = Math.max(300, Math.min(1200, startWidth + delta));
		}

		function onUp() {
			resizing = false;
			overlay.remove();
			resizeOverlay = undefined;
			window.removeEventListener('mousemove', onMove);
			window.removeEventListener('mouseup', onUp);
		}

		window.addEventListener('mousemove', onMove);
		window.addEventListener('mouseup', onUp);
	}

	let saving = $state(false);
	let submitting = $state(false);
	let deleting = $state(false);
	let confirmDelete = $state(false);
	let reviewing = $state(false);
	let uploadingFile = $state(false);
	let confirmDeleteFile = $state(false);
	let deletingFile = $state(false);
	let fileInput: HTMLInputElement | undefined = $state();
	let showRejectForm = $state(false);
	let rejectReason = $state('');
	let selectedApproverId = $state('');

	// Whether to show the approver picker on submit
	let needsApproverSelect = $derived(
		status === 'new' &&
		activeSteps.approval &&
		activeSteps.approval_config?.approver_strategy === 'manual'
	);

	/**
	 * Load the people this invoice can be assigned to for review.
	 *
	 * `GET /api/invoices/assignable-reviewers` is the ONLY source. The picker
	 * used to fall back to `GET /api/admin/users`, which is admin-only: for
	 * every other role that call 403'd, the list stayed empty with nothing
	 * catching the rejection, and Submit was disabled forever — so an
	 * ap_manager or cfo (both of whom the backend lets submit, and the manager
	 * assign) could not advance a single invoice on a workflow whose approval
	 * step is `approver_strategy: "manual"`. That is the seeded default.
	 *
	 * The reviewer endpoint gates on exactly what `POST /invoices/{id}/assign`
	 * gates on, so a CFO gets a 403 from it too — deliberately, since a CFO
	 * cannot call assign either. That is why the fallback's removal costs
	 * nothing and why the submit-UNASSIGNED path below is load-bearing rather
	 * than a nicety: an empty picker is a normal outcome, not a failure.
	 *
	 * `fetchAssignableReviewers` resolves `false` rather than throwing — a
	 * failure is state the picker renders (see `approverNote`). The `.catch` is
	 * there for the fire-and-forget rule (`utils/storeLoadRejection.test.ts`),
	 * which is textual and blunt on purpose, not because this can reject.
	 *
	 * The approval-chain stepper below (`chainLevels`) reuses this same
	 * directory to resolve a named approver's id to a display name — it's the
	 * correct source (active users holding `invoice.approve`), not a
	 * coincidence of reuse.
	 */
	$effect(() => {
		if (needsApproverSelect || chainLevels.length > 0) {
			adminStore.fetchAssignableReviewers().catch(() => {});
		}
	});

	/**
	 * Multi-level approval-chain progress (`state_data.approval_levels` on the
	 * invoice's `WorkflowInstance` — backend/app/services/approval_chain.py) —
	 * which levels are done, who's approved, who's still pending. `null` for
	 * an invoice with no chain (manual/specific/auto strategy) or no workflow
	 * instance yet (`new`).
	 */
	let workflowInstance = $state<WorkflowInstanceDetail | null>(null);
	let chainState = $derived(workflowInstance?.state_data?.approval_levels ?? null);
	let chainLevels = $derived(chainState?.levels ?? []);
	let chainCurrentLevel = $derived(chainState?.current_level ?? 0);

	async function loadWorkflowInstance() {
		try {
			workflowInstance = await getInvoiceWorkflowInstance(invoice.id);
		} catch {
			// non-critical — modal still works without the chain-progress stepper
			workflowInstance = null;
		}
	}

	function resolveApproverName(userId: string): string {
		const match = adminStore.assignableReviewers.find((u) => u.id === userId);
		return match?.full_name ?? m('invoices.modal.chain.unknownApprover');
	}

	function chainStatusLabel(chainStepStatus: 'done' | 'current' | 'pending'): string {
		if (chainStepStatus === 'done') return m('invoices.modal.chain.statusDone');
		if (chainStepStatus === 'current') return m('invoices.modal.chain.statusCurrent');
		return m('invoices.modal.chain.statusPending');
	}

	function chainProgressLabel(approved: number, required: number): string {
		return m('invoices.modal.chain.progress', { approved, required });
	}

	/** Who the picker offers: active assignable reviewers other than the
	 *  submitter. */
	let reviewerOptions = $derived(
		adminStore.assignableReviewers.filter((u) => u.is_active && u.id !== auth.user?.id)
	);

	/** Only block Submit while there is genuinely someone to pick. An empty
	 *  list — because the lookup failed, or because this tenant has no other
	 *  active user — must not be a dead end: `POST /invoices/{id}/complete`
	 *  moves the invoice to `ready_for_review` with no assignee, and an
	 *  unassigned invoice is reviewable by any approver. */
	let approverRequired = $derived(needsApproverSelect && reviewerOptions.length > 0);

	/** Explain the missing picker — but only once the lookup has settled, so a
	 *  slow response doesn't flash a "no reviewers" note that is then wrong. */
	let approverNote = $derived.by(() => {
		if (!needsApproverSelect || !adminStore.reviewersLoaded || reviewerOptions.length > 0) {
			return '';
		}
		return adminStore.reviewersErrored
			? m('invoices.modal.approverUnavailable')
			: m('invoices.modal.approverNone');
	});

	let isClerkOnly = $derived(auth.isClerkOnly);
	let isDone = $derived(status === 'done' || status === 'sent_to_erp');
	let isExtracting = $derived(status === 'pending');
	let resettingExtraction = $state(false);

	async function handleResetExtraction() {
		resettingExtraction = true;
		try {
			await api.post(`/api/invoices/${invoice.id}/reset-extraction`, {});
			await refreshList();
			status = 'failed' as typeof status;
			extracting = false;
			extractionStatus = '';
			toast(m('invoices.modal.toast.extractionReset'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.resetFailed'), 'error');
		} finally {
			resettingExtraction = false;
		}
	}
	let isErpStatus = $derived(
		status === 'sending_to_erp' || status === 'sent_to_erp' || status === 'posted_in_erp' ||
		status === 'payment_scheduled' || status === 'paid'
	);
	let canRetryErp = $derived(status === 'failed' && !isClerkOnly && invoice.approved_by);
	let retryingErp = $state(false);
	let canExtract = $derived(
		(status === 'new' || status === 'failed') && currentFileUrl
	);
	let extracting = $state(false);
	let canDelete = $derived(
		!isClerkOnly && status !== 'done' && status !== 'sent_to_erp' && status !== 'sending_to_erp'
	);
	let canManageFile = $derived(!isClerkOnly && status !== 'done');
	let isReadyForReview = $derived(status === 'ready_for_review');
	let canReview = $derived(isReadyForReview && !isClerkOnly && (
		!invoice.assigned_to_id || invoice.assigned_to_id === auth.user?.id
	));
	let canSubmitStatus = $derived(
		isClerkOnly
			? status === 'new'
			: status === 'new' || status === 'approved'
	);

	let submitLabel = $derived.by(() => {
		if (status === 'new' && activeSteps.approval) return m('invoices.modal.submit.forReview');
		if (status === 'approved' && activeSteps.erp_export) return m('invoices.modal.submit.toErp');
		return m('invoices.modal.submit.markComplete');
	});

	let missingFields = $derived.by(() => {
		const missing: string[] = [];
		if (!vendor.trim()) missing.push(m('invoices.modal.field.vendor'));
		if (!invoice_number.trim()) missing.push(m('invoices.modal.field.invoiceNumber'));
		// `amount` is money, so "is there a usable figure here?" is a predicate —
		// `isPositiveAmount`, not `<= 0` on a value that may be an exact string.
		if (!isPositiveAmount(amount)) missing.push(m('invoices.modal.field.amount'));
		return missing;
	});

	let canSubmit = $derived(canSubmitStatus && missingFields.length === 0);

	// Financial fields are frozen once an invoice is approved: the backend 409s
	// a PATCH that includes any of them (the signed-off amount is what the
	// payment run pays — services/api/invoices.py `_FINANCIALLY_LOCKED_STATUSES`).
	// `vendor` is in that frozen set too — re-saving it re-runs the vendor
	// matcher and re-points `Invoice.vendor_id`, which is where the payment run
	// reads the payee's bank details from, so WHO gets paid is frozen alongside
	// HOW MUCH.
	// Both Save and the workflow-submit button ("Send to ERP" / Mark Complete)
	// reuse this field write, so once locked we must send only the non-financial
	// metadata (GL coding, addresses, notes) — otherwise every write to an
	// approved invoice fails and, in particular, "Send to ERP" never fires.
	const FINANCIALLY_LOCKED_STATUSES = [
		'approved',
		'sending_to_erp',
		'sent_to_erp',
		'posted_in_erp',
		'payment_scheduled',
		'paid',
		'done',
	];
	let financiallyLocked = $derived(FINANCIALLY_LOCKED_STATUSES.includes(status));

	function invoiceFieldPayload(): Record<string, unknown> {
		const payload: Record<string, unknown> = {
			invoice_number,
			due_date,
			po_number,
			description,
			vendor_address: vendor_address || null,
			vendor_tax_id: vendor_tax_id || null,
			ship_to_address: ship_to_address || null,
			payment_method: payment_method || null,
			reference_number: reference_number || null,
			gl_account: gl_account || null,
			cost_center: cost_center || null,
			payment_terms: payment_terms || null,
			department: department || null,
			project: project || null,
			// Optimistic-concurrency token: the `updated_at` this modal loaded
			// alongside `invoice` (a static snapshot — see the `$props()` doc
			// above). Sent back VERBATIM as captured, never round-tripped
			// through a JS `Date` (see the field's own doc in `types/invoice.ts`
			// for why that would false-positive every save).
			expected_updated_at: invoice.updated_at,
		};
		if (!financiallyLocked) {
			payload.vendor = vendor;
			payload.amount = amount;
			payload.tax_rate = tax_rate;
		}
		return payload;
	}

	/**
	 * True (and handled — a confirm-to-reload prompt, never a silent failure)
	 * when `err` is the backend's stale-`expected_updated_at` 409: another user
	 * saved this invoice after this modal loaded it. Distinguished from the
	 * PATCH endpoint's OTHER 409s (immutable status, financially-locked fields)
	 * by the backend's own detail text — same substring-match convention
	 * `submitDone()`'s catch below already uses to tell a validation 409 apart
	 * from a generic failure.
	 */
	function handleStaleConflict(err: unknown): boolean {
		if (!(err instanceof ApiError) || err.status !== 409) return false;
		if (!err.message.toLowerCase().includes('modified since you loaded it')) return false;
		if (confirm(m('invoices.modal.staleConflict.confirm'))) {
			// Discard this modal's stale edits and let the host reopen it fresh —
			// nothing in this modal re-fetches a single invoice by id.
			refreshList();
			onclose();
		} else {
			toast(m('invoices.modal.toast.staleConflict'), 'error');
		}
		return true;
	}

	async function save() {
		saving = true;
		try {
			await invoiceStore.update(invoice.id, invoiceFieldPayload());
			toast(m('invoices.modal.toast.saved'), 'success');
			onclose();
		} catch (err) {
			if (!handleStaleConflict(err)) {
				toast(err instanceof Error ? err.message : m('invoices.modal.toast.saveFailed'), 'error');
			}
		} finally {
			saving = false;
		}
	}

	async function submitDone() {
		submitting = true;
		try {
			// Save any edits first, then advance the workflow. On an approved
			// invoice `invoiceFieldPayload()` omits the frozen financial fields,
			// so the pre-save no longer 409s and the "Send to ERP" advance runs.
			await invoiceStore.update(invoice.id, invoiceFieldPayload());
			const result = await api.post<{ id: string; status: string }>(`/api/invoices/${invoice.id}/complete`, {});
			// If manually assigning an approver and invoice moved to ready_for_review
			if (selectedApproverId && result.status === 'ready_for_review') {
				await api.post(`/api/invoices/${invoice.id}/assign`, { user_id: selectedApproverId });
			}
			toast(m('invoices.modal.toast.submitted'), 'success');
			// No refresh here: `onclose()` hands the list back to the host,
			// which refetches it with its own filters (same reason
			// `handleApprove` doesn't refresh either).
			onclose();
		} catch (err) {
			if (handleStaleConflict(err)) return;
			const msg = err instanceof Error ? err.message : m('invoices.modal.toast.submitFailed');
			// Don't toast field validation errors — the form highlights them already
			if (!msg.toLowerCase().includes('missing') && !msg.toLowerCase().includes('required field')) {
				toast(msg, 'error');
			}
		} finally {
			submitting = false;
		}
	}

	async function handleApprove() {
		reviewing = true;
		try {
			const prevStatus = status;
			const result = await api.post<Invoice>(`/api/invoices/${invoice.id}/approve`, {});
			// A multi-level approval chain (services/review.py::approve_invoice)
			// stays in `ready_for_review` until every level is satisfied — the
			// backend recorded this approval on the current level (writing an
			// `invoice.approval_step` audit row, not `invoice.approved`) but the
			// invoice hasn't moved. Distinguishing that from a final approval is
			// the whole point: an approver who thinks their sign-off just sent
			// the invoice onward, when in fact two more people still need to act,
			// is the exact "chain progress is invisible" gap this closes.
			if (result.status === prevStatus) {
				await loadWorkflowInstance();
				const state = workflowInstance?.state_data?.approval_levels;
				const activeLevel =
					state && !chainIsComplete(state) ? state.levels[state.current_level] : undefined;
				const remaining = activeLevel ? chainLevelRemaining(activeLevel) : null;
				toast(
					remaining !== null
						? m('invoices.modal.toast.approvedPartial', { n: remaining })
						: m('invoices.modal.toast.approvedPartialGeneric'),
					'success'
				);
			} else {
				toast(m('invoices.modal.toast.approvedFinal'), 'success');
			}
			// The host (/invoices) refetches the list with ITS active filters
			// in closeInvoiceModal, so refreshing here would only race that
			// refetch — and any refresh that skipped the host's filters would
			// leave the just-approved row visible under an active status chip.
			// Every close-then-refresh handler below follows this shape.
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.approveFailed'), 'error');
		} finally {
			reviewing = false;
		}
	}

	async function handleReject() {
		if (!rejectReason.trim()) return;
		reviewing = true;
		try {
			await api.post(`/api/invoices/${invoice.id}/reject`, { reason: rejectReason.trim() });
			toast(m('invoices.modal.toast.rejected'), 'success');
			// Host refetches with its active filters on close (see approve).
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.rejectFailed'), 'error');
		} finally {
			reviewing = false;
		}
	}

	async function handleRetryErp() {
		retryingErp = true;
		try {
			await api.post(`/api/invoices/${invoice.id}/retry-erp`, {});
			toast(m('invoices.modal.toast.erpRetryInitiated'), 'success');
			// The host refetches with its filters on close — see `submitDone`.
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.retryFailed'), 'error');
		} finally {
			retryingErp = false;
		}
	}

	let extractionStatus = $state('');

	async function handleExtract() {
		extracting = true;
		extractionStatus = m('invoices.modal.extraction.triggering');
		try {
			await api.post(`/api/invoices/${invoice.id}/extract`, {});
			extractionStatus = m('invoices.modal.extraction.extractingFields');
			await pollForCompletion();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.extractionFailed'), 'error');
			extracting = false;
			extractionStatus = '';
		}
	}

	/**
	 * Set when this modal is destroyed, so `pollForCompletion` can stop.
	 *
	 * The poll runs for up to 60 s and nothing blocks Close while it does, so it
	 * routinely outlives its component. When it did, it kept polling a closed
	 * modal and then refreshed the invoice list — unfiltered. The host has
	 * already re-applied its filters by then, so seconds after closing, the list
	 * silently widened to every status while the chips still showed a filter,
	 * and the store's `lastParams` reset so Load-more paged the wrong set.
	 * Every `await` in the poll re-checks this before touching the network or
	 * any shared state. Not `$state` — nothing renders it.
	 */
	let pollCancelled = false;
	$effect(() => {
		return () => {
			pollCancelled = true;
		};
	});

	async function pollForCompletion() {
		const maxPolls = 30;
		const interval = 2000;

		for (let i = 0; i < maxPolls; i++) {
			await new Promise(r => setTimeout(r, interval));
			if (pollCancelled) return;

			try {
				const updated = await api.get<import('$lib/types/invoice').Invoice>(`/api/invoices/${invoice.id}`);
				if (pollCancelled) return;

				if (updated.status !== 'pending') {
					// Extraction finished — update the modal fields with extracted data
					vendor = updated.vendor || vendor;
					invoice_number = updated.invoice_number || invoice_number;
					amount = updated.amount || amount;
					due_date = updated.due_date || due_date;
					po_number = updated.po_number || po_number;
					description = updated.description || description;
					status = updated.status as typeof status;
					vendor_address = updated.vendor_address ?? vendor_address;
					vendor_tax_id = updated.vendor_tax_id ?? vendor_tax_id;
					ship_to_address = updated.ship_to_address ?? ship_to_address;
					tax_rate = updated.tax_rate ?? tax_rate;
					payment_method = updated.payment_method ?? payment_method;
					reference_number = updated.reference_number ?? reference_number;
					gl_account = updated.gl_account ?? gl_account;
					cost_center = updated.cost_center ?? cost_center;
					payment_terms = updated.payment_terms ?? payment_terms;
					department = updated.department ?? department;
					project = updated.project ?? project;

					// Refresh the invoice list, audit log, and line items. Re-check
					// first: the field writes above are component-local, but this
					// touches the host's shared list, so a close during the last
					// await must not reach it.
					if (pollCancelled) return;
					await refreshList();
					await loadLineItems();
					await loadExtractionConfidence();
					await loadAuditLog();

					if (updated.status === 'ready_for_review') {
						toast(m('invoices.modal.toast.extractionComplete'), 'success');
					} else if (updated.status === 'failed') {
						toast(m('invoices.modal.toast.extractionFailedLog'), 'error');
					}

					extracting = false;
					extractionStatus = '';
					return;
				}

				extractionStatus = m('invoices.modal.extraction.extractingProgress', { n: i + 1 });
			} catch {
				// Poll failed — keep trying
			}
		}

		// Timeout
		if (pollCancelled) return;
		extracting = false;
		extractionStatus = '';
		toast(m('invoices.modal.toast.extractionTimeout'), 'warning');
		await refreshList();
	}

	async function deleteInvoice() {
		deleting = true;
		try {
			await api.delete(`/api/invoices/${invoice.id}`);
			toast(m('invoices.modal.toast.deleted'), 'success');
			// The host refetches with its filters on close — see `submitDone`.
			onclose();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.deleteFailed'), 'error');
		} finally {
			deleting = false;
			confirmDelete = false;
		}
	}

	async function handleFileSelected(e: Event) {
		const input = e.target as HTMLInputElement;
		const file = input.files?.[0];
		if (!file) return;
		const isReplace = !!currentFileUrl;
		uploadingFile = true;
		try {
			const response = await api.upload<Invoice>(
				`/api/invoices/${invoice.id}/file`,
				file,
				undefined,
				isReplace ? 'PUT' : 'POST'
			);
			currentFileUrl = response.file_url;
			invoiceStore.patchLocal(invoice.id, { file_url: response.file_url });
			toast(isReplace ? m('invoices.modal.toast.fileReplaced') : m('invoices.modal.toast.fileUploaded'), 'success');
			await loadAuditLog();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.fileActionFailed'), 'error');
		} finally {
			uploadingFile = false;
			input.value = '';
		}
	}

	async function handleDeleteFile() {
		if (!confirmDeleteFile) {
			confirmDeleteFile = true;
			return;
		}
		deletingFile = true;
		try {
			await api.delete(`/api/invoices/${invoice.id}/file`);
			currentFileUrl = null;
			invoiceStore.patchLocal(invoice.id, { file_url: null });
			toast(m('invoices.modal.toast.fileDeleted'), 'success');
			await loadAuditLog();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.fileActionFailed'), 'error');
		} finally {
			deletingFile = false;
			confirmDeleteFile = false;
		}
	}

	function handleFileActionsWindowClick(e: MouseEvent) {
		if (confirmDeleteFile && !(e.target as HTMLElement).closest('.row-action')) {
			confirmDeleteFile = false;
		}
		if (showEinvoiceMenu && !(e.target as HTMLElement).closest('.export-wrapper')) {
			showEinvoiceMenu = false;
		}
	}

	// ---------------------------------------------------------------- e-invoice
	//
	// `GET /api/invoices/{id}/einvoice?format=…` generates a standards-compliant
	// document (UBL 2.1 / CII, or one of the four national dialects) and is open
	// to all four roles. It 422s a tax-invalid invoice on purpose — an AP user
	// must not emit a non-compliant document — so the refusal is the
	// interesting outcome, not an edge case: it goes to a persistent alert
	// region beside the control, not a toast that fades before it's read.

	let showEinvoiceMenu = $state(false);
	let einvoiceBusy = $state<EInvoiceFormat | null>(null);
	let einvoiceError = $state<{
		format: EInvoiceFormat;
		/** 422 — the document is not valid for this dialect (vs. any other failure). */
		invalid: boolean;
		detail: string;
		issues: EInvoiceValidationIssue[];
	} | null>(null);

	function einvoiceFormatLabel(format: EInvoiceFormat): string {
		const opt = E_INVOICE_FORMATS.find((f) => f.format === format);
		return opt ? m(opt.labelKey) : format;
	}

	async function handleEinvoiceDownload(format: EInvoiceFormat) {
		showEinvoiceMenu = false;
		einvoiceError = null;
		einvoiceBusy = format;
		try {
			const blob = await downloadEInvoice(invoice.id, format);
			triggerDownload(blob, einvoiceFilename(format, invoice.invoice_number || invoice.id));
		} catch (err) {
			// `api.downloadBlob` already ran the body through `formatApiDetail`,
			// which renders the backend's structured error list as
			// `"field: message; …"`. Re-split it into the per-field rows it was
			// built from; an unparseable detail (any other 4xx/5xx) renders
			// verbatim rather than being reshaped into a lie.
			const detail = err instanceof Error ? err.message : '';
			const invalid = err instanceof ApiError && err.status === 422;
			einvoiceError = {
				format,
				invalid,
				detail,
				issues: invalid ? parseEInvoiceIssues(detail) : [],
			};
		} finally {
			einvoiceBusy = null;
		}
	}

	// ------------------------------------------------------------------ PEPPOL
	//
	// Transmission is idempotent server-side (a partial unique index), but it
	// is still a real outbound onto a public network — so it is confirm-then-act
	// (the same inline confirm shape the reject form uses), and the outcome is
	// reported from the RESPONSE: `already_sent` says the short-circuit hit and
	// nothing new left the building, which must never read as a fresh send.

	let showPeppolForm = $state(false);
	let peppolReceiverScheme = $state('');
	let peppolReceiverValue = $state('');
	let peppolSending = $state(false);
	let peppolResult = $state<PeppolSendResult | null>(null);
	// Same two shapes as the export refusal: a named failure code, or the
	// per-field rendering of the structured 422 — PEPPOL runs the EN 16931 /
	// BIS Billing 3.0 conformance pass on top of the export's own guard, and it
	// reports the rule id (`due_date: BR-CO-25`) through that identical
	// channel, so it gets the identical readable treatment rather than a second
	// error format.
	let peppolError = $state<{ detail: string; issues: EInvoiceValidationIssue[] } | null>(null);

	let canSendPeppol = $derived(auth.hasAnyRole('admin', 'ap_manager', 'cfo'));
	let peppolStatusReady = $derived(PEPPOL_SENDABLE_STATUSES.has(status));

	// The transmission LOG, read on open. Until this endpoint existed the only
	// evidence a send had ever happened was the response to a send made in this
	// same session, so the block could report an outcome but had to stay silent
	// about the state — and silence reads as "not sent", which is the one claim
	// it could not support. Four states, deliberately distinct: not-yet-known
	// (`null`, still loading or unreadable), none, a live transmission, or only
	// failed attempts (which the backend's partial unique index also treats as
	// re-sendable, so the UI must not present them as done).
	let peppolTransmissions = $state<PeppolTransmissionSummary[] | null>(null);
	let peppolTransmissionsErrored = $state(false);
	let peppolLive = $derived(
		peppolTransmissions ? livePeppolTransmission(peppolTransmissions) : null
	);

	async function loadPeppolTransmissions() {
		// Reference invoice.id first so the effect re-runs when the modal is
		// pointed at a different invoice, and drop the previous invoice's rows
		// rather than showing them against this one.
		const id = invoice.id;
		peppolTransmissions = null;
		peppolTransmissionsErrored = false;
		try {
			const res = await listPeppolTransmissions(id);
			peppolTransmissions = res.transmissions;
		} catch {
			// A read failure must not become a "not yet transmitted" claim.
			peppolTransmissions = null;
			peppolTransmissionsErrored = true;
		}
	}

	async function handlePeppolSend() {
		peppolSending = true;
		peppolError = null;
		try {
			const result = await sendInvoiceOverPeppol(invoice.id, {
				receiver_scheme: peppolReceiverScheme.trim(),
				receiver_value: peppolReceiverValue.trim(),
			});
			peppolResult = result;
			showPeppolForm = false;
			await loadAuditLog();
			await loadPeppolTransmissions();
		} catch (err) {
			const detail = err instanceof Error ? err.message : '';
			const key = PEPPOL_ERROR_KEYS[detail];
			peppolError = {
				detail: key ? m(key) : detail || m('invoices.modal.peppol.failed'),
				issues: key ? [] : parseEInvoiceIssues(detail),
			};
		} finally {
			peppolSending = false;
		}
	}

	function triggerDownload(blob: Blob, filename: string) {
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = filename;
		a.click();
		URL.revokeObjectURL(url);
	}

	interface LineItem {
		id: string;
		line_number: number | null;
		item_code: string | null;
		description: string | null;
		quantity: number | null;
		unit_price: number | null;
		tax: number | null;
		total: number | null;
		gl_account: string | null;
	}

	let lineItems = $state<LineItem[]>([]);
	let lineItemsDirty = $state(false);
	let savingLines = $state(false);

	// The line-item table is an EDITOR over a fetched list, and three things
	// load it: the modal's open `$effect`, the extraction poll when it finishes,
	// and `saveLineItems` afterwards. Any of those can be in flight while the
	// user is typing into a cell — and a load resolving then replaces the whole
	// array, wiping unsaved edits while `lineItemsDirty` stays set on rows that
	// no longer hold them. `markLineItemsDirty()` retires the in-flight load
	// before every local edit. See `frontend/CLAUDE.md` § Sequencing list
	// fetches.
	const lineItemsSequence = createRequestSequencer();

	/** Flag an unsaved local edit — and retire whatever is already in flight,
	 *  whose response predates it: a load (which would replace the table) or a
	 *  save (whose payload doesn't carry this edit — see `saveLineItems`). */
	function markLineItemsDirty() {
		lineItemsSequence.supersedeInFlight();
		lineItemsDirty = true;
	}

	/**
	 * Outcome of `PUT /api/invoices/{id}/line-items`. The backend reconciles
	 * the re-summed lines against the header and reports the verdict here —
	 * money as exact decimal strings, never floats.
	 */
	interface LineItemsSaveResult {
		saved: number;
		line_items_total: string;
		header_amount: string;
		reconciles_with_header: boolean;
	}

	// Set when the last save came back NOT reconciling. The `invoice` prop is
	// a snapshot taken when the row was clicked, so the `line_total_mismatch`
	// warning the backend just raised is not visible on it until the modal is
	// reopened — this response-driven banner is what makes the divergence
	// visible to the person who caused it, immediately.
	let lineTotalMismatch = $state<LineItemsSaveResult | null>(null);

	// --- Contract link ---
	interface ContractOption {
		id: string;
		contract_number: string;
		vendor_name: string | null;
	}
	let contractId = $state<string | null>(invoice.contract_id);
	let contracts = $state<ContractOption[]>([]);
	let pickContractId = $state('');
	let linkingContract = $state(false);
	let linkedContract = $derived(contracts.find((c) => c.id === contractId) ?? null);

	$effect(() => {
		loadContracts();
	});

	async function loadContracts() {
		try {
			const data = await api.get<{ items: ContractOption[] }>('/api/contracts?status=active&page_size=100');
			contracts = data.items;
		} catch {
			// non-critical — link control still renders, just without options
		}
	}

	async function linkContract() {
		if (!pickContractId) return;
		linkingContract = true;
		try {
			await api.post(`/api/invoices/${invoice.id}/link-contract`, { contract_id: pickContractId });
			contractId = pickContractId;
			pickContractId = '';
			await refreshList();
			toast(m('invoices.modal.toast.contractLinked'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.linkFailed'), 'error');
		} finally {
			linkingContract = false;
		}
	}

	async function unlinkContract() {
		linkingContract = true;
		try {
			await api.post(`/api/invoices/${invoice.id}/unlink-contract`, {});
			contractId = null;
			await refreshList();
			toast(m('invoices.modal.toast.contractUnlinked'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.unlinkFailed'), 'error');
		} finally {
			linkingContract = false;
		}
	}

	function updateLineItem(idx: number, field: string, value: unknown) {
		lineItems = lineItems.map((li, i) => i === idx ? { ...li, [field]: value } : li);
		markLineItemsDirty();
	}

	function addLineItem() {
		lineItems = [...lineItems, {
			id: '',
			line_number: lineItems.length + 1,
			item_code: null,
			description: '',
			quantity: 1,
			unit_price: null,
			tax: null,
			total: null,
			gl_account: gl_account || null,
		}];
		markLineItemsDirty();
	}

	function removeLineItem(idx: number) {
		lineItems = lineItems.filter((_, i) => i !== idx);
		markLineItemsDirty();
	}

	async function saveLineItems() {
		// The PUT is itself a request whose response can be superseded, so it
		// takes a token like any load. Only the Save BUTTON is disabled while it
		// is in flight — the cells stay editable — and an edit made in that
		// window is not in the payload this request carries.
		//
		// It asks `wasSupersededByEdit`, NOT `canCommit`: only an edit
		// invalidates what this request sent. `canCommit` would also go false
		// for an unrelated newer read — the extraction poll's own
		// `loadLineItems()` landing mid-save — and the dirty flag would then
		// stick on, leaving a Save button up over a table nobody had touched.
		const saveToken = lineItemsSequence.start();
		savingLines = true;
		try {
			const res = await api.put<LineItemsSaveResult>(`/api/invoices/${invoice.id}/line-items`, lineItems.map((li, idx) => ({
				line_number: idx + 1,
				item_code: li.item_code,
				description: li.description,
				quantity: li.quantity,
				unit_price: li.unit_price,
				tax: li.tax,
				total: li.total,
				gl_account: li.gl_account,
			})));
			const editedDuringSave = lineItemsSequence.wasSupersededByEdit(saveToken);
			// Clearing `lineItemsDirty` over an edit made mid-save is the worse
			// half of the bug: it loses the edit AND removes the Save button
			// (which only renders while dirty), so the user can't re-submit
			// what they just typed.
			if (!editedDuringSave) lineItemsDirty = false;
			// Surface the backend's reconciliation verdict inline instead of
			// letting it stay invisible until the modal is reopened. A
			// mismatch is a blocking condition downstream (the invoice can't
			// enter a payment run), so it gets the persistent panel below the
			// table, not just a transient toast. This reports what the server
			// now holds, so it stands whether or not the table moved on.
			lineTotalMismatch = res.reconciles_with_header ? null : res;
			toast(
				res.reconciles_with_header
					? m('invoices.modal.toast.lineItemsSaved')
					: m('invoices.modal.toast.lineItemsMismatch'),
				res.reconciles_with_header ? 'success' : 'warning'
			);
			// Re-reading the server list would discard that mid-save edit — the
			// server hasn't been told about it yet.
			if (!editedDuringSave) await loadLineItems();
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.saveFailed'), 'error');
		} finally {
			savingLines = false;
		}
	}

	let auditLog = $state<AuditEntry[]>([]);

	// --- Supplier chat (between AP and the supplier). Lazily loaded on open,
	// modeled on loadAuditLog(). Local $state per the contract (no store). ---
	let chatThread = $state<ChatThread | null>(null);
	let chatLoading = $state(false);
	let chatTemplates = $state<ChatTemplate[]>([]);
	// Colleagues the composer's @mention picker may offer. This used to read
	// `adminStore.users` — a list only `/admin` and `/workflows/[id]` ever load,
	// so on `/invoices` (where this modal lives) the picker was permanently
	// empty, and visiting `/admin` first silently made it work. Same bug, and
	// the same fix, as the approver picker: a narrow endpoint gated exactly like
	// the action it feeds, never the admin directory.
	let chatMembers = $state<ChatMentionCandidate[]>([]);

	async function loadChatThread() {
		const id = invoice.id;
		chatLoading = true;
		try {
			chatThread = await getChatThread(id);
		} catch {
			// non-critical — feature flag off or older invoice; show empty thread
			chatThread = { id: null, invoice_id: id, status: 'open', resolved_at: null, resolved_by: null, messages: [] };
		} finally {
			chatLoading = false;
		}
	}

	async function loadChatTemplates() {
		try {
			chatTemplates = await getChatTemplates();
		} catch {
			// non-critical — composer still works without canned templates
		}
	}

	async function loadChatMembers() {
		try {
			chatMembers = await getChatMentionableUsers();
		} catch {
			// Non-critical, and it must stay that way: the composer sends
			// perfectly well with no mentions, so a failed roster load costs the
			// picker, not the message. An empty list simply renders no dropdown.
			chatMembers = [];
		}
	}

	async function handleChatSend(body: string, mentionUserIds: string[], file?: File) {
		if (file) {
			await uploadChatAttachment(invoice.id, file, body || undefined, mentionUserIds);
		} else {
			await postChatMessage(invoice.id, {
				body,
				mention_user_ids: mentionUserIds.length ? mentionUserIds : undefined,
			});
		}
		await loadChatThread();
		// Surface the new chat_message_posted row in the Activity timeline.
		await loadAuditLog();
	}

	async function handleChatResolve() {
		chatThread = await resolveChatThread(invoice.id);
		await loadAuditLog();
	}

	async function handleChatReopen() {
		chatThread = await reopenChatThread(invoice.id);
		await loadAuditLog();
	}

	async function handleChatDownload(fileUrl: string, filename: string) {
		// AP side renders the bytes through the auth'd client and saves them.
		const blob = await api.downloadBlob(fileUrl);
		triggerDownload(blob, filename);
	}

	// Per-field confidence from extraction
	type FieldConfidence = Record<string, number>;
	let fieldConfidence = $state<FieldConfidence>({});
	let erpInfo = $derived.by(() => {
		const erpActions = auditLog.filter(e =>
			e.action.startsWith('invoice.erp_') || e.action.startsWith('invoice.completed')
		);
		if (erpActions.length === 0) return null;
		const latest = erpActions[erpActions.length - 1];
		return {
			erp_reference: (latest.details as Record<string, unknown>)?.erp_reference as string | undefined,
			erp_document_id: (latest.details as Record<string, unknown>)?.erp_document_id as string | undefined,
			last_error: (latest.details as Record<string, unknown>)?.error as string | undefined,
			action: latest.action,
			actor: latest.actor_name,
			time: latest.created_at,
		};
	});
	let auditLoading = $state(false);

	interface RagNeighbor {
		invoice_id: string;
		similarity: number;
		vendor_name: string | null;
		invoice_number: string | null;
		amount: string | null;
	}
	interface PriorsData {
		vendor_cache_applied: string[];
		rag_neighbors: RagNeighbor[];
	}
	let priors = $state<PriorsData>({ vendor_cache_applied: [], rag_neighbors: [] });
	let priorsOpen = $state(false);

	// Audit-log summary (top of the modal). Lazily fetched on first open;
	// regenerated server-side only when the audit log changed.
	let summary = $state<AuditSummary | null>(null);
	let summaryLoading = $state(false);
	let summaryRegenerating = $state(false);
	// Managers/admins can force a regenerate (matches the backend RBAC on
	// POST /summary/regenerate).
	let canRegenerateSummary = $derived(auth.isAdmin || auth.isManager);

	// File preview — `<img src>` and `<iframe src>` can't reach the file
	// endpoint because they don't carry the Bearer token. Fetch the bytes
	// through the auth'd API client and render via a blob URL. The effect
	// also handles cleanup so we don't leak object URLs across invoices.
	let fileBlobUrl = $state<string | null>(null);
	let fileLoadError = $state(false);

	$effect(() => {
		const path = currentFileUrl;
		fileLoadError = false;
		if (!path) {
			fileBlobUrl = null;
			return;
		}
		let revoked = false;
		let createdUrl: string | null = null;
		api.fetchBlob(path)
			.then((url) => {
				if (revoked) {
					URL.revokeObjectURL(url);
					return;
				}
				createdUrl = url;
				fileBlobUrl = url;
			})
			.catch(() => {
				fileLoadError = true;
				fileBlobUrl = null;
			});
		return () => {
			revoked = true;
			if (createdUrl) URL.revokeObjectURL(createdUrl);
		};
	});

	$effect(() => {
		loadAuditLog();
		loadExtractionConfidence();
		loadLineItems();
		loadPriors();
		loadSummary();
		loadChatThread();
		loadChatTemplates();
		loadChatMembers();
		loadWorkflowInstance();
		// Only the roles that can send ever see the PEPPOL block, so nobody
		// else pays for the read.
		if (canSendPeppol) loadPeppolTransmissions();
	});

	async function loadSummary() {
		// Reference invoice.id so the effect re-runs when the modal is
		// pointed at a different invoice.
		const id = invoice.id;
		summaryLoading = true;
		try {
			summary = await api.get<AuditSummary>(`/api/invoices/${id}/summary`);
		} catch {
			// non-critical — modal still works without the summary line
			summary = null;
		} finally {
			summaryLoading = false;
		}
	}

	async function regenerateSummary() {
		summaryRegenerating = true;
		try {
			summary = await api.post<AuditSummary>(
				`/api/invoices/${invoice.id}/summary/regenerate`,
				{}
			);
		} catch (err) {
			toast(err instanceof Error ? err.message : m('invoices.modal.toast.summaryFailed'), 'error');
		} finally {
			summaryRegenerating = false;
		}
	}

	async function loadPriors() {
		try {
			priors = await api.get<PriorsData>(`/api/invoices/${invoice.id}/priors`);
		} catch {
			// non-critical — older invoices may predate the priors_metadata column
		}
	}

	async function loadLineItems() {
		const token = lineItemsSequence.start();
		try {
			const fetched = await api.get<LineItem[]>(`/api/invoices/${invoice.id}/line-items`);
			// Superseded by a newer load, or by an unsaved local edit.
			if (!lineItemsSequence.canCommit(token)) return;
			lineItems = fetched;
		} catch {
			// non-critical
		}
	}

	async function loadAuditLog() {
		auditLoading = true;
		try {
			auditLog = await getInvoiceAuditLog(invoice.id);
		} catch {
			// non-critical
		} finally {
			auditLoading = false;
		}
	}

	async function loadExtractionConfidence() {
		try {
			const results = await api.get<Array<{ raw_result: Record<string, unknown> | null }>>(`/api/invoices/${invoice.id}/extraction`);
			if (results.length === 0) return;
			const latest = results[0];
			const raw = latest.raw_result;
			if (!raw) return;

			const conf: FieldConfidence = {};
			for (const [key, val] of Object.entries(raw)) {
				if (!val || typeof val !== 'object') continue;
				if (!('confidence' in val) || !('value' in val)) continue;
				const field = val as { value: unknown; confidence: number };
				// Don't record confidence for fields the model didn't actually
				// extract — many adapters emit a "0% / Very low" confidence on
				// blank fields, which would render a misleading dot next to an
				// empty input. Treat null / empty string / zero-length as
				// "no extraction" for dot-rendering purposes.
				const v = field.value;
				const hasValue =
					v !== null &&
					v !== undefined &&
					!(typeof v === 'string' && v.trim() === '');
				if (!hasValue) continue;
				conf[key] = field.confidence;
			}
			fieldConfidence = conf;
		} catch {
			// non-critical
		}
	}

	// Render a confidence dot only when (a) the model reported a confidence
	// AND (b) the value the user actually sees in the input is non-empty.
	// The raw extraction result alone isn't enough — adapters sometimes emit
	// values the backend can't parse (e.g. "8.25%" for a Decimal column),
	// leaving the input blank but raw_result populated. Previously we showed
	// a dot next to a blank field in that case.
	function dot(field: string, value: unknown): boolean {
		if (!(field in fieldConfidence)) return false;
		if (value === null || value === undefined) return false;
		if (typeof value === 'string' && value.trim() === '') return false;
		if (typeof value === 'number' && Number.isNaN(value)) return false;
		return true;
	}

	function confidenceLabel(score: number): string {
		if (score >= 0.95) return m('invoices.modal.confidence.veryHigh');
		if (score >= 0.85) return m('invoices.modal.confidence.high');
		if (score >= 0.7) return m('invoices.modal.confidence.medium');
		if (score >= 0.5) return m('invoices.modal.confidence.low');
		return m('invoices.modal.confidence.veryLow');
	}

	function confidenceColor(score: number): string {
		if (score >= 0.85) return '#1fa86a';
		if (score >= 0.7) return '#d4940a';
		return '#e04040';
	}

	function formatAuditDate(iso: string): string {
		if (!iso) return '';
		return formatDate(iso, '', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
	}

	function handleBackdrop(e: MouseEvent) {
		if (e.target === e.currentTarget) onclose();
	}

	// Esc + focus trap/restore are handled by the shared `focusTrap` action.
</script>

<svelte:window onclick={handleFileActionsWindowClick} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="backdrop" onclick={handleBackdrop}>
	<div
		use:focusTrap={{ onEscape: onclose }}
		class="modal"
		class:fullscreen
		role="dialog"
		aria-label="Edit invoice {invoice.invoice_number}"
		tabindex="-1"
	>
		<header>
			<h2>{m('invoices.modal.header', { number: invoice.invoice_number })}</h2>
			<div class="header-actions">
				<button class="icon-btn" onclick={toggleFullscreen} aria-label={fullscreen ? m('invoices.modal.exitFullscreen') : m('invoices.modal.fullscreen')}>
					{#if fullscreen}
						<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
							<polyline points="4 14 10 14 10 20" /><polyline points="20 10 14 10 14 4" />
							<line x1="14" y1="10" x2="21" y2="3" /><line x1="3" y1="21" x2="10" y2="14" />
						</svg>
					{:else}
						<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
							<polyline points="15 3 21 3 21 9" /><polyline points="9 21 3 21 3 15" />
							<line x1="21" y1="3" x2="14" y2="10" /><line x1="3" y1="21" x2="10" y2="14" />
						</svg>
					{/if}
				</button>
				<button class="icon-btn close-btn" onclick={onclose} aria-label={m('invoices.modal.close')}>&times;</button>
			</div>
		</header>

		<div class="split" class:resizing>
			<div class="pdf-pane">
				{#if canManageFile}
					<div class="file-actions">
						{#if !currentFileUrl}
							<button
								type="button"
								class="btn-file-action"
								disabled={uploadingFile}
								aria-label={m('invoices.modal.file.uploadAria')}
								onclick={() => fileInput?.click()}
							>
								{m('invoices.modal.file.upload')}
							</button>
						{:else}
							<button
								type="button"
								class="btn-file-action"
								disabled={uploadingFile}
								onclick={() => fileInput?.click()}
							>
								{m('invoices.modal.file.replace')}
							</button>
							<RowAction
								variant="danger"
								armed={confirmDeleteFile}
								disabled={deletingFile}
								onclick={handleDeleteFile}
							>
								{confirmDeleteFile ? m('invoices.modal.file.deleteConfirm') : m('invoices.modal.file.delete')}
							</RowAction>
						{/if}
						<input
							type="file"
							accept=".pdf,.png,.jpg,.jpeg,.tiff"
							bind:this={fileInput}
							onchange={handleFileSelected}
							hidden
						/>
					</div>
				{/if}
				{#if currentFileUrl}
					{@const isImage = /\.(png|jpg|jpeg|tiff?)$/i.test(currentFileUrl)}
					{#if fileLoadError}
						<div class="no-pdf">{m('invoices.modal.fileLoadError')}</div>
					{:else if !fileBlobUrl}
						<div class="no-pdf">{m('common.loading')}</div>
					{:else if isImage}
						<img src={fileBlobUrl} alt={m('invoices.modal.imageAlt', { number: invoice.invoice_number })} class="invoice-image" />
					{:else}
						<iframe src={fileBlobUrl} title={m('invoices.modal.pdfTitle', { number: invoice.invoice_number })}></iframe>
					{/if}
				{:else}
					<div class="no-pdf">
						<svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
							<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
							<polyline points="14 2 14 8 20 8" />
							<line x1="9" y1="15" x2="15" y2="15" />
						</svg>
						<span>{m('invoices.modal.noPdf')}</span>
					</div>
				{/if}
			</div>

			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="resize-handle" onmousedown={startResize}></div>
			<div class="form-pane" style="width:{formPaneWidth}px">
				<form onsubmit={(e) => { e.preventDefault(); save(); }}>
					{#if summaryLoading && !summary}
						<div class="audit-summary" data-testid="audit-summary">
							<div class="audit-summary-skeleton"></div>
							<div class="audit-summary-skeleton short"></div>
						</div>
					{:else if summary}
						<section class="audit-summary" data-testid="audit-summary" aria-label={m('invoices.modal.summaryAria')}>
							<div class="audit-summary-head">
								<span class="audit-summary-label">{m('invoices.modal.summary')}</span>
								{#if canRegenerateSummary}
									<button
										type="button"
										class="audit-summary-regen"
										onclick={regenerateSummary}
										disabled={summaryRegenerating}
										data-testid="audit-summary-regenerate"
									>
										{summaryRegenerating ? m('invoices.modal.regenerating') : m('invoices.modal.regenerate')}
									</button>
								{/if}
							</div>
							<!-- Plain-text binding only — never {@html} — so the model
							     output can't inject markup (XSS invariant). -->
							<p class="audit-summary-text" data-testid="audit-summary-text">{summary.text}</p>
							{#if summary.confidence_context}
								<p class="audit-summary-context" data-testid="audit-summary-context">
									{summary.confidence_context}
								</p>
							{/if}
						</section>
					{/if}

					<div class="form-grid">
						<label class:field-error={canSubmitStatus && !vendor.trim()}>
							<span>{m('invoices.modal.field.vendor')} <em class="required">*</em> {#if dot('vendor_name', vendor)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.vendor_name)}" data-tip="{Math.round(fieldConfidence.vendor_name * 100)}% — {confidenceLabel(fieldConfidence.vendor_name)}"></span>{/if}</span>
							<input type="text" bind:value={vendor} required />
						</label>
						<label class:field-error={canSubmitStatus && !invoice_number.trim()}>
							<span>{m('invoices.modal.field.invoiceNumber')} <em class="required">*</em> {#if dot('invoice_number', invoice_number)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.invoice_number)}" data-tip="{Math.round(fieldConfidence.invoice_number * 100)}% — {confidenceLabel(fieldConfidence.invoice_number)}"></span>{/if}</span>
							<input type="text" bind:value={invoice_number} required />
						</label>
						<label class:field-error={canSubmitStatus && !isPositiveAmount(amount)}>
							<span>{m('invoices.modal.field.amount')} <em class="required">*</em> {#if dot('amount', amount)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.amount)}" data-tip="{Math.round(fieldConfidence.amount * 100)}% — {confidenceLabel(fieldConfidence.amount)}"></span>{/if}</span>
							<input type="number" step="0.01" bind:value={amount} required />
						</label>
						<label>
							<span>{m('invoices.modal.field.dueDate')} {#if dot('due_date', due_date)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.due_date)}" data-tip="{Math.round(fieldConfidence.due_date * 100)}% — {confidenceLabel(fieldConfidence.due_date)}"></span>{/if}</span>
							<input type="date" bind:value={due_date} />
						</label>
						<label>
							<span>{m('invoices.modal.field.poNumber')} {#if dot('po_number', po_number)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.po_number)}" data-tip="{Math.round(fieldConfidence.po_number * 100)}% — {confidenceLabel(fieldConfidence.po_number)}"></span>{/if}</span>
							<input type="text" bind:value={po_number} />
						</label>
						{#if !isClerkOnly}
							<label>
								<span>{m('invoices.modal.field.status')}</span>
								<select bind:value={status}>
									{#each INVOICE_STATUSES as s}
										<option value={s}>{m(INVOICE_STATUS_LABEL_KEYS[s])}</option>
									{/each}
								</select>
							</label>
						{:else}
							<label>
								<span>{m('invoices.modal.field.status')}</span>
								<input type="text" value={m(INVOICE_STATUS_LABEL_KEYS[status])} disabled />
							</label>
						{/if}
						<label>
							<span>{m('invoices.modal.field.referenceNumber')} {#if dot('reference_number', reference_number)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.reference_number)}" data-tip="{Math.round(fieldConfidence.reference_number * 100)}% — {confidenceLabel(fieldConfidence.reference_number)}"></span>{/if}</span>
							<input type="text" bind:value={reference_number} />
						</label>
						<label>
							<span>{m('invoices.modal.field.paymentMethod')} {#if dot('payment_method', payment_method)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.payment_method)}" data-tip="{Math.round(fieldConfidence.payment_method * 100)}% — {confidenceLabel(fieldConfidence.payment_method)}"></span>{/if}</span>
							<select bind:value={payment_method}>
								<option value="">—</option>
								<option value="ach">{m('invoices.modal.method.ach')}</option>
								<option value="wire">{m('invoices.modal.method.wire')}</option>
								<option value="check">{m('invoices.modal.method.check')}</option>
								<option value="credit_card">{m('invoices.modal.method.creditCard')}</option>
								<option value="other">{m('invoices.modal.method.other')}</option>
							</select>
						</label>
						<label>
							<span>{m('invoices.modal.field.taxRate')} {#if dot('tax_rate', tax_rate)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.tax_rate)}" data-tip="{Math.round(fieldConfidence.tax_rate * 100)}% — {confidenceLabel(fieldConfidence.tax_rate)}"></span>{/if}</span>
							<input type="number" step="0.01" min="0" max="100" bind:value={tax_rate} />
						</label>
						<label>
							<span>{m('invoices.modal.field.vendorTaxId')} {#if dot('vendor_tax_id', vendor_tax_id)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.vendor_tax_id)}" data-tip="{Math.round(fieldConfidence.vendor_tax_id * 100)}% — {confidenceLabel(fieldConfidence.vendor_tax_id)}"></span>{/if}</span>
							<input type="text" bind:value={vendor_tax_id} placeholder={m('invoices.modal.field.vendorTaxIdPlaceholder')} />
						</label>
						<label class="full-width">
							<span>{m('invoices.modal.field.vendorAddress')} {#if dot('vendor_address', vendor_address)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.vendor_address)}" data-tip="{Math.round(fieldConfidence.vendor_address * 100)}% — {confidenceLabel(fieldConfidence.vendor_address)}"></span>{/if}</span>
							<input type="text" bind:value={vendor_address} />
						</label>
						<label class="full-width">
							<span>{m('invoices.modal.field.shipToAddress')} {#if dot('ship_to_address', ship_to_address)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.ship_to_address)}" data-tip="{Math.round(fieldConfidence.ship_to_address * 100)}% — {confidenceLabel(fieldConfidence.ship_to_address)}"></span>{/if}</span>
							<input type="text" bind:value={ship_to_address} />
						</label>
						<label class="full-width">
							<span>{m('invoices.modal.field.description')} {#if dot('description', description)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.description)}" data-tip="{Math.round(fieldConfidence.description * 100)}% — {confidenceLabel(fieldConfidence.description)}"></span>{/if}</span>
							<input type="text" bind:value={description} />
						</label>
						<label>
							<span>{m('invoices.modal.field.glAccount')} {#if dot('suggested_gl_account', gl_account)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.suggested_gl_account)}" data-tip="{Math.round(fieldConfidence.suggested_gl_account * 100)}% — {confidenceLabel(fieldConfidence.suggested_gl_account)}"></span>{/if}</span>
							{#if glAccounts.length > 0}
								<select bind:value={gl_account}>
									<option value="">{m('invoices.modal.field.glSelect')}</option>
									<!-- A value the catalog doesn't carry (an older code, or an
									     applied history suggestion coded before the account was
									     retired) still has to be visible: without its own option
									     the select renders blank and the field silently reads as
									     unset while the form still holds it. -->
									{#if gl_account && !glAccounts.some((a) => a.code === gl_account)}
										<option value={gl_account}>{gl_account}</option>
									{/if}
									<!-- Keyed by `id`, not `code`: two subsidiaries may each
									     hold their own `6000` in the consolidated view. -->
									{#each glAccounts as acct (acct.id)}
										<option value={acct.code}>{glLabel(acct, true)}</option>
									{/each}
								</select>
							{:else}
								<input type="text" bind:value={gl_account} placeholder={m('invoices.modal.field.glPlaceholder')} />
							{/if}
						</label>
						<label>
							<span>{m('invoices.modal.field.costCenter')} {#if dot('suggested_cost_center', cost_center)}<span class="confidence-dot" style="background:{confidenceColor(fieldConfidence.suggested_cost_center)}" data-tip="{Math.round(fieldConfidence.suggested_cost_center * 100)}% — {confidenceLabel(fieldConfidence.suggested_cost_center)}"></span>{/if}</span>
							<input type="text" bind:value={cost_center} placeholder={m('invoices.modal.field.costCenterPlaceholder')} />
						</label>
						<label>
							<span>{m('invoices.modal.field.paymentTerms')}</span>
							<input type="text" bind:value={payment_terms} placeholder={m('invoices.modal.field.paymentTermsPlaceholder')} />
						</label>
						<label>
							<span>{m('invoices.modal.field.department')}</span>
							<input type="text" bind:value={department} placeholder={m('invoices.modal.field.departmentPlaceholder')} />
						</label>
						<label>
							<span>{m('invoices.modal.field.project')}</span>
							<input type="text" bind:value={project} placeholder={m('invoices.modal.field.projectPlaceholder')} />
						</label>
					</div>

					{#if codingSuggestions.length > 0 || appliedSuggestionFields.length > 0}
						<section class="coding-suggestions" data-testid="coding-suggestions" aria-label={m('invoices.modal.suggestions.aria')}>
							<div class="coding-suggestions-head">
								<span class="coding-suggestions-title">{m('invoices.modal.suggestions.title')}</span>
							</div>
							<p class="coding-suggestions-hint">{m('invoices.modal.suggestions.hint')}</p>
							{#each codingSuggestions as s (s.field)}
								<div class="coding-suggestion" data-testid="coding-suggestion-{s.field}">
									<div class="coding-suggestion-main">
										<span class="coding-suggestion-field">{suggestionFieldLabel(s.field)}</span>
										<span class="coding-suggestion-value">{s.value}</span>
									</div>
									<!-- Provenance, built from the response's own counts rather
									     than restating the backend's English sentence, so it
									     localizes. Without it this is an unexplained value
									     asking to be trusted. -->
									<p class="coding-suggestion-why">
										{m('invoices.modal.suggestions.provenance', {
											occurrences: s.occurrences,
											sample: s.sample_size,
											confidence: s.confidence,
										})}
										{#if s.runner_up}
											<span class="coding-suggestion-runner">
												{m('invoices.modal.suggestions.runnerUp', { value: s.runner_up })}
											</span>
										{/if}
									</p>
									<button
										type="button"
										class="coding-suggestion-apply"
										onclick={() => applySuggestion(s)}
									>
										{m('invoices.modal.suggestions.apply')}
									</button>
								</div>
							{/each}
							{#if appliedSuggestionFields.length > 0}
								<p class="coding-suggestion-applied" data-testid="coding-suggestion-applied">
									{m('invoices.modal.suggestions.appliedNotSaved', {
										fields: formatList(appliedSuggestionFields.map(suggestionFieldLabel)),
									})}
								</p>
							{/if}
						</section>
					{/if}

					<div class="line-items-section">
						<div class="line-items-header">
							<span class="line-items-title">{m('invoices.modal.lineItems.title')}</span>
							<button type="button" class="btn-add-line" onclick={addLineItem}>{m('invoices.modal.lineItems.addLine')}</button>
						</div>
						{#if lineItems.length > 0}
							<div class="line-items-scroll">
							<table class="line-items-table">
								<thead>
									<tr>
										<th>#</th>
										<th>{m('invoices.modal.lineItems.colDescription')}</th>
										<th class="right">{m('invoices.modal.lineItems.colQty')}</th>
										<th class="right">{m('invoices.modal.lineItems.colUnitPrice')}</th>
										<th class="right">{m('invoices.modal.lineItems.colTax')}</th>
										<th class="right">{m('invoices.modal.lineItems.colTotal')}</th>
										<th>{m('invoices.modal.lineItems.colGl')}</th>
										<th></th>
									</tr>
								</thead>
								<tbody>
									{#each lineItems as li, idx}
										<tr>
											<td class="li-num">{idx + 1}</td>
											<td><input type="text" class="li-input" aria-label={m('invoices.modal.lineItems.descriptionAria', { n: idx + 1 })} value={li.description ?? ''} oninput={(e) => updateLineItem(idx, 'description', e.currentTarget.value)} /></td>
											<td><input type="number" class="li-input right" step="0.01" aria-label={m('invoices.modal.lineItems.quantityAria', { n: idx + 1 })} value={li.quantity ?? ''} oninput={(e) => updateLineItem(idx, 'quantity', e.currentTarget.value ? parseFloat(e.currentTarget.value) : null)} /></td>
											<td><input type="number" class="li-input right" step="0.01" aria-label={m('invoices.modal.lineItems.unitPriceAria', { n: idx + 1 })} value={li.unit_price ?? ''} oninput={(e) => updateLineItem(idx, 'unit_price', e.currentTarget.value ? parseFloat(e.currentTarget.value) : null)} /></td>
											<td><input type="number" class="li-input right" step="0.01" aria-label={m('invoices.modal.lineItems.taxAria', { n: idx + 1 })} value={li.tax ?? ''} oninput={(e) => updateLineItem(idx, 'tax', e.currentTarget.value ? parseFloat(e.currentTarget.value) : null)} /></td>
											<td><input type="number" class="li-input right" step="0.01" aria-label={m('invoices.modal.lineItems.totalAria', { n: idx + 1 })} value={li.total ?? ''} oninput={(e) => updateLineItem(idx, 'total', e.currentTarget.value ? parseFloat(e.currentTarget.value) : null)} /></td>
											<td>
											{#if glAccounts.length > 0}
												<select class="li-input li-gl" aria-label={m('invoices.modal.lineItems.glAria', { n: idx + 1 })} value={li.gl_account ?? ''} onchange={(e) => updateLineItem(idx, 'gl_account', e.currentTarget.value)}>
													<option value="">—</option>
													{#each glAccounts as acct (acct.id)}
														<option value={acct.code}>{glLabel(acct, false)}</option>
													{/each}
												</select>
											{:else}
												<input type="text" class="li-input li-gl" aria-label={m('invoices.modal.lineItems.glAria', { n: idx + 1 })} value={li.gl_account ?? ''} oninput={(e) => updateLineItem(idx, 'gl_account', e.currentTarget.value)} />
											{/if}
										</td>
											<td>
												<button type="button" class="li-delete" aria-label={m('invoices.modal.lineItems.removeAria', { n: idx + 1 })} onclick={() => removeLineItem(idx)}>&times;</button>
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
							</div>
						{:else}
							<p class="line-items-empty">{m('invoices.modal.lineItems.empty')}</p>
						{/if}
						{#if lineItemsDirty}
							<div class="line-items-actions">
								<button type="button" class="btn-save-lines" disabled={savingLines} onclick={saveLineItems}>
									{savingLines ? m('invoices.modal.lineItems.saving') : m('invoices.modal.lineItems.save')}
								</button>
							</div>
						{/if}
						{#if lineTotalMismatch}
							<!-- role="alert" so the divergence is announced the moment the
							     save returns; the heading + the "cannot enter a payment run"
							     line carry the meaning in text, never by colour alone. -->
							<div class="line-total-mismatch" role="alert" data-testid="line-total-mismatch">
								<div class="ltm-head">
									<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
										<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
									</svg>
									<span class="ltm-title">{m('invoices.modal.lineItems.mismatchTitle')}</span>
								</div>
								<dl class="ltm-figures">
									<div>
										<dt>{m('invoices.modal.lineItems.mismatchLinesLabel')}</dt>
										<dd><Money amount={lineTotalMismatch.line_items_total} currency={invoice.currency} mono /></dd>
									</div>
									<div>
										<dt>{m('invoices.modal.lineItems.mismatchHeaderLabel')}</dt>
										<dd><Money amount={lineTotalMismatch.header_amount} currency={invoice.currency} mono /></dd>
									</div>
								</dl>
								<p class="ltm-blocking">{m('invoices.modal.lineItems.mismatchBlocking')}</p>
								<p class="ltm-hint">{m('invoices.modal.lineItems.mismatchHint')}</p>
							</div>
						{/if}
					</div>

					<div class="contract-section">
						<span class="contract-label">{m('invoices.modal.contract.label')}</span>
						{#if contractId}
							<span class="contract-linked mono">{linkedContract?.contract_number ?? m('invoices.modal.contract.linked')}</span>
							{#if !isClerkOnly}
								<button type="button" class="btn-contract-unlink" disabled={linkingContract} onclick={unlinkContract}>
									{linkingContract ? '…' : m('invoices.modal.contract.unlink')}
								</button>
							{/if}
						{:else if isClerkOnly}
							<span class="contract-empty">{m('invoices.modal.contract.empty')}</span>
						{:else}
							<select class="contract-select" aria-label={m('invoices.modal.contract.selectAria')} bind:value={pickContractId}>
								<option value="">{m('invoices.modal.contract.selectPlaceholder')}</option>
								{#each contracts as c (c.id)}
									<option value={c.id}>{c.contract_number}{c.vendor_name ? ` — ${c.vendor_name}` : ''}</option>
								{/each}
							</select>
							<button type="button" class="btn-contract-link" disabled={linkingContract || !pickContractId} onclick={linkContract}>
								{linkingContract ? '…' : m('invoices.modal.contract.link')}
							</button>
						{/if}
					</div>

					{#if invoice.warnings?.filter(w => w.type !== 'missing_field' && w.type !== 'po_mismatch').length}
						<div class="warnings-list">
							{#each invoice.warnings.filter(w => w.type !== 'missing_field' && w.type !== 'po_mismatch') as w}
								<div class="warning-item {w.severity}">
									<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
										<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
								</svg>
								<!-- Same resolver as the `/invoices` row icon, so a finding cannot
								     read as German in the list and English in the tooltip. -->
								{invoiceWarningText(w, m)}
							</div>
							{/each}
						</div>
					{/if}

					{#if invoice.po_match}
						{@const pm = invoice.po_match}
						<div class="po-match {pm.status}">
							<div class="po-match-header">
								<span class="po-match-title">{m('invoices.modal.poMatch.title')}</span>
								<span class="po-match-status {pm.status}">
									{#if pm.status === 'matched'}{m('invoices.modal.poMatch.matched')}
									{:else if pm.status === 'mismatch'}{m('invoices.modal.poMatch.mismatch')}
									{:else if pm.status === 'partial'}{m('invoices.modal.poMatch.partialReceipt')}
									{:else}{m('invoices.modal.poMatch.notFound')}
									{/if}
								</span>
								{#if pm.match_type !== 'none'}
									<span class="po-match-type">{pm.match_type}</span>
								{/if}
							</div>
							{#if pm.po_number}
								<div class="po-match-grid">
									<div>
										<span class="po-match-label">{m('invoices.modal.poMatch.poNumber')}</span>
										<span class="po-match-value mono">{pm.po_number}</span>
									</div>
									{#if pm.po_total !== null}
										<div>
											<span class="po-match-label">{m('invoices.modal.poMatch.poTotal')}</span>
											<span class="po-match-value mono">{formatMoney(pm.po_total, { currency: invoice.currency })}</span>
										</div>
									{/if}
									{#if isPositiveAmount(pm.amount_variance) || isNegativeAmount(pm.amount_variance)}
										<div>
											<span class="po-match-label">{m('invoices.modal.poMatch.variance')}</span>
											<span
												class="po-match-value mono"
												class:variance-pos={isPositiveAmount(pm.amount_variance)}
												class:variance-neg={isNegativeAmount(pm.amount_variance)}
											>
												{isPositiveAmount(pm.amount_variance) ? '+' : ''}{formatMoney(pm.amount_variance, { currency: invoice.currency })}
												({pm.amount_variance_pct > 0 ? '+' : ''}{pm.amount_variance_pct.toFixed(1)}%)
											</span>
										</div>
									{/if}
								</div>
							{/if}
							{#if pm.match_type === '4-way' || pm.inspection_result || pm.inspection_required}
								<div class="po-match-inspection">
									<span class="po-match-label">{m('invoices.modal.poMatch.qualityInspection')}</span>
									{#if pm.inspection_result}
										<Badge tone={INSPECTION_TONES[pm.inspection_result]} variant={pm.inspection_result}>
											{#if pm.inspection_result === 'pass'}{m('invoices.modal.poMatch.passed')}
											{:else if pm.inspection_result === 'fail'}{m('invoices.modal.poMatch.failed')}
											{:else}{m('invoices.modal.poMatch.partial')}
											{/if}
										</Badge>
										{#if pm.inspection_accepted_quantity !== null}
											<span class="po-match-value mono">
												{m('invoices.modal.poMatch.accepted', { qty: pm.inspection_accepted_quantity })}
											</span>
										{/if}
									{:else if pm.inspection_required}
										<Badge tone="danger" variant="missing">{m('invoices.modal.poMatch.requiredMissing')}</Badge>
									{/if}
								</div>
							{/if}
							{#if pm.issues.length > 0}
								<ul class="po-match-issues">
									{#each pm.issues as issue}
										<li>{issue}</li>
									{/each}
								</ul>
							{/if}
						</div>
					{/if}

					{#if canSubmitStatus && missingFields.length > 0}
						<div class="validation-hint">{m('invoices.modal.requiredHint', { fields: formatList(missingFields) })}</div>
					{/if}

					{#if invoice.approved_by || invoice.rejected_by || invoice.assigned_to}
						<div class="meta-section">
							{#if invoice.assigned_to}
								<div class="meta-item">
									<span class="meta-label">{m('invoices.modal.meta.assignedTo')}</span>
									<span class="meta-value">{invoice.assigned_to}</span>
								</div>
							{/if}
							{#if invoice.approved_by}
								<div class="meta-item">
									<span class="meta-label">{m('invoices.modal.meta.approvedBy')}</span>
									<span class="meta-value approved">{invoice.approved_by}</span>
									{#if invoice.approval_date}
										<span class="meta-date">{invoice.approval_date}</span>
									{/if}
								</div>
							{/if}
							{#if invoice.rejected_by}
								<div class="meta-item">
									<span class="meta-label">{m('invoices.modal.meta.rejectedBy')}</span>
									<span class="meta-value rejected">{invoice.rejected_by}</span>
								</div>
							{/if}
						</div>
					{/if}

					{#if chainLevels.length > 0}
						<ApprovalChainProgress
							levels={chainLevels}
							currentLevel={chainCurrentLevel}
							{resolveApproverName}
							statusLabel={chainStatusLabel}
							formatProgress={chainProgressLabel}
							anyApproverLabel={m('invoices.modal.chain.anyApprover')}
							title={m('invoices.modal.chain.title')}
						/>
					{/if}

					{#if isErpStatus || (status === 'failed' && erpInfo)}
						<div class="erp-section">
							<div class="erp-title">{m('invoices.modal.erp.title')}</div>
							<div class="erp-details">
								{#if erpInfo?.erp_reference}
									<div class="erp-row">
										<span class="erp-label">{m('invoices.modal.erp.reference')}</span>
										<code class="erp-value">{erpInfo.erp_reference}</code>
									</div>
								{/if}
								{#if erpInfo?.erp_document_id}
									<div class="erp-row">
										<span class="erp-label">{m('invoices.modal.erp.documentId')}</span>
										<code class="erp-value">{erpInfo.erp_document_id}</code>
									</div>
								{/if}
								{#if erpInfo?.last_error}
									<div class="erp-row erp-error">
										<span class="erp-label">{m('invoices.modal.erp.error')}</span>
										<span class="erp-value">{erpInfo.last_error}</span>
									</div>
								{/if}
							</div>
							{#if canRetryErp}
								<button type="button" class="btn-retry-erp" disabled={retryingErp} onclick={handleRetryErp}>
									<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
									{retryingErp ? m('invoices.modal.erp.retrying') : m('invoices.modal.erp.retrySend')}
								</button>
							{/if}
						</div>
					{/if}

					{#if canRouteIntercompany && entityStore.multiEntity}
						<section class="ic-section" data-testid="intercompany">
							<div class="ic-title">{m('invoices.modal.intercompany.title')}</div>
							{#if mirrorInvoiceId}
								<!-- Already routed. The pairing is settled: the backend only
								     stamps a counterparty while unrouted and returns the same
								     mirror on a repeat call, so there is no action left to
								     offer here — only the state. -->
								<p class="ic-routed" data-testid="intercompany-routed">
									{m('invoices.modal.intercompany.routed', {
										entity: entityName(counterpartyEntityId),
									})}
								</p>
								<p class="ic-mirror">
									{m('invoices.modal.intercompany.mirrorId')}
									<code class="ic-mirror-id" data-testid="intercompany-mirror-id">{mirrorInvoiceId}</code>
								</p>
							{:else}
								<p class="ic-hint">{m('invoices.modal.intercompany.hint')}</p>
								<label class="ic-picker">
									<span>{m('invoices.modal.intercompany.counterparty')}</span>
									<select
										bind:value={selectedCounterparty}
										onchange={() => { icArmed = false; icError = ''; }}
										data-testid="intercompany-counterparty"
									>
										<option value="">{m('invoices.modal.intercompany.selectEntity')}</option>
										{#each counterpartyOptions as e (e.id)}
											<option value={e.id}>{e.name}</option>
										{/each}
									</select>
								</label>
								<div class="ic-actions">
									<RowAction
										variant="danger"
										armed={icArmed}
										disabled={icBusy || !selectedCounterparty}
										onclick={handleRouteIntercompany}
									>
										{#if icBusy}
											{m('invoices.modal.intercompany.routing')}
										{:else if icArmed}
											{m('invoices.modal.intercompany.confirm', {
												entity: entityName(selectedCounterparty),
											})}
										{:else}
											{m('invoices.modal.intercompany.route')}
										{/if}
									</RowAction>
									{#if icArmed}
										<button type="button" class="ic-cancel" onclick={() => (icArmed = false)}>
											{m('common.cancel')}
										</button>
									{/if}
								</div>
								{#if icError}
									<p class="ic-error" data-testid="intercompany-error">{icError}</p>
								{/if}
							{/if}
						</section>
					{/if}

					{#if priors.vendor_cache_applied.length > 0 || priors.rag_neighbors.length > 0}
						<div class="priors-section">
							<button
								type="button"
								class="priors-toggle"
								onclick={() => (priorsOpen = !priorsOpen)}
								aria-expanded={priorsOpen}
							>
								<span class="priors-title">{m('invoices.modal.priors.title')}</span>
								<span class="priors-chips">
									{#if priors.vendor_cache_applied.length > 0}
										<Badge tone="accent" variant="priors-chip">{m('invoices.modal.priors.vendorCacheChip', { count: priors.vendor_cache_applied.length })}</Badge>
									{/if}
									{#if priors.rag_neighbors.length > 0}
										<Badge tone="accent" variant="priors-chip">{m('invoices.modal.priors.ragChip', { count: priors.rag_neighbors.length })}</Badge>
									{/if}
								</span>
								<span class="priors-caret">{priorsOpen ? '▾' : '▸'}</span>
							</button>

							{#if priorsOpen}
								<div class="priors-body">
									{#if priors.vendor_cache_applied.length > 0}
										<div class="priors-group">
											<div class="priors-group-title">{m('invoices.modal.priors.cacheGroupTitle')}</div>
											<div class="priors-tags">
												{#each priors.vendor_cache_applied as field}
													<span class="priors-tag">{field}</span>
												{/each}
											</div>
											<div class="priors-help">
												{m('invoices.modal.priors.cacheHelp')}
											</div>
										</div>
									{/if}

									{#if priors.rag_neighbors.length > 0}
										<div class="priors-group">
											<div class="priors-group-title">{m('invoices.modal.priors.ragGroupTitle')}</div>
											<table class="priors-table">
												<thead>
													<tr>
														<th>{m('invoices.modal.priors.colSimilarity')}</th>
														<th>{m('invoices.modal.priors.colVendor')}</th>
														<th>{m('invoices.modal.priors.colInvoice')}</th>
														<th>{m('invoices.modal.priors.colAmount')}</th>
													</tr>
												</thead>
												<tbody>
													{#each priors.rag_neighbors as n}
														<tr>
															<td>{Math.round(n.similarity * 100)}%</td>
															<td>{n.vendor_name ?? '—'}</td>
															<td>{n.invoice_number ?? '—'}</td>
															<td>{formatMoney(n.amount, { currency: invoice.currency })}</td>
														</tr>
													{/each}
												</tbody>
											</table>
											<div class="priors-help">
												{m('invoices.modal.priors.ragHelp')}
											</div>
										</div>
									{/if}
								</div>
							{/if}
						</div>
					{/if}

					{#if auditLog.length > 0}
						<div class="activity-section">
							<div class="activity-title">{m('invoices.modal.activity.title')}</div>
							<div class="activity-list">
								{#each auditLog as entry}
									<div class="activity-item">
										<div class="activity-dot"></div>
										<div class="activity-content">
											<span class="activity-action">{actionLabel(entry.action)}</span>
											{#if entry.actor_name}
												<span class="activity-actor">{m('invoices.modal.activity.by', { actor: entry.actor_name })}</span>
											{/if}
											{#if entry.details?.reason}
												<span class="activity-detail">— {entry.details.reason}</span>
											{/if}
											{#if entry.action === 'invoice.extraction_completed' && entry.details?.confidence}
												<span class="activity-detail">{m('invoices.modal.activity.confidence', { method: String(entry.details.method), pct: Math.round((entry.details.confidence as number) * 100) })}</span>
											{/if}
											{#if entry.action === 'invoice.extraction_completed' && entry.details?.gl_suggested}
												<span class="activity-detail">{m('invoices.modal.activity.glSuggested', { gl: String(entry.details.gl_suggested) })}</span>
											{/if}
											{#if entry.action === 'invoice.extraction_completed' && entry.details?.vendor_action === 'created'}
												<span class="activity-detail">{m('invoices.modal.activity.newVendor')}</span>
											{/if}
											{#if entry.action === 'invoice.extraction_failed' && entry.details?.error}
												<span class="activity-detail error">— {entry.details.error}</span>
											{/if}
											{#if fieldChanges(entry).length > 0}
												<ul class="activity-changes">
													{#each fieldChanges(entry) as [field, change] (field)}
														<li>
															<span class="change-field">{fieldLabel(field)}:</span>
															<span class="change-old">{change.old ?? '—'}</span>
															<span class="change-arrow">→</span>
															<span class="change-new">{change.new ?? '—'}</span>
														</li>
													{/each}
												</ul>
											{/if}
										</div>
										<span class="activity-time">{formatAuditDate(entry.created_at)}</span>
									</div>
								{/each}
							</div>
						</div>
					{/if}

					<div class="chat-section">
						<SupplierChatThread
							surface="ap"
							thread={chatThread}
							currentUserId={auth.user?.id}
							members={chatMembers}
							templates={chatTemplates}
							loading={chatLoading}
							onsend={handleChatSend}
							onresolve={handleChatResolve}
							onreopen={handleChatReopen}
							ondownload={handleChatDownload}
						/>
					</div>

					<!-- Structured e-invoicing. Distinct from the invoice's own source
					     document (the PDF in the left pane): this GENERATES a
					     standards-compliant XML from the invoice's data, in the dialect
					     the receiver's jurisdiction expects. -->
					<div class="einvoice-section" data-testid="einvoice-section">
						<div class="review-title">{m('invoices.modal.einvoice.title')}</div>
						<p class="einvoice-hint">{m('invoices.modal.einvoice.hint')}</p>

						<div class="export-wrapper">
							<button
								type="button"
								class="btn-export"
								data-testid="einvoice-menu-toggle"
								aria-haspopup="true"
								aria-expanded={showEinvoiceMenu}
								disabled={einvoiceBusy !== null}
								onclick={() => (showEinvoiceMenu = !showEinvoiceMenu)}
							>
								{einvoiceBusy
									? m('invoices.modal.einvoice.preparing')
									: m('invoices.modal.einvoice.download')}
							</button>
							{#if showEinvoiceMenu}
								<div
									class="export-menu"
									data-testid="einvoice-menu"
									role="group"
									aria-label={m('invoices.modal.einvoice.download')}
								>
									{#each E_INVOICE_FORMATS as opt (opt.format)}
										<button
											type="button"
											data-testid="einvoice-format-{opt.format}"
											onclick={() => handleEinvoiceDownload(opt.format)}
										>
											{m(opt.labelKey)}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						{#if einvoiceError}
							<div class="einvoice-error" role="alert" data-testid="einvoice-error">
								<p class="einvoice-error-title">
									{einvoiceError.invalid
										? m('invoices.modal.einvoice.invalidTitle', {
												format: einvoiceFormatLabel(einvoiceError.format)
											})
										: m('invoices.modal.einvoice.failed', {
												format: einvoiceFormatLabel(einvoiceError.format)
											})}
								</p>
								{#if einvoiceError.issues.length > 0}
									<EInvoiceIssueList issues={einvoiceError.issues} />
								{:else if einvoiceError.detail}
									<p class="einvoice-error-detail">{einvoiceError.detail}</p>
								{/if}
							</div>
						{/if}

						{#if canSendPeppol}
							<div class="peppol-block">
								<div class="peppol-title">{m('invoices.modal.peppol.title')}</div>
								{#if !peppolStatusReady}
									<p class="einvoice-hint" data-testid="peppol-not-sendable">
										{m('invoices.modal.peppol.notSendable')}
									</p>
								{:else if !peppolResult}
									<!--
										The transmission state on OPEN, from the invoice's own
										transmission log. Four distinct states — a read that
										failed says so rather than degrading into "not yet
										transmitted", and attempts that all FAILED are re-sendable
										(the backend's partial unique index excludes them), so
										they must not read as done.
									-->
									<p class="peppol-state" data-testid="peppol-state">
										{#if peppolTransmissionsErrored}
											{m('invoices.modal.peppol.state.unknown')}
										{:else if peppolTransmissions === null}
											{m('invoices.modal.peppol.state.loading')}
										{:else if peppolLive}
											{m('invoices.modal.peppol.state.transmitted', {
												date: formatAuditDate(peppolLive.transmitted_at ?? peppolLive.created_at)
											})}
											{#if peppolLive.message_id}
												<span class="peppol-msgid"
													>{m('invoices.modal.peppol.messageId', {
														id: peppolLive.message_id
													})}</span
												>
											{/if}
										{:else if peppolTransmissions.length > 0}
											{m('invoices.modal.peppol.state.failedOnly')}
										{:else}
											{m('invoices.modal.peppol.state.none')}
										{/if}
									</p>
								{/if}
								{#if !peppolStatusReady}
									<!-- The send controls stay behind the same gate the backend
									     enforces; only the state line above is independent of it. -->
								{:else if peppolResult}
									<p class="peppol-result" data-testid="peppol-result">
										{peppolResult.already_sent
											? m('invoices.modal.peppol.alreadySent')
											: m('invoices.modal.peppol.sent')}
										{#if peppolResult.message_id}
											<span class="peppol-msgid"
												>{m('invoices.modal.peppol.messageId', {
													id: peppolResult.message_id
												})}</span
											>
										{/if}
									</p>
								{:else if showPeppolForm}
									<div class="peppol-form" data-testid="peppol-form">
										<p class="einvoice-hint">{m('invoices.modal.peppol.hint')}</p>
										<label class="peppol-field">
											<span>{m('invoices.modal.peppol.receiverScheme')}</span>
											<input
												type="text"
												data-testid="peppol-receiver-scheme"
												bind:value={peppolReceiverScheme}
												placeholder="9930"
											/>
										</label>
										<label class="peppol-field">
											<span>{m('invoices.modal.peppol.receiverValue')}</span>
											<input
												type="text"
												data-testid="peppol-receiver-value"
												bind:value={peppolReceiverValue}
											/>
										</label>
										<p class="einvoice-hint">{m('invoices.modal.peppol.idempotentNote')}</p>
										<div class="reject-actions">
											<button
												type="button"
												class="btn-cancel-sm"
												onclick={() => {
													showPeppolForm = false;
													peppolError = null;
												}}>{m('common.cancel')}</button
											>
											<button
												type="button"
												class="btn-peppol-confirm"
												data-testid="peppol-confirm"
												disabled={peppolSending ||
													!peppolReceiverScheme.trim() ||
													!peppolReceiverValue.trim()}
												onclick={handlePeppolSend}
											>
												{peppolSending
													? m('invoices.modal.peppol.sending')
													: m('invoices.modal.peppol.confirm')}
											</button>
										</div>
									</div>
								{:else}
									<button
										type="button"
										class="btn-export"
										data-testid="peppol-send"
										onclick={() => {
											showPeppolForm = true;
											peppolError = null;
										}}
									>
										{m('invoices.modal.peppol.send')}
									</button>
								{/if}
								{#if peppolError}
									<div class="einvoice-error" role="alert" data-testid="peppol-error">
										<p class="einvoice-error-title">{m('invoices.modal.peppol.failed')}</p>
										{#if peppolError.issues.length > 0}
											<EInvoiceIssueList issues={peppolError.issues} />
										{:else}
											<p class="einvoice-error-detail">{peppolError.detail}</p>
										{/if}
									</div>
								{/if}
							</div>
						{/if}
					</div>

					{#if canReview}
						<div class="review-section">
							<div class="review-title">{m('invoices.modal.review.title')}</div>
							{#if showRejectForm}
								<div class="reject-form">
									<textarea
										class="reject-input"
										placeholder={m('invoices.modal.review.rejectPlaceholder')}
										aria-label={m('invoices.modal.review.rejectAria')}
										bind:value={rejectReason}
										rows="2"
									></textarea>
									<div class="reject-actions">
										<button type="button" class="btn-cancel-sm" onclick={() => { showRejectForm = false; rejectReason = ''; }}>{m('common.cancel')}</button>
										<button type="button" class="btn-reject" disabled={reviewing || !rejectReason.trim()} onclick={handleReject}>
											{reviewing ? m('invoices.modal.review.rejecting') : m('invoices.modal.review.confirmReject')}
										</button>
									</div>
								</div>
							{:else}
								<div class="review-actions">
									<button type="button" class="btn-approve" disabled={reviewing} onclick={handleApprove}>
										<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
										{reviewing ? m('invoices.modal.review.approving') : m('invoices.modal.review.approve')}
									</button>
									<button type="button" class="btn-reject-outline" disabled={reviewing} onclick={() => (showRejectForm = true)}>
										<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
										{m('invoices.modal.review.reject')}
									</button>
								</div>
							{/if}
						</div>
					{:else if isReadyForReview && invoice.assigned_to}
						<div class="review-section">
							<p class="review-assigned-hint">{m('invoices.modal.review.assignedHintPre')}<strong>{invoice.assigned_to}</strong>{m('invoices.modal.review.assignedHintPost')}</p>
						</div>
					{/if}

					<footer>
						<div class="footer-right">
							<button type="button" class="btn-cancel" onclick={onclose}>{m('common.cancel')}</button>
							{#if canExtract || extracting}
								<button type="button" class="btn-extract" disabled={extracting} onclick={handleExtract}>
									{#if extracting}
										<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="10" stroke-dasharray="31.4" stroke-dashoffset="10"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></circle></svg>
										{extractionStatus || m('invoices.modal.extracting')}
									{:else}
										<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="9" y1="15" x2="15" y2="15"/></svg>
										{status === 'failed' ? m('invoices.modal.reExtract') : m('invoices.modal.extract')}
									{/if}
								</button>
							{/if}
							{#if isExtracting && !extracting}
								<button type="button" class="btn-reset" disabled={resettingExtraction} onclick={handleResetExtraction}>
									{resettingExtraction ? m('invoices.modal.resetting') : m('invoices.modal.reset')}
								</button>
							{/if}
							{#if !isDone}
								<button type="submit" class="btn-save" disabled={saving}>
									{saving ? m('common.saving') : m('common.save')}
								</button>
							{/if}
							{#if canSubmit}
								{#if needsApproverSelect && reviewerOptions.length > 0}
									<select class="approver-select" aria-label={m('invoices.modal.assignApprover')} bind:value={selectedApproverId}>
										<option value="">{m('invoices.modal.approverPlaceholder')}</option>
										{#each reviewerOptions as user}
											<option value={user.id}>{user.full_name}</option>
										{/each}
									</select>
								{:else if approverNote}
									<!-- No picker to offer. Say what Submit will do instead of
									     leaving a disabled button with no explanation. -->
									<p class="approver-note">{approverNote}</p>
								{/if}
								<button type="button" class="btn-submit" disabled={submitting || (approverRequired && !selectedApproverId)} onclick={submitDone}>
									{submitting ? m('invoices.modal.submitting') : submitLabel}
								</button>
							{/if}
						</div>
					</footer>
				</form>
			</div>
		</div>
	</div>
</div>

<style>
	.backdrop {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.6);
		display: grid;
		place-items: center;
		z-index: 100;
	}

	.modal {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		width: min(1300px, 95vw);
		height: min(800px, 92vh);
		display: flex;
		flex-direction: column;
		box-shadow: 0 16px 48px rgba(0, 0, 0, 0.3);
		overflow: hidden;
		transition: width 0.2s ease, height 0.2s ease;
	}

	.modal.fullscreen {
		width: 100vw;
		height: 100vh;
		border-radius: 0;
		border: none;
	}


	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 14px 20px;
		border-bottom: 1px solid var(--border);
		flex-shrink: 0;
	}

	h2 {
		margin: 0;
		font-size: 1.05rem;
		font-weight: 600;
	}

	.header-actions {
		display: flex;
		align-items: center;
		gap: 4px;
	}

	.icon-btn {
		background: none;
		border: 1px solid transparent;
		border-radius: 4px;
		cursor: pointer;
		color: var(--text-muted);
		padding: 6px;
		display: grid;
		place-items: center;
	}

	.icon-btn:hover {
		color: var(--text);
		background: var(--bg);
		border-color: var(--border);
	}

	.close-btn {
		font-size: 1.4rem;
		line-height: 1;
		padding: 4px 6px;
	}

	/* --- Split pane --- */

	.split {
		display: flex;
		flex: 1;
		min-height: 0;
	}

	.split.resizing {
		user-select: none;
		cursor: col-resize;
	}

	/* --- PDF pane --- */

	.pdf-pane {
		flex: 1;
		border-right: 1px solid var(--border);
		background: #1a1a24;
		display: flex;
		flex-direction: column;
		contain: paint;
	}

	.pdf-pane iframe {
		width: 100%;
		height: 100%;
		flex: 1;
		min-height: 0;
		border: none;
		/* Own compositing layer — prevents the browser from repainting the
		   heavyweight PDF renderer on every scroll frame of the form pane. */
		will-change: transform;
	}

	.pdf-pane .invoice-image {
		width: 100%;
		height: 100%;
		flex: 1;
		min-height: 0;
		object-fit: contain;
		will-change: transform;
	}

	.no-pdf {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		color: var(--text-muted);
		font-size: 0.9rem;
	}

	.file-actions {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 14px;
		border-bottom: 1px solid var(--border);
		background: var(--surface);
	}

	.btn-file-action {
		padding: 5px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-file-action:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-file-action:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* --- Form pane --- */

	.form-pane {
		flex-shrink: 0;
		overflow-y: auto;
		overflow-x: hidden;
		display: flex;
		flex-direction: column;
		contain: paint;
	}

	.resize-handle {
		width: 5px;
		cursor: col-resize;
		background: transparent;
		flex-shrink: 0;
		position: relative;
		z-index: 2;
	}

	.resize-handle:hover,
	.resize-handle:active {
		background: var(--accent);
		opacity: 0.3;
	}

	form {
		padding: 20px;
		display: flex;
		flex-direction: column;
		flex: 1;
		min-width: 0;
	}

	.form-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 14px;
		min-width: 0;
	}

	.full-width {
		grid-column: 1 / -1;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		min-width: 0;
	}

	label span {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	/* Only the layout bits here; the visual recipe (border/radius/colour/
	   font/padding, + the select chevron + its 30px gutter) comes from the
	   global `.modal input` / `.modal select` rules in app.css. */
	input,
	select {
		min-width: 0;
		width: 100%;
		box-sizing: border-box;
	}

	input:focus,
	select:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	footer {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		gap: 10px;
		padding-top: 18px;
		border-top: 1px solid var(--border);
		margin-top: auto;
	}

	.footer-left {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.footer-right {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 8px;
	}

	.btn-cancel,
	.btn-save,
	.btn-submit {
		padding: 8px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		border: 1px solid var(--border);
		font-family: inherit;
		white-space: nowrap;
	}

	.btn-cancel {
		background: var(--surface);
		color: var(--text-muted);
	}

	.btn-cancel:hover {
		background: var(--bg);
	}

	.btn-delete-outline {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		white-space: nowrap;
		background: var(--surface);
		color: var(--text-muted);
		border: 1px solid var(--border);
		transition: all 0.15s;
	}

	.btn-delete-outline:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	.btn-delete-outline.armed {
		border-color: var(--danger);
		background: rgba(224, 64, 64, 0.1);
		color: var(--danger);
	}

	.btn-delete-outline:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	/* --- Line items --- */

	.line-items-section {
		margin-top: 14px;
	}

	.line-items-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 8px;
	}

	.line-items-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.btn-add-line {
		padding: 3px 10px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.75rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-add-line:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.line-items-scroll {
		width: 100%;
		overflow-x: auto;
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.line-items-table {
		width: 100%;
		min-width: 520px;
		border-collapse: collapse;
		font-size: 0.8rem;
	}

	.line-items-table th {
		background: var(--bg);
		padding: 8px 8px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-muted);
		border-bottom: 1px solid var(--border);
		text-align: left;
		white-space: nowrap;
	}

	.line-items-table td {
		padding: 3px 4px;
		border-bottom: 1px solid var(--border);
		color: var(--text);
	}

	.line-items-table tr:last-child td {
		border-bottom: none;
	}

	.line-items-table .right {
		text-align: right;
	}

	.li-num {
		text-align: center;
		color: var(--text-muted);
		font-size: 0.75rem;
		width: 28px;
	}

	.li-input {
		width: 100%;
		min-width: 0;
		box-sizing: border-box;
		padding: 5px 7px;
		border: 1px solid transparent;
		border-radius: 4px;
		background: transparent;
		font-size: 0.8rem;
		color: var(--text);
		font-family: inherit;
	}

	/* Kill the native number-input spinners — they steal ~20px per cell, which
	   in a narrow modal pane meant amounts like "$7,000.00" got clipped to "70".
	   Keep semantic type=number for keyboard + inputmode behaviour. */
	.li-input[type='number']::-webkit-inner-spin-button,
	.li-input[type='number']::-webkit-outer-spin-button {
		-webkit-appearance: none;
		margin: 0;
	}
	.li-input[type='number'] {
		-moz-appearance: textfield;
		appearance: textfield;
	}

	.li-input:focus {
		outline: none;
		border-color: var(--accent);
		background: var(--bg);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.1);
	}

	.li-input:hover:not(:focus) {
		border-color: var(--border);
		background: var(--bg);
	}

	.li-input.right {
		text-align: right;
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
	}

	.li-gl {
		min-width: 70px;
	}

	/* Fixed layout so column widths are honoured even when the modal pane
	   is narrow. Numbers get whatever they actually need to display
	   "$10,000.00" without truncation; description flexes. */
	.line-items-table {
		table-layout: fixed;
	}

	/* # */
	.line-items-table th:nth-child(1),
	.line-items-table td:nth-child(1) {
		width: 32px;
	}

	/* Description — flexes, inputs ellipsis if too long */
	.line-items-table th:nth-child(2),
	.line-items-table td:nth-child(2) {
		width: auto;
	}

	/* Qty */
	.line-items-table th:nth-child(3),
	.line-items-table td:nth-child(3) {
		width: 60px;
	}

	/* Unit Price */
	.line-items-table th:nth-child(4),
	.line-items-table td:nth-child(4) {
		width: 90px;
	}

	/* Tax */
	.line-items-table th:nth-child(5),
	.line-items-table td:nth-child(5) {
		width: 70px;
	}

	/* Total */
	.line-items-table th:nth-child(6),
	.line-items-table td:nth-child(6) {
		width: 90px;
	}

	/* GL */
	.line-items-table th:nth-child(7),
	.line-items-table td:nth-child(7) {
		width: 90px;
	}

	/* × delete button */
	.line-items-table th:nth-child(8),
	.line-items-table td:nth-child(8) {
		width: 28px;
	}

	.li-delete {
		background: none;
		border: none;
		color: var(--text-muted);
		cursor: pointer;
		font-size: 1rem;
		padding: 0 4px;
		line-height: 1;
	}

	.li-delete:hover {
		color: var(--danger);
	}

	.line-items-empty {
		font-size: 0.8rem;
		color: var(--text-muted);
		text-align: center;
		padding: 12px;
		margin: 0;
	}

	/* --- Contract link --- */
	.contract-section {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 16px;
		flex-wrap: wrap;
	}
	.contract-label {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
	}
	.contract-linked {
		font-size: 0.85rem;
		color: var(--accent);
		font-weight: 600;
	}
	.contract-empty {
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	.contract-select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		padding: 6px 30px 6px 8px;
		border-radius: 5px;
		font-size: 0.85rem;
		max-width: 280px;
	}
	.btn-contract-link,
	.btn-contract-unlink {
		padding: 5px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}
	.btn-contract-link:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-contract-unlink:hover:not(:disabled) {
		border-color: var(--danger);
		color: var(--danger);
	}
	.btn-contract-link:disabled,
	.btn-contract-unlink:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.line-items-actions {
		display: flex;
		justify-content: flex-start;
		margin-top: 8px;
	}

	/* Line-total reconciliation failure. Styled like the `error`-severity
	   warning item, but persistent + structured: it names both figures and
	   states the money consequence (no payment run) in text — the red tone is
	   reinforcement, never the sole signal (WCAG 1.4.1). */
	.line-total-mismatch {
		margin-top: 10px;
		padding: 10px 12px;
		border: 1px solid rgba(240, 70, 70, 0.45);
		border-left-width: 3px;
		border-radius: 6px;
		background: rgba(240, 70, 70, 0.07);
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.ltm-head {
		display: flex;
		align-items: center;
		gap: 6px;
		color: #f06464;
	}

	.ltm-title {
		font-size: 0.82rem;
		font-weight: 600;
	}

	.ltm-figures {
		display: flex;
		flex-wrap: wrap;
		gap: 8px 28px;
		margin: 0;
	}

	.ltm-figures div {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.ltm-figures dt {
		font-size: 0.7rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.ltm-figures dd {
		margin: 0;
		font-size: 0.9rem;
		font-weight: 600;
	}

	.ltm-blocking {
		margin: 0;
		font-size: 0.78rem;
		font-weight: 600;
	}

	.ltm-hint {
		margin: 0;
		font-size: 0.76rem;
		color: var(--text-muted);
		line-height: 1.45;
	}

	.btn-save-lines {
		padding: 5px 14px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.8rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-save-lines:hover:not(:disabled) {
		opacity: 0.85;
	}

	.btn-save-lines:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.confidence-dot {
		display: inline-block;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		margin-left: 4px;
		vertical-align: middle;
		cursor: help;
		position: relative;
		/* Expand hover area */
		padding: 4px;
		margin: -4px;
		margin-left: 0;
		background-clip: content-box;
	}

	.confidence-dot::after {
		content: attr(data-tip);
		position: absolute;
		bottom: calc(100% + 6px);
		left: 50%;
		transform: translateX(-50%);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px 8px;
		font-size: 0.72rem;
		font-weight: 500;
		color: var(--text);
		white-space: nowrap;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
		pointer-events: none;
		/* display:none instead of opacity:0 — avoids laying out ~15
		   absolute-positioned pseudo-elements during scroll, which
		   causes layout thrashing in the form pane. */
		display: none;
		z-index: 10;
	}

	.confidence-dot:hover::after {
		display: block;
	}

	.btn-reset {
		padding: 8px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		border: 1px solid #d4940a;
		font-family: inherit;
		white-space: nowrap;
		background: var(--surface);
		color: #d4940a;
	}

	.btn-reset:hover:not(:disabled) {
		background: rgba(212, 148, 10, 0.1);
	}

	.btn-reset:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.btn-extract {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		border: 1px solid var(--border);
		font-family: inherit;
		white-space: nowrap;
		background: var(--surface);
		color: var(--text-muted);
	}

	.btn-extract:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-extract:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.btn-save {
		background: var(--accent-strong);
		color: #fff;
		border-color: var(--accent-strong);
	}

	.btn-save:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-save:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.btn-submit {
		background: var(--success-strong);
		color: #fff;
		border-color: var(--success-strong);
	}

	.btn-submit:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-submit:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.approver-select {
		/* base look (border/radius/colour/font/chevron) from the global recipe */
		padding: 8px 30px 8px 10px;
		font-size: 0.82rem;
		max-width: 140px;
	}

	.approver-select:focus {
		outline: none;
		border-color: var(--accent);
	}

	/* Shown in place of the picker when there is no reviewer to offer —
	   explains what Submit will do rather than leaving a bare disabled button. */
	.approver-note {
		margin: 0;
		max-width: 320px;
		font-size: 0.78rem;
		line-height: 1.35;
		color: var(--text-muted);
	}

	/* E-invoice section + its format dropdown */
	.einvoice-section {
		margin-top: 14px;
		padding: 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.einvoice-hint {
		margin: 4px 0 10px;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.einvoice-error {
		margin-top: 10px;
		padding: 10px 12px;
		border: 1px solid var(--danger);
		border-left-width: 3px;
		border-radius: 5px;
		background: rgba(224, 82, 82, 0.08);
	}

	.einvoice-error-title {
		margin: 0;
		font-size: 0.82rem;
		font-weight: 600;
		color: var(--text);
	}

	/* The per-field reason rows (and their hint) live in
	   `EInvoiceIssueList.svelte` — styles travel with the markup, since Svelte
	   scoping would not reach a child component from here. */
	.einvoice-error-detail {
		margin: 8px 0 0;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.peppol-block {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid var(--border);
	}

	.peppol-title {
		font-size: 0.8rem;
		font-weight: 600;
		color: var(--text);
		margin-bottom: 4px;
	}

	.peppol-field {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin-bottom: 8px;
		font-size: 0.76rem;
		color: var(--text-muted);
	}

	.peppol-field input {
		padding: 6px 8px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--surface);
		color: var(--text);
		font-size: 0.85rem;
		font-family: inherit;
	}

	.btn-peppol-confirm {
		padding: 6px 14px;
		border: none;
		border-radius: 4px;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.8rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-peppol-confirm:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.peppol-result {
		margin: 0;
		font-size: 0.8rem;
		color: var(--text);
	}

	.peppol-state {
		margin: 0 0 8px;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.peppol-msgid {
		display: block;
		margin-top: 3px;
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.export-wrapper {
		position: relative;
	}

	.btn-export {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 8px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-family: inherit;
	}

	.btn-export:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.btn-export:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}

	.export-menu {
		position: absolute;
		bottom: 100%;
		left: 0;
		margin-bottom: 4px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
		overflow: hidden;
		z-index: 10;
	}

	.export-menu button {
		display: block;
		width: 100%;
		padding: 8px 20px;
		border: none;
		background: none;
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		text-align: left;
		font-family: inherit;
	}

	.export-menu button:hover {
		background: rgba(99, 140, 255, 0.1);
		color: var(--accent);
	}

	.warnings-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-top: 10px;
	}

	.warning-item {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8rem;
		padding: 6px 10px;
		border-radius: 5px;
	}

	.warning-item.error {
		background: rgba(224, 64, 64, 0.1);
		color: var(--danger);
	}

	.warning-item.warning {
		background: rgba(212, 148, 10, 0.1);
		color: #d4940a;
	}

	.warning-item.info {
		background: rgba(99, 140, 255, 0.1);
		color: #638cff;
	}

	.coding-suggestions {
		margin-top: 14px;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
	}
	.coding-suggestions-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}
	.coding-suggestions-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.coding-suggestions-hint {
		margin: 6px 0 10px;
		font-size: 0.78rem;
		line-height: 1.4;
		color: var(--text-muted);
	}
	.coding-suggestion {
		display: grid;
		grid-template-columns: 1fr auto;
		grid-template-areas: 'main action' 'why action';
		gap: 2px 10px;
		align-items: center;
		padding: 8px 0;
		border-top: 1px solid var(--border);
	}
	.coding-suggestion-main {
		grid-area: main;
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 8px;
	}
	.coding-suggestion-field {
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.coding-suggestion-value {
		font-size: 0.88rem;
		font-weight: 600;
	}
	.coding-suggestion-why {
		grid-area: why;
		margin: 0;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}
	.coding-suggestion-runner {
		margin-left: 6px;
	}
	.coding-suggestion-apply {
		grid-area: action;
		align-self: center;
		padding: 4px 12px;
		border-radius: 4px;
		border: 1px solid var(--accent);
		background: var(--surface);
		color: var(--accent);
		font-family: inherit;
		font-size: 0.8rem;
		cursor: pointer;
	}
	.coding-suggestion-apply:hover {
		background: rgba(99, 140, 255, 0.08);
	}
	.coding-suggestion-applied {
		margin: 10px 0 0;
		font-size: 0.76rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.ic-section {
		margin-top: 14px;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
	}
	.ic-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.ic-hint,
	.ic-routed,
	.ic-mirror {
		margin: 8px 0 0;
		font-size: 0.8rem;
		line-height: 1.4;
	}
	.ic-hint,
	.ic-mirror {
		color: var(--text-muted);
	}
	.ic-routed {
		font-weight: 500;
	}
	.ic-mirror-id {
		font-family: var(--font-mono);
		font-size: 0.74rem;
	}
	.ic-picker {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 10px;
	}
	.ic-picker > span {
		font-size: 0.74rem;
		color: var(--text-muted);
	}
	.ic-actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		margin-top: 10px;
	}
	.ic-cancel {
		border: none;
		background: none;
		padding: 4px 6px;
		color: var(--text-muted);
		font-family: inherit;
		font-size: 0.8rem;
		cursor: pointer;
		text-decoration: underline;
	}
	.ic-error {
		margin: 8px 0 0;
		font-size: 0.8rem;
		line-height: 1.4;
		color: var(--danger);
	}

	.po-match {
		margin-top: 12px;
		padding: 12px 14px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--bg);
		font-size: 0.85rem;
	}

	.po-match.matched {
		border-color: rgba(31, 168, 106, 0.4);
		background: rgba(31, 168, 106, 0.06);
	}

	.po-match.mismatch,
	.po-match.no_po {
		border-color: rgba(224, 64, 64, 0.4);
		background: rgba(224, 64, 64, 0.06);
	}

	.po-match.partial {
		border-color: rgba(212, 148, 10, 0.4);
		background: rgba(212, 148, 10, 0.06);
	}

	.po-match-header {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-bottom: 8px;
	}

	.po-match-title {
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-right: auto;
	}

	.po-match-status {
		font-size: 0.75rem;
		font-weight: 600;
		padding: 2px 10px;
		border-radius: 10px;
	}

	.po-match-status.matched {
		background: rgba(31, 168, 106, 0.15);
		color: #1fa86a;
	}

	.po-match-status.mismatch,
	.po-match-status.no_po {
		background: rgba(224, 64, 64, 0.15);
		color: var(--danger);
	}

	.po-match-status.partial {
		background: rgba(212, 148, 10, 0.15);
		color: #d4940a;
	}

	.po-match-type {
		font-size: 0.72rem;
		font-weight: 500;
		padding: 1px 8px;
		border-radius: 8px;
		background: var(--surface);
		color: var(--text-muted);
		border: 1px solid var(--border);
	}

	.po-match-grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
		gap: 10px;
		margin-bottom: 6px;
	}

	.po-match-grid > div {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.po-match-label {
		font-size: 0.72rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.po-match-value {
		font-size: 0.88rem;
		font-weight: 500;
	}

	.po-match-value.mono {
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.82rem;
	}

	.po-match-value.variance-pos {
		color: var(--danger);
	}

	.po-match-value.variance-neg {
		color: #d4940a;
	}

	.po-match-issues {
		margin: 6px 0 0;
		padding-left: 18px;
		font-size: 0.8rem;
		color: var(--text-muted);
	}

	.po-match-issues li {
		margin-bottom: 2px;
	}

	.po-match-inspection {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 8px;
		padding-top: 8px;
		border-top: 1px solid var(--border);
	}

	.required {
		color: var(--danger);
		font-style: normal;
	}

	.field-error input,
	.field-error select {
		border-color: var(--danger);
	}

	.field-error span {
		color: var(--danger);
	}

	.validation-hint {
		font-size: 0.8rem;
		color: #d4940a;
		margin-top: 8px;
	}

	/* --- Approval/assignment meta --- */

	.meta-section {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-top: 12px;
		padding: 10px 12px;
		background: var(--bg);
		border-radius: 6px;
		border: 1px solid var(--border);
	}

	.meta-item {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8rem;
	}

	.meta-label {
		color: var(--text-muted);
		min-width: 80px;
	}

	.meta-value {
		font-weight: 500;
		color: var(--text);
	}

	.meta-value.approved {
		color: #1fa86a;
	}

	.meta-value.rejected {
		color: var(--danger);
	}

	.meta-date {
		color: var(--text-muted);
		font-size: 0.75rem;
	}

	/* --- Activity log --- */

	.activity-section {
		margin-top: 14px;
	}

	.activity-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}

	.activity-list {
		display: flex;
		flex-direction: column;
		gap: 0;
		border-left: 2px solid var(--border);
		margin-left: 4px;
		padding-left: 12px;
	}

	.activity-item {
		display: flex;
		align-items: flex-start;
		gap: 6px;
		padding: 5px 0;
		position: relative;
		font-size: 0.78rem;
	}

	.activity-dot {
		position: absolute;
		left: -17px;
		top: 10px;
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--text-muted);
	}

	.activity-content {
		flex: 1;
		min-width: 0;
	}

	.activity-action {
		color: var(--text);
		font-weight: 500;
	}

	.activity-actor {
		color: var(--text-muted);
	}

	.activity-detail {
		color: var(--text-muted);
		display: block;
	}

	.activity-detail.error {
		color: var(--danger);
		font-style: italic;
	}

	.activity-changes {
		list-style: none;
		margin: 4px 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
		font-size: 0.85em;
	}

	.activity-changes li {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-wrap: wrap;
	}

	.change-field {
		color: var(--text-muted);
		font-weight: 600;
	}

	.change-old {
		color: var(--text-muted);
		text-decoration: line-through;
	}

	.change-arrow {
		color: var(--text-muted);
	}

	.change-new {
		color: var(--text);
		font-weight: 500;
	}

	.activity-time {
		color: var(--text-muted);
		font-size: 0.72rem;
		white-space: nowrap;
		flex-shrink: 0;
	}

	/* --- ERP status section --- */

	.erp-section {
		margin-top: 14px;
		padding: 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.erp-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}

	.erp-details {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.erp-row {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.8rem;
	}

	.erp-label {
		color: var(--text-muted);
		min-width: 80px;
	}

	.erp-value {
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
		font-size: 0.78rem;
		color: var(--text);
	}

	.erp-error .erp-value {
		color: var(--danger);
		font-family: inherit;
	}

	.btn-retry-erp {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		margin-top: 10px;
		padding: 6px 14px;
		border-radius: 4px;
		border: 1px solid var(--accent);
		background: var(--surface);
		color: var(--accent);
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-retry-erp:hover:not(:disabled) {
		background: rgba(99, 140, 255, 0.08);
	}

	.btn-retry-erp:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* --- Review section --- */

	.review-section {
		margin-top: 14px;
		padding: 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.chat-section {
		margin-top: 14px;
		padding: 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
	}

	.review-title {
		font-size: 0.72rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 10px;
	}

	.review-actions {
		display: flex;
		gap: 8px;
	}

	.btn-approve {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 18px;
		border-radius: 4px;
		border: none;
		background: var(--success-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		flex: 1;
		justify-content: center;
	}

	.btn-approve:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-approve:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.btn-reject-outline {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 8px 18px;
		border-radius: 4px;
		border: 1px solid var(--danger);
		background: var(--surface);
		color: var(--danger);
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
		flex: 1;
		justify-content: center;
	}

	.btn-reject-outline:hover:not(:disabled) {
		background: rgba(224, 64, 64, 0.1);
	}

	.btn-reject-outline:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.reject-form {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.reject-input {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 8px 10px;
		font-size: 0.85rem;
		color: var(--text);
		font-family: inherit;
		resize: vertical;
		width: 100%;
		box-sizing: border-box;
	}

	.reject-input:focus {
		outline: none;
		border-color: var(--danger);
		box-shadow: 0 0 0 2px rgba(224, 64, 64, 0.15);
	}

	.reject-actions {
		display: flex;
		gap: 8px;
		justify-content: flex-end;
	}

	.btn-cancel-sm {
		padding: 6px 12px;
		border-radius: 4px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-cancel-sm:hover {
		background: var(--bg);
	}

	.btn-reject {
		padding: 6px 14px;
		border-radius: 4px;
		border: none;
		background: var(--danger-strong);
		color: #fff;
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-reject:hover:not(:disabled) {
		opacity: 0.9;
	}

	.btn-reject:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	.review-assigned-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0;
	}

	.save-error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 8px 12px;
		border-radius: 4px;
		font-size: 0.82rem;
		margin-top: 8px;
	}

	/* --- Responsive: stack on narrow screens --- */

	@media (max-width: 768px) {
		.split {
			flex-direction: column;
		}

		.pdf-pane {
			border-right: none;
			border-bottom: 1px solid var(--border);
			height: 45%;
			flex: none;
		}

		.form-pane {
			width: 100% !important;
		}

		.resize-handle {
			display: none;
		}
	}

	/* ------------------------- audit summary ---------------------------- */
	.audit-summary {
		margin: 0 0 16px;
		padding: 12px 14px;
		border: 1px solid var(--border);
		border-left: 3px solid var(--accent);
		border-radius: 8px;
		background: var(--surface);
	}
	.audit-summary-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 6px;
	}
	.audit-summary-label {
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.audit-summary-regen {
		font-size: 11px;
		padding: 2px 8px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: transparent;
		color: var(--text-muted);
		cursor: pointer;
	}
	.audit-summary-regen:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.audit-summary-regen:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.audit-summary-text {
		margin: 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--text);
	}
	.audit-summary-context {
		margin: 6px 0 0;
		font-size: 11px;
		font-style: italic;
		color: var(--text-muted);
	}
	.audit-summary-skeleton {
		height: 12px;
		border-radius: 4px;
		background: linear-gradient(90deg, var(--border) 25%, var(--surface) 50%, var(--border) 75%);
		background-size: 200% 100%;
		animation: audit-summary-shimmer 1.2s ease-in-out infinite;
		margin-bottom: 8px;
	}
	.audit-summary-skeleton.short {
		width: 60%;
		margin-bottom: 0;
	}
	@keyframes audit-summary-shimmer {
		0% { background-position: 200% 0; }
		100% { background-position: -200% 0; }
	}
	@media (prefers-reduced-motion: reduce) {
		.audit-summary-skeleton {
			animation: none;
		}
	}

	/* -------------------------- priors panel ---------------------------- */
	.priors-section {
		margin: 16px 0;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
	}
	.priors-toggle {
		width: 100%;
		background: transparent;
		border: none;
		padding: 12px 14px;
		display: flex;
		align-items: center;
		gap: 10px;
		cursor: pointer;
		color: var(--text);
		font-family: inherit;
		text-align: left;
	}
	.priors-title {
		font-weight: 600;
		font-size: 0.88rem;
	}
	.priors-chips {
		display: inline-flex;
		gap: 6px;
		flex: 1;
	}
	.priors-caret {
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.priors-body {
		padding: 0 14px 14px;
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.priors-group-title {
		font-size: 0.76rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 8px;
	}
	.priors-tags {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.priors-tag {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 4px 10px;
		font-size: 0.8rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.priors-help {
		margin-top: 8px;
		color: var(--text-muted);
		font-size: 0.78rem;
		line-height: 1.5;
	}
	.priors-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.82rem;
	}
	.priors-table th,
	.priors-table td {
		text-align: left;
		padding: 6px 10px;
		border-bottom: 1px solid var(--border);
	}
	.priors-table th {
		color: var(--text-muted);
		font-weight: 500;
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.priors-table tr:last-child td {
		border-bottom: none;
	}
</style>
