<script lang="ts">
	import { page } from '$app/stores';
	import { beforeNavigate, goto } from '$app/navigation';
	import { auth } from '$lib/stores/auth.svelte';
	import { workflowStore } from '$lib/stores/workflows.svelte';
	import { adminStore } from '$lib/stores/admin.svelte';
	import { api } from '$lib/api';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { orgCurrency } from '$lib/stores/orgSettings.svelte';
	import type { MoneyAmount } from '$lib/utils/money';
	import { isMoneyInput } from '$lib/utils/moneyInput';
	import ApprovalMatrixEditor from '$lib/components/modals/ApprovalMatrixEditor.svelte';
	import Badge from '$lib/components/ui/Badge.svelte';
	import WorkflowCanvas from '$lib/components/workflow-builder/WorkflowCanvas.svelte';
	import StepPalette from '$lib/components/workflow-builder/StepPalette.svelte';
	import ConditionBuilder from '$lib/components/workflow-builder/ConditionBuilder.svelte';
	import ParallelBranchEditor from '$lib/components/workflow-builder/ParallelBranchEditor.svelte';
	import CustomStepConfig from '$lib/components/workflow-builder/CustomStepConfig.svelte';
	import type {
		WorkflowDefinition,
		WorkflowStep,
		WorkflowStepType,
		StepConfig,
		ExtractionStepConfig,
		ApprovalStepConfig,
		ApprovalLevelConfig,
		ErpExportStepConfig,
		ConditionStepConfig,
		ParallelStepConfig,
		WebhookStepConfig,
		EmailStepConfig,
		DelayStepConfig,
	} from '$lib/types/workflow';
	import {
		STEP_TYPE_LABELS,
		STEP_TYPE_DESCRIPTIONS,
		ERP_FORMAT_LABELS,
		DEFAULT_STEP_CONFIGS,
	} from '$lib/types/workflow';

	let workflow = $state<WorkflowDefinition | null>(null);
	let steps = $state<WorkflowStep[]>([]);
	let selectedIndex = $state<number>(0);
	let saving = $state(false);
	let dirty = $state(false);

	// Unsaved-changes guard. Editing the canvas sets `dirty`; without this, a
	// click on another nav link or a tab reload silently discarded all edits.
	// `beforeNavigate` covers in-app navigation; the `beforeunload` listener
	// covers a browser reload / tab close. `saving` is exempt so a successful
	// save (which clears `dirty` right after) never trips the prompt.
	beforeNavigate((nav) => {
		if (dirty && !saving) {
			if (!confirm(m('workflows.builder.unsavedConfirm'))) {
				nav.cancel();
			}
		}
	});

	$effect(() => {
		function onBeforeUnload(e: BeforeUnloadEvent) {
			if (dirty) {
				e.preventDefault();
				e.returnValue = '';
			}
		}
		window.addEventListener('beforeunload', onBeforeUnload);
		return () => window.removeEventListener('beforeunload', onBeforeUnload);
	});
	let editingName = $state(false);
	let nameInput = $state('');
	let descInput = $state('');
	let approverSearch = $state('');
	let approverDropdownOpen = $state(false);
	let erpMethod = $state<string>('merge_dev');
	// Set while a palette item is being dragged, so the canvas can show drop slots.
	let paletteDragType = $state<WorkflowStepType | null>(null);

	const id = $derived($page.params.id ?? '');

	/**
	 * The same redirect guard the `/workflows` list carries, for the same
	 * reason — see the long note there.
	 *
	 * Guarding only the list would have left a hole a bookmark walks straight
	 * through: `GET /api/workflows/{id}` is `get_current_user`, so a non-admin
	 * typing this URL got the drag-and-drop builder in full, with an editable
	 * canvas, a step palette and a Save that 403s — the worst version of the
	 * dead end, since the work is lost rather than merely refused. This route
	 * also fires `adminStore.fetchUsers()` for the approver picker, which is
	 * admin-only (`GET /api/admin/users`), so a non-admin's picker was silently
	 * empty on top of it.
	 */
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	$effect(() => {
		if (!userLoaded || !allowed) return;
		if (id) loadWorkflow(id);
		// Fire-and-forget: the store loaders re-throw so an awaiting caller keeps
		// its own handling, but nothing awaits here — the store's `errored` flag is
		// what the UI renders. Swallow so a failed load isn't an unhandled rejection.
		adminStore.fetchUsers().catch(() => {});
		loadErpMethod();
		// The approval-step money thresholds (`auto_approve_below`,
		// `require_cfo_above`, `max_invoice_amount`) and the matrix's per-level
		// amount bands are BARE NUMBERS denominated in the org's reporting
		// currency — the backend converts each invoice into it before comparing
		// (`approval_chain.reporting_gate_amount`). Nothing in the input says so,
		// so the labels name the code, resolved from the `orgCurrency` store
		// rather than assuming dollars. Until it resolves — or when the org sets
		// nothing the client can read — `orgCurrency.label` names the concept
		// ("reporting currency") instead of guessing a code (decisions §200), so
		// the field is never unlabelled and never mislabelled.
		orgCurrency.ensureLoaded();
	});

	async function loadErpMethod() {
		try {
			const org = await api.get<{ settings: { erp?: { integration_method?: string } } }>(
				'/api/organization'
			);
			erpMethod = org.settings?.erp?.integration_method ?? 'merge_dev';
		} catch {
			// default to merge_dev
		}
	}

	// Sequences `loadWorkflow`. The canvas is an editor over the fetched
	// definition, and every edit below is applied locally with no fetch of its
	// own — so a GET still in flight (the mount load, or the one the `id`
	// `$effect` re-fires) resolves afterwards holding the pre-edit definition
	// and silently wipes the edits, leaving `dirty` set on a canvas that no
	// longer shows them. `markDirty()` retires the in-flight load before every
	// such edit. See `frontend/CLAUDE.md` § Sequencing list fetches.
	const loadSequence = createRequestSequencer();

	/** Apply a local edit to the canvas: retire any in-flight load first (its
	 *  response predates the edit), then flag the unsaved-changes guard. */
	function markDirty() {
		loadSequence.supersedeInFlight();
		dirty = true;
	}

	async function loadWorkflow(wfId: string) {
		const token = loadSequence.start();
		try {
			const wf = await workflowStore.getById(wfId);
			// Superseded by a newer load, or by a local canvas edit.
			if (!loadSequence.canCommit(token)) return;
			workflow = wf;
			steps = structuredClone(wf.steps_config?.steps ?? []);
			nameInput = wf.name;
			descInput = wf.description ?? '';
			selectedIndex = 0;
		} catch {
			// `isCurrentRequest`, not `canCommit`: a load superseded by a local
			// edit still failed, and no newer load is coming to report it.
			if (loadSequence.isCurrentRequest(token)) toast(m('workflows.builder.toast.notFound'), 'error');
		}
	}

	let selectedStep = $derived(steps[selectedIndex] ?? null);

	function renumber(arr: WorkflowStep[]): WorkflowStep[] {
		return arr.map((s, i) => ({ ...s, number: i + 1 }));
	}

	function makeStep(type: WorkflowStepType): WorkflowStep {
		return {
			number: steps.length + 1,
			type,
			name: STEP_TYPE_LABELS[type],
			enabled: true,
			config: DEFAULT_STEP_CONFIGS[type](),
		};
	}

	function addStep(type: WorkflowStepType) {
		const next = renumber([...steps, makeStep(type)]);
		steps = next;
		selectedIndex = next.length - 1;
		markDirty();
	}

	function addStepAt(type: WorkflowStepType, index: number) {
		const clamped = Math.max(0, Math.min(index, steps.length));
		const arr = [...steps];
		arr.splice(clamped, 0, makeStep(type));
		steps = renumber(arr);
		selectedIndex = clamped;
		markDirty();
	}

	function reorderStep(from: number, to: number) {
		if (from === to) return;
		const arr = [...steps];
		const [moved] = arr.splice(from, 1);
		arr.splice(to, 0, moved);
		steps = renumber(arr);
		selectedIndex = to;
		markDirty();
	}

	function removeStep(index: number) {
		if (steps.length <= 1) return;
		steps = renumber(steps.filter((_, i) => i !== index));
		if (selectedIndex >= steps.length) selectedIndex = steps.length - 1;
		markDirty();
	}

	function toggleStep(index: number) {
		steps = steps.map((s, i) => (i === index ? { ...s, enabled: !s.enabled } : s));
		markDirty();
	}

	function updateStepField(index: number, field: 'name' | 'enabled', value: unknown) {
		steps = steps.map((s, i) => (i === index ? { ...s, [field]: value } : s));
		markDirty();
	}

	function updateStepConfig(index: number, key: string, value: unknown) {
		steps = steps.map((s, i) =>
			i === index ? { ...s, config: { ...s.config, [key]: value } } : s
		);
		markDirty();
	}

	function replaceStepConfig(index: number, config: StepConfig) {
		steps = steps.map((s, i) => (i === index ? { ...s, config } : s));
		markDirty();
	}

	function handleWindowClick(e: MouseEvent) {
		if (approverDropdownOpen && !(e.target as HTMLElement).closest('.approver-search-wrap')) {
			approverDropdownOpen = false;
		}
	}

	/**
	 * Every money threshold on an approval step, in the order the editor shows
	 * them: the three step-level gates, then each chain level's amount band.
	 *
	 * These go to the wire as the exact decimal text that was typed
	 * (`MoneyString`), so the shape check belongs at save — not on every
	 * keystroke, which would toast at a half-typed `12.`. A blank field is a
	 * real value ("no threshold") and is skipped; anything else the money
	 * grammar cannot read is REFUSED rather than repaired or forwarded as
	 * `null`, because a silently-dropped `require_cfo_above` removes the CFO
	 * gate on every invoice this workflow routes.
	 */
	function unreadableMoneyThresholds(): boolean {
		return steps.some((step) => {
			if (step.type !== 'approval') return false;
			const cfg = step.config as ApprovalStepConfig;
			const thresholds: MoneyAmount[] = [
				cfg.auto_approve_below,
				cfg.require_cfo_above,
				cfg.max_invoice_amount,
				...(cfg.approval_chain ?? []).flatMap((level) => [level.min_amount, level.max_amount]),
			];
			return thresholds.some(
				(value) =>
					value !== null &&
					value !== undefined &&
					value !== '' &&
					// `String(...)` because a workflow saved before the text
					// conversion still holds a NUMBER in `steps_config`; the money
					// grammar reads text, and a legacy `1000` is legitimate.
					!isMoneyInput(String(value))
			);
		});
	}

	async function handleSave() {
		if (!workflow) return;
		if (unreadableMoneyThresholds()) {
			toast(m('common.amountInvalid'), 'error');
			return;
		}
		saving = true;
		try {
			await workflowStore.update(workflow.id, {
				name: nameInput.trim(),
				description: descInput.trim() || undefined,
				steps,
			});
			dirty = false;
			toast(m('workflows.builder.toast.saved'), 'success');
		} catch (e: unknown) {
			toast(e instanceof Error ? e.message : m('workflows.builder.toast.saveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	async function toggleActive() {
		if (!workflow) return;
		try {
			const updated = await workflowStore.update(workflow.id, {
				is_active: !workflow.is_active,
			});
			workflow = updated;
			toast(
				updated.is_active
					? m('workflows.builder.toast.activated')
					: m('workflows.builder.toast.deactivated'),
				'success'
			);
		} catch (e: unknown) {
			toast(e instanceof Error ? e.message : m('workflows.builder.toast.updateFailed'), 'error');
		}
	}
</script>

<svelte:window onclick={handleWindowClick} />

<div class="workspace">
	{#if !workflow}
		<div class="loading">{m('common.loading')}</div>
	{:else}
		<header class="toolbar">
			<div class="toolbar-left">
				<a href="/workflows" class="back-link" aria-label={m('workflows.builder.aria.back')}>
					<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>
				</a>
				{#if editingName}
					<input
						class="name-input"
						type="text"
						aria-label={m('workflows.builder.aria.workflowName')}
						bind:value={nameInput}
						onblur={() => { editingName = false; markDirty(); }}
						onkeydown={(e) => { if (e.key === 'Enter') { editingName = false; markDirty(); } }}
					/>
				{:else}
					<h1 class="page-title">
						<button
							type="button"
							class="page-title-edit"
							aria-label={m('workflows.builder.aria.editName', { name: nameInput })}
							onclick={() => (editingName = true)}
						>
							{nameInput}
						</button>
						{#if workflow.is_default}
							<Badge tone="accent" variant="default-badge">{m('workflows.builder.defaultBadge')}</Badge>
						{/if}
					</h1>
				{/if}
			</div>
			<div class="toolbar-right">
				<button class="btn-toggle" class:active={workflow.is_active} onclick={toggleActive}>
					{workflow.is_active
						? m('workflows.builder.status.active')
						: m('workflows.builder.status.inactive')}
				</button>
				<button class="btn-save" disabled={saving || !dirty} onclick={handleSave}>
					{saving ? m('common.saving') : m('common.save')}
				</button>
			</div>
		</header>

		<div class="description-row">
			<input
				class="desc-input"
				type="text"
				aria-label={m('workflows.builder.aria.workflowDescription')}
				placeholder={m('workflows.builder.descriptionPlaceholder')}
				bind:value={descInput}
				oninput={() => markDirty()}
			/>
		</div>

		<div class="editor">
			<!-- Left: draggable step library -->
			<StepPalette
				ondragtype={(type) => (paletteDragType = type)}
				ondragend={() => (paletteDragType = null)}
				onadd={addStep}
			/>

			<!-- Centre: flow canvas -->
			<div class="canvas-pane">
				<div class="pane-header">
					<span class="pane-label">{m('workflows.builder.flowLabel')}</span>
				</div>
				<WorkflowCanvas
					{steps}
					{selectedIndex}
					paletteType={paletteDragType}
					onselect={(i) => (selectedIndex = i)}
					onreorder={reorderStep}
					onaddat={addStepAt}
					ontoggle={toggleStep}
					ondelete={removeStep}
				/>
			</div>

			<!-- Right: step config panel -->
			<div class="config-panel">
				{#if selectedStep}
					<div class="config-header">
						<h3>{selectedStep.name}</h3>
						<p class="config-desc">{STEP_TYPE_DESCRIPTIONS[selectedStep.type]}</p>
					</div>

					<div class="config-body">
						<!-- Common fields -->
						<div class="field">
							<label for="step-name">{m('workflows.builder.field.stepName')}</label>
							<input
								id="step-name"
								type="text"
								value={selectedStep.name}
								oninput={(e) => updateStepField(selectedIndex, 'name', e.currentTarget.value)}
							/>
						</div>

						<div class="field toggle-field">
							<label id="step-enabled-label" for="step-enabled">{m('workflows.builder.field.enabled')}</label>
							<button
								id="step-enabled"
								class="toggle"
								class:on={selectedStep.enabled}
								role="switch"
								aria-checked={selectedStep.enabled}
								aria-labelledby="step-enabled-label"
								onclick={() => updateStepField(selectedIndex, 'enabled', !selectedStep.enabled)}
							>
								<span class="toggle-knob"></span>
							</button>
						</div>

						<hr />

						<!-- Extraction config -->
						{#if selectedStep.type === 'extraction'}
							{@const cfg = selectedStep.config as ExtractionStepConfig}
							<div class="field toggle-field">
								<label id="auto-approve-label" for="auto-approve">{m('workflows.builder.extraction.autoApprove')}</label>
								<button
									id="auto-approve"
									class="toggle"
									class:on={cfg.auto_approve_enabled}
									role="switch"
									aria-checked={cfg.auto_approve_enabled}
									aria-labelledby="auto-approve-label"
									onclick={() => updateStepConfig(selectedIndex, 'auto_approve_enabled', !cfg.auto_approve_enabled)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>

							{#if cfg.auto_approve_enabled}
								<div class="field">
									<label for="threshold">{m('workflows.builder.extraction.confidenceThreshold')}</label>
									<div class="range-row">
										<input
											id="threshold"
											type="range"
											min="0.5"
											max="1"
											step="0.01"
											value={cfg.auto_approve_threshold}
											oninput={(e) => updateStepConfig(selectedIndex, 'auto_approve_threshold', parseFloat(e.currentTarget.value))}
										/>
										<span class="range-value">{Math.round(cfg.auto_approve_threshold * 100)}%</span>
									</div>
									<p class="field-hint">
										{m('workflows.builder.extraction.thresholdHint')}
									</p>
								</div>
							{/if}
						{/if}

						<!-- Approval config -->
						{#if selectedStep.type === 'approval'}
							{@const cfg = selectedStep.config as ApprovalStepConfig}
							<div class="field toggle-field">
								<label id="approval-required-label" for="approval-required">{m('workflows.builder.approval.required')}</label>
								<button
									id="approval-required"
									class="toggle"
									class:on={cfg.required}
									role="switch"
									aria-checked={cfg.required}
									aria-labelledby="approval-required-label"
									onclick={() => updateStepConfig(selectedIndex, 'required', !cfg.required)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>

							<div class="field">
								<label for="approver-strategy">{m('workflows.builder.approval.approverAssignment')}</label>
								<select
									id="approver-strategy"
									value={cfg.approver_strategy}
									onchange={(e) => updateStepConfig(selectedIndex, 'approver_strategy', e.currentTarget.value)}
								>
									<option value="manual">{m('workflows.builder.approval.strategyManual')}</option>
									<option value="specific">{m('workflows.builder.approval.strategySpecific')}</option>
									<option value="chain">{m('workflows.builder.approval.strategyChain')}</option>
									<option value="auto">{m('workflows.builder.approval.strategyAuto')}</option>
								</select>
							</div>

							{#if cfg.approver_strategy === 'specific'}
								{@const ids = cfg.approver_ids ?? []}
								{@const selectedUsers = ids.map((uid: string) => adminStore.users.find(u => u.id === uid)).filter(Boolean)}
								{@const availableUsers = adminStore.users.filter(u => u.is_active && !ids.includes(u.id))}
								{@const query = approverSearch.toLowerCase().trim()}
								{@const filteredUsers = query
									? availableUsers.filter(u =>
										u.full_name.toLowerCase().includes(query) ||
										u.email.toLowerCase().includes(query)
									)
									: availableUsers}
								<div class="field">
									<label for="approver-search-input">{m('workflows.builder.approval.approvers')}</label>

									{#if selectedUsers.length > 0}
										<div class="approver-chips">
											{#each selectedUsers as user}
												<span class="approver-chip">
													{user?.full_name}
													<button
														type="button"
														class="chip-remove"
														aria-label={m('workflows.builder.approval.removeApprover', { name: user?.full_name ?? '' })}
														onclick={(e) => {
															e.stopPropagation();
															updateStepConfig(selectedIndex, 'approver_ids', ids.filter((uid: string) => uid !== user?.id));
														}}
													>&times;</button>
												</span>
											{/each}
										</div>
									{/if}

									<!-- Opening the dropdown is driven by the input's onfocus below
									     (keyboard Tab + pointer click both focus it), so the wrapper
									     itself carries no click handler — keeps it a plain container. -->
									<div class="approver-search-wrap">
										<input
											id="approver-search-input"
											type="text"
											class="approver-search"
											placeholder={m('workflows.builder.approval.searchUsers')}
											bind:value={approverSearch}
											onfocus={() => (approverDropdownOpen = true)}
										/>
										{#if approverDropdownOpen && filteredUsers.length > 0}
											<div class="approver-dropdown">
												{#each filteredUsers as user}
													<button
														type="button"
														class="approver-option"
														onclick={() => {
															updateStepConfig(selectedIndex, 'approver_ids', [...ids, user.id]);
														}}
													>
														<span class="approver-name">{user.full_name}</span>
														<span class="approver-email">{user.email}</span>
													</button>
												{/each}
											</div>
										{:else if approverDropdownOpen && query && filteredUsers.length === 0}
											<div class="approver-dropdown">
												<div class="approver-empty">{m('workflows.builder.approval.noMatchingUsers')}</div>
											</div>
										{/if}
									</div>

									{#if ids.length > 0}
										<p class="field-hint">{m('workflows.builder.approval.roundRobinHint', { count: ids.length })}</p>
									{:else}
										<p class="field-hint warning">{m('workflows.builder.approval.noApproversSelected')}</p>
									{/if}
								</div>
							{/if}

							{#if cfg.approver_strategy === 'auto'}
								<p class="field-hint warning">
									{m('workflows.builder.approval.autoWarning')}
								</p>
							{/if}

							{#if cfg.approver_strategy === 'chain'}
								<div class="field">
									<label for="approval-matrix">{m('workflows.builder.approval.matrix')}</label>
									<p class="field-hint">
										{m('workflows.builder.approval.matrixHint')}
									</p>
									<ApprovalMatrixEditor
										chain={cfg.approval_chain ?? []}
										users={adminStore.users}
										onchange={(next: ApprovalLevelConfig[]) =>
											updateStepConfig(selectedIndex, 'approval_chain', next)}
									/>
								</div>
							{/if}

							<div class="field-divider"></div>
							<h4 class="field-section-title">{m('workflows.builder.approval.thresholdsTitle')}</h4>

							<div class="field">
								<label for="auto-approve-below">{m('workflows.builder.approval.autoApproveBelow', { currency: orgCurrency.label })}</label>
								<input
									id="auto-approve-below"
									type="number"
									step="0.01"
									min="0"
									placeholder={m('workflows.builder.approval.noLimit')}
									value={cfg.auto_approve_below ?? ''}
									oninput={(e) => updateStepConfig(selectedIndex, 'auto_approve_below', e.currentTarget.value.trim() || null)}
								/>
								<p class="field-hint">{m('workflows.builder.approval.autoApproveBelowHint')}</p>
							</div>

							<div class="field">
								<label for="require-cfo-above">{m('workflows.builder.approval.requireCfoAbove', { currency: orgCurrency.label })}</label>
								<input
									id="require-cfo-above"
									type="number"
									step="0.01"
									min="0"
									placeholder={m('workflows.builder.approval.noLimit')}
									value={cfg.require_cfo_above ?? ''}
									oninput={(e) => updateStepConfig(selectedIndex, 'require_cfo_above', e.currentTarget.value.trim() || null)}
								/>
								<p class="field-hint">{m('workflows.builder.approval.requireCfoAboveHint')}</p>
							</div>

							<div class="field">
								<label for="max-invoice-amount">{m('workflows.builder.approval.maxInvoiceAmount', { currency: orgCurrency.label })}</label>
								<input
									id="max-invoice-amount"
									type="number"
									step="0.01"
									min="0"
									placeholder={m('workflows.builder.approval.noLimit')}
									value={cfg.max_invoice_amount ?? ''}
									oninput={(e) => updateStepConfig(selectedIndex, 'max_invoice_amount', e.currentTarget.value.trim() || null)}
								/>
								<p class="field-hint">{m('workflows.builder.approval.maxInvoiceAmountHint')}</p>
							</div>

							<div class="field-divider"></div>
							<h4 class="field-section-title">{m('workflows.builder.approval.controlsTitle')}</h4>

							<!--
								Segregation of duties (approver ≠ uploader). Defaults ON — the
								backend's own default — and is now visible so switching it OFF
								is a deliberate act. `?? true` mirrors `approval_chain.py`'s
								`.get("require_segregation", True)` so a legacy definition saved
								without the key reads as ON here, exactly as the backend enforces
								it, rather than rendering as OFF and being written back as OFF.
							-->
							{@const segregation = cfg.require_segregation ?? true}
							<div class="field toggle-field">
								<label id="approval-segregation-label" for="approval-segregation">
									{m('workflows.builder.approval.requireSegregation')}
								</label>
								<button
									id="approval-segregation"
									class="toggle"
									class:on={segregation}
									role="switch"
									aria-checked={segregation}
									aria-labelledby="approval-segregation-label"
									onclick={() => updateStepConfig(selectedIndex, 'require_segregation', !segregation)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>
							<p class="field-hint" class:warning={!segregation}>
								{segregation
									? m('workflows.builder.approval.requireSegregationHint')
									: m('workflows.builder.approval.requireSegregationWarning')}
							</p>
						{/if}

						<!-- ERP Export config -->
						{#if selectedStep.type === 'erp_export'}
							{@const cfg = selectedStep.config as ErpExportStepConfig}

							<p class="field-hint">{m('workflows.builder.erp.credentialsHintPre')}<a href="/organization">{m('workflows.builder.erp.credentialsHintLink')}</a>{m('workflows.builder.erp.credentialsHintPost')}</p>

							<div class="field toggle-field">
								<label id="auto-send-label" for="auto-send">{m('workflows.builder.erp.autoSend')}</label>
								<button
									id="auto-send"
									class="toggle"
									class:on={cfg.auto_send_on_approval}
									role="switch"
									aria-checked={cfg.auto_send_on_approval}
									aria-labelledby="auto-send-label"
									onclick={() => updateStepConfig(selectedIndex, 'auto_send_on_approval', !cfg.auto_send_on_approval)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>
							{#if !cfg.auto_send_on_approval}
								<p class="field-hint warning">{m('workflows.builder.erp.noAutoSendWarning')}</p>
							{/if}

							{#if erpMethod === 'direct'}
								<div class="field">
									<label for="export-format">{m('workflows.builder.erp.exportFormat')}</label>
									<select
										id="export-format"
										value={cfg.export_format}
										onchange={(e) => updateStepConfig(selectedIndex, 'export_format', e.currentTarget.value)}
									>
										{#each Object.entries(ERP_FORMAT_LABELS) as [value, label]}
											<option {value}>{label}</option>
										{/each}
									</select>
									<p class="field-hint">
										{#if cfg.export_format === 'xml'}
											{m('workflows.builder.erp.formatHintXml')}
										{:else if cfg.export_format === 'csv'}
											{m('workflows.builder.erp.formatHintCsv')}
										{:else if cfg.export_format === 'cxml'}
											{m('workflows.builder.erp.formatHintCxml')}
										{:else if cfg.export_format === 'edi'}
											{m('workflows.builder.erp.formatHintEdi')}
										{:else}
											{m('workflows.builder.erp.formatHintJson')}
										{/if}
									</p>
								</div>
							{:else}
								<p class="field-hint">{m('workflows.builder.erp.formatHintMerge')}</p>
							{/if}

							<div class="field-divider"></div>
							<h4 class="field-section-title">{m('workflows.builder.erp.payloadOptions')}</h4>

							<div class="field toggle-field">
								<label id="include-lines-label" for="include-lines">{m('workflows.builder.erp.includeLineItems')}</label>
								<button
									id="include-lines"
									class="toggle"
									class:on={cfg.include_line_items}
									role="switch"
									aria-checked={cfg.include_line_items}
									aria-labelledby="include-lines-label"
									onclick={() => updateStepConfig(selectedIndex, 'include_line_items', !cfg.include_line_items)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>

							<div class="field toggle-field">
								<label id="include-attach-label" for="include-attach">{m('workflows.builder.erp.includeAttachments')}</label>
								<button
									id="include-attach"
									class="toggle"
									class:on={cfg.include_attachments}
									role="switch"
									aria-checked={cfg.include_attachments}
									aria-labelledby="include-attach-label"
									onclick={() => updateStepConfig(selectedIndex, 'include_attachments', !cfg.include_attachments)}
								>
									<span class="toggle-knob"></span>
								</button>
							</div>
						{/if}

						<!-- Condition config -->
						{#if selectedStep.type === 'condition'}
							<ConditionBuilder
								config={selectedStep.config as ConditionStepConfig}
								{steps}
								selfNumber={selectedStep.number}
								onchange={(next) => replaceStepConfig(selectedIndex, next)}
							/>
						{/if}

						<!-- Parallel config -->
						{#if selectedStep.type === 'parallel'}
							<ParallelBranchEditor
								config={selectedStep.config as ParallelStepConfig}
								users={adminStore.users}
								onchange={(next) => replaceStepConfig(selectedIndex, next)}
							/>
						{/if}

						<!-- Webhook / Email / Delay config -->
						{#if selectedStep.type === 'webhook' || selectedStep.type === 'email' || selectedStep.type === 'delay'}
							<CustomStepConfig
								type={selectedStep.type}
								config={selectedStep.config as WebhookStepConfig | EmailStepConfig | DelayStepConfig}
								onchange={(next) => replaceStepConfig(selectedIndex, next)}
							/>
						{/if}
					</div>

					<div class="config-footer">
						<div class="move-btns">
							<button class="move-btn" disabled={selectedIndex === 0} onclick={() => reorderStep(selectedIndex, selectedIndex - 1)}>{m('workflows.builder.moveUp')}</button>
							<button class="move-btn" disabled={selectedIndex === steps.length - 1} onclick={() => reorderStep(selectedIndex, selectedIndex + 1)}>{m('workflows.builder.moveDown')}</button>
						</div>
						{#if steps.length > 1}
							<button class="remove-btn" onclick={() => removeStep(selectedIndex)}>{m('workflows.builder.removeStep')}</button>
						{/if}
					</div>
				{:else}
					<div class="no-selection">{m('workflows.builder.noSelection')}</div>
				{/if}
			</div>
		</div>
	{/if}
</div>

<style>
	/* Page-specific styling; shared design-system CSS lives in app.css. */
	.loading {
		padding: 60px 20px;
		text-align: center;
		color: var(--text-muted);
	}

	/* Toolbar */
	.toolbar-left {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.toolbar-right {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.back-link {
		display: grid;
		place-items: center;
		width: 32px;
		height: 32px;
		border-radius: 6px;
		color: var(--text-muted);
		text-decoration: none;
		transition: all 0.15s;
	}

	.back-link:hover {
		background: var(--surface);
		color: var(--text);
	}

	.page-title {
		font-size: 1.15rem;
		font-weight: 600;
		color: var(--text);
		margin: 0;
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.page-title-edit {
		font-size: inherit;
		font-weight: inherit;
		color: inherit;
		background: none;
		border: none;
		padding: 0;
		margin: 0;
		cursor: pointer;
		font-family: inherit;
	}

	.page-title-edit:hover {
		text-decoration: underline;
	}

	.name-input {
		font-size: 1.15rem;
		font-weight: 600;
		color: var(--text);
		background: var(--bg);
		border: 1px solid var(--accent);
		border-radius: 6px;
		padding: 4px 10px;
		outline: none;
		font-family: inherit;
	}

	.btn-toggle {
		padding: 6px 14px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-toggle.active {
		border-color: #1fa86a;
		color: #1fa86a;
	}

	.btn-save {
		padding: 8px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-save:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	/* Description */
	.description-row {
		padding: 0;
	}

	.desc-input {
		width: 100%;
		padding: 8px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.85rem;
		font-family: inherit;
		outline: none;
		box-sizing: border-box;
	}

	.desc-input:focus {
		border-color: var(--accent);
		color: var(--text);
	}

	/* Editor layout: palette | canvas | config */
	.editor {
		display: grid;
		grid-template-columns: 240px 1fr 360px;
		gap: 16px;
		min-height: 500px;
		align-items: start;
	}

	.canvas-pane {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		display: flex;
		flex-direction: column;
	}

	.pane-header {
		padding: 12px 14px;
		border-bottom: 1px solid var(--border);
	}

	.pane-label {
		font-size: 0.75rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	/* Config panel (right) */
	.config-panel {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		display: flex;
		flex-direction: column;
	}

	.config-header {
		padding: 16px 20px;
		border-bottom: 1px solid var(--border);
	}

	.config-header h3 {
		margin: 0;
		font-size: 1rem;
		color: var(--text);
	}

	.config-desc {
		margin: 4px 0 0;
		font-size: 0.82rem;
		color: var(--text-muted);
	}

	.config-body {
		padding: 20px;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	.config-body hr {
		border: none;
		border-top: 1px solid var(--border);
		margin: 4px 0;
	}

	.field label {
		display: block;
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin-bottom: 5px;
	}

	.field input[type='text'],
	.field input[type='url'],
	.field input[type='number'],
	.field select {
		/* base look (border/colour/font/chevron) from the global select recipe */
		width: 100%;
		padding: 8px 30px 8px 10px;
		border-radius: 6px;
		outline: none;
		box-sizing: border-box;
	}

	.field input:focus,
	.field select:focus {
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.field select {
		cursor: pointer;
	}

	.field-hint {
		margin: 5px 0 0;
		font-size: 0.78rem;
		color: var(--text-muted);
		line-height: 1.4;
	}

	.field-hint.warning {
		color: #d4940a;
	}

	.field-divider {
		height: 1px;
		background: var(--border);
		margin: 8px 0 4px;
	}

	.field-section-title {
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
		margin: 0 0 4px;
	}

	.approver-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-bottom: 8px;
	}

	/* Not `<Badge>`: a selected-approver token that wraps its own remove button
	   — a flex container with a child control, and it renders a person's name,
	   not a status. Colour comes from the palette pair. */
	.approver-chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 3px 8px 3px 10px;
		border-radius: 14px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.78rem;
		font-weight: 500;
	}

	.chip-remove {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		border: none;
		background: none;
		color: var(--accent);
		font-size: 1rem;
		line-height: 1;
		cursor: pointer;
		border-radius: 50%;
		padding: 0;
	}

	/* Deliberately still a literal, and NOT `--accent-tint`: this is a hover
	   step that has to read as stronger than the chip it sits inside, which is
	   already on `--accent-tint`. A tone token would make the hover invisible.
	   The pairing here is `--text` over the composite, not a tint/on-tint
	   pair. */
	.chip-remove:hover {
		background: rgba(99, 140, 255, 0.2);
		color: var(--text);
	}

	.approver-search-wrap {
		position: relative;
	}

	.approver-search {
		width: 100%;
		box-sizing: border-box;
	}

	.approver-dropdown {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		max-height: 180px;
		overflow-y: auto;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
		z-index: 20;
		margin-top: 4px;
	}

	.approver-option {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 12px;
		border: none;
		background: none;
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
		text-align: left;
		font-family: inherit;
	}

	.approver-option:hover {
		background: rgba(99, 140, 255, 0.08);
	}

	.approver-name {
		font-weight: 500;
		color: var(--text);
	}

	.approver-email {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.approver-empty {
		padding: 10px 12px;
		font-size: 0.82rem;
		color: var(--text-muted);
		text-align: center;
	}

	/* Toggle switch */
	.toggle-field {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.toggle-field label {
		margin-bottom: 0;
	}

	.toggle {
		position: relative;
		width: 40px;
		height: 22px;
		border-radius: 12px;
		border: none;
		background: var(--border);
		cursor: pointer;
		padding: 0;
		transition: background 0.2s;
	}

	.toggle.on {
		background: var(--accent);
	}

	.toggle-knob {
		position: absolute;
		top: 3px;
		left: 3px;
		width: 16px;
		height: 16px;
		border-radius: 50%;
		background: #fff;
		transition: transform 0.2s;
	}

	.toggle.on .toggle-knob {
		transform: translateX(18px);
	}

	/* Range slider */
	.range-row {
		display: flex;
		align-items: center;
		gap: 12px;
	}

	.range-row input[type='range'] {
		flex: 1;
		accent-color: var(--accent);
	}

	.range-value {
		font-size: 0.88rem;
		font-weight: 600;
		color: var(--accent);
		min-width: 42px;
		text-align: right;
		font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
	}

	/* Config footer */
	.config-footer {
		padding: 14px 20px;
		border-top: 1px solid var(--border);
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.move-btns {
		display: flex;
		gap: 6px;
	}

	.move-btn {
		padding: 5px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}

	.move-btn:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}

	.move-btn:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}

	.remove-btn {
		padding: 5px 12px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.8rem;
		cursor: pointer;
		font-family: inherit;
	}

	.remove-btn:hover {
		border-color: var(--danger);
		color: var(--danger);
	}

	.no-selection {
		padding: 40px 20px;
		text-align: center;
		color: var(--text-muted);
	}

	@media (max-width: 1100px) {
		.editor {
			grid-template-columns: 1fr;
		}
	}
</style>
