<script lang="ts">
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import { goto } from '$app/navigation';
	import { auth } from '#lib/stores/auth.svelte.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import DataTable from '#lib/components/ui/DataTable.svelte';
	import Modal from '#lib/components/ui/Modal.svelte';
	import RowLink from '#lib/components/ui/RowLink.svelte';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import FieldWarning from '#lib/components/ui/FieldWarning.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { isRowOpenClick } from '#lib/utils/rowNav.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import { accentStrongContrast } from '#lib/stores/brandTheme.ts';
	import { formatRatio, WCAG_AA_NORMAL } from '#lib/a11y/contrast.ts';
	import {
		getPartnerOverview,
		getChildBranding,
		updateChildBranding,
		mintLinkCode,
		attachChild,
		provisionChild,
		detachChild
	} from '#lib/api/partner.ts';
	import type {
		ChildBranding,
		ChildTenant,
		PartnerOverview,
		ProvisionedChild
	} from '#lib/types/partner.ts';

	// RBAC: the backend gates every /api/partner endpoint to admin only and 403s
	// the rest. Wait for `auth.user` to resolve before redirecting so we don't
	// bounce before /me lands (the api-keys / billing pages do the same).
	const userLoaded = $derived(auth.user !== null);
	const allowed = $derived(auth.isAdmin);

	$effect(() => {
		if (userLoaded && !allowed) goto('/');
	});

	let overview = $state<PartnerOverview | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	// Narrowed away from null so the DataTable `body` snippet (a closure, which
	// loses the `{:else if overview}` narrowing) can read the children.
	const children = $derived(overview ? overview.children : []);

	// $derived so the column headers re-render when the locale changes.
	let COLUMNS = $derived([
		{ label: m('admin.partner.col.tenant') },
		{ label: m('admin.partner.col.slug') },
		{ label: m('admin.partner.col.plan') },
		{ label: m('admin.partner.col.brandName') },
		{ label: '', class: 'actions-col' }
	]);

	async function load() {
		loading = true;
		error = null;
		try {
			overview = await getPartnerOverview();
		} catch (e) {
			error = e instanceof Error ? e.message : m('admin.partner.loadFailed');
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		// Only fetch once the role is allowed (avoids a guaranteed 403 before the
		// redirect fires).
		if (userLoaded && allowed) load();
	});

	// Brand-edit modal state.
	let editingChild = $state<ChildTenant | null>(null);
	let brand = $state<ChildBranding | null>(null);
	let brandLoading = $state(false);
	let brandError = $state<string | null>(null);
	let saving = $state(false);

	// A partner pushes this brand into the CHILD tenant's `--accent-strong`,
	// whose one contract is that white text sits on it — so an unreadable
	// choice here lands on someone else's users. Same advisory as the org
	// Branding panel, from the same helper.
	const childAccentStrongRatio = $derived(accentStrongContrast(brand?.accent_strong_color));
	const childAccentStrongFails = $derived(
		childAccentStrongRatio !== null && childAccentStrongRatio < WCAG_AA_NORMAL
	);

	const HEX_RE = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;
	const URL_RE = /^https?:\/\//i;

	async function openEdit(child: ChildTenant) {
		editingChild = child;
		brand = null;
		brandError = null;
		brandLoading = true;
		try {
			const loaded = await getChildBranding(child.id);
			// Request-identity guard, the same rule `goods-receipts` states: open
			// one child, close it, open another, and the FIRST response must not
			// land in the dialog now showing the second. Here it is not cosmetic —
			// `saveBrand` PUTs these fields to `editingChild.id`, so a late
			// response would push one tenant's white-label brand onto ANOTHER
			// tenant's users, under a heading naming the second.
			if (editingChild?.id !== child.id) return;
			brand = loaded;
		} catch (e) {
			if (editingChild?.id !== child.id) return;
			brandError = e instanceof Error ? e.message : m('admin.partner.branding.loadFailed');
		} finally {
			if (editingChild?.id === child.id) brandLoading = false;
		}
	}

	function closeEdit() {
		editingChild = null;
		brand = null;
	}

	async function saveBrand() {
		if (!editingChild || !brand) return;
		// Client-side validation mirrors the backend BrandConfig guards so a typo
		// surfaces inline instead of as a 422. The backend still validates.
		const b = brand;
		if (b.accent_color.trim() && !HEX_RE.test(b.accent_color.trim())) {
			toast(m('admin.partner.toast.accentInvalid'), 'error');
			return;
		}
		if (b.accent_strong_color.trim() && !HEX_RE.test(b.accent_strong_color.trim())) {
			toast(m('admin.partner.toast.accentStrongInvalid'), 'error');
			return;
		}
		for (const [val, label] of [
			[b.logo_url, m('admin.partner.label.logoUrl')],
			[b.support_url, m('admin.partner.label.supportUrl')],
			[b.legal_url, m('admin.partner.label.legalUrl')]
		] as const) {
			if (val.trim() && !URL_RE.test(val.trim())) {
				toast(m('admin.partner.toast.urlInvalid', { label }), 'error');
				return;
			}
		}
		saving = true;
		try {
			await updateChildBranding(editingChild.id, {
				product_name: b.product_name.trim(),
				logo_url: b.logo_url.trim(),
				accent_color: b.accent_color.trim(),
				accent_strong_color: b.accent_strong_color.trim(),
				support_url: b.support_url.trim(),
				legal_url: b.legal_url.trim()
			});
			toast(m('admin.partner.toast.brandingSaved', { name: editingChild.name }), 'success');
			closeEdit();
			// Refresh the list so the brand-name column reflects the change.
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.partner.toast.brandingSaveFailed'), 'error');
		} finally {
			saving = false;
		}
	}

	// ── Link-code mint (this workspace consents to being a child) ──────────────
	let mintedCode = $state<string | null>(null);
	let mintExpiry = $state<number | null>(null);
	let minting = $state(false);

	async function mintCode() {
		minting = true;
		try {
			const res = await mintLinkCode();
			mintedCode = res.link_code;
			mintExpiry = res.expires_in_minutes;
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.partner.toast.mintFailed'), 'error');
		} finally {
			minting = false;
		}
	}

	async function copyCode() {
		if (!mintedCode) return;
		try {
			await navigator.clipboard.writeText(mintedCode);
			toast(m('admin.partner.toast.codeCopied'), 'success');
		} catch {
			toast(m('admin.partner.toast.copyFailed'), 'error');
		}
	}

	// ── Attach a consenting child (redeem a code its admin minted) ─────────────
	let showAttach = $state(false);
	let attachCodeInput = $state('');
	let attaching = $state(false);

	function openAttach() {
		attachCodeInput = '';
		showAttach = true;
	}

	async function submitAttach() {
		const code = attachCodeInput.trim();
		if (!code) {
			toast(m('admin.partner.toast.pasteCode'), 'error');
			return;
		}
		attaching = true;
		try {
			const child = await attachChild(code);
			toast(m('admin.partner.toast.attached', { name: child.name }), 'success');
			showAttach = false;
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.partner.toast.attachFailed'), 'error');
		} finally {
			attaching = false;
		}
	}

	// ── Provision a brand-NEW child tenant under this partner ───────────────────
	let showProvision = $state(false);
	let provName = $state('');
	let provSlug = $state('');
	let provEmail = $state('');
	let provisioning = $state(false);
	// The new child, and whether its admin was emailed their set-password link.
	// No credential ever reaches the partner (decisions §283).
	let provisioned = $state<ProvisionedChild | null>(null);

	const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/;
	const EMAIL_RE = /^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+$/;

	function openProvision() {
		provName = '';
		provSlug = '';
		provEmail = '';
		provisioned = null;
		showProvision = true;
	}

	async function submitProvision() {
		const name = provName.trim();
		const slug = provSlug.trim().toLowerCase();
		const email = provEmail.trim();
		if (!name) {
			toast(m('admin.partner.toast.enterName'), 'error');
			return;
		}
		if (!SLUG_RE.test(slug)) {
			toast(m('admin.partner.toast.invalidSlug'), 'error');
			return;
		}
		if (!EMAIL_RE.test(email)) {
			toast(m('admin.partner.toast.invalidEmail'), 'error');
			return;
		}
		provisioning = true;
		try {
			const child = await provisionChild({ name, slug, admin_email: email });
			provisioned = child;
			toast(m('admin.partner.toast.provisioned', { name: child.name }), 'success');
			// Refresh the children list so the new tenant appears immediately.
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.partner.toast.provisionFailed'), 'error');
		} finally {
			provisioning = false;
		}
	}

	function closeProvision() {
		showProvision = false;
		provisioned = null;
	}

	// ── Detach a child (armed two-click confirm) ───────────────────────────────
	let confirmDetachId = $state<string | null>(null);
	// The child whose detach is in flight. Its button goes `busy` so the
	// still-armed confirm cannot send a second request; other rows' Detach is
	// disabled until it settles.
	let detachingId = $state<string | null>(null);

	async function handleDetach(child: ChildTenant) {
		if (detachingId) return;
		if (confirmDetachId !== child.id) {
			confirmDetachId = child.id;
			return;
		}
		detachingId = child.id;
		try {
			await detachChild(child.id);
			toast(m('admin.partner.toast.detached', { name: child.name }), 'success');
			confirmDetachId = null;
			await load();
		} catch (e) {
			toast(e instanceof Error ? e.message : m('admin.partner.toast.detachFailed'), 'error');
		} finally {
			detachingId = null;
		}
	}
</script>

<!-- Un-arm a pending Detach when clicking outside any row action. -->
<svelte:window
	onclick={(e) => {
		if (confirmDetachId && !(e.target as HTMLElement)?.closest?.('.row-action')) {
			confirmDetachId = null;
		}
	}}
/>

<PageHeader title={m('admin.partner.title')}>
	{#snippet actions()}
		<button
			type="button"
			class="btn-cancel"
			onclick={openProvision}
			data-testid="provision-child-btn"
		>
			{m('admin.partner.createChild')}
		</button>
		<button type="button" class="btn-primary" onclick={openAttach} data-testid="attach-child-btn">
			{m('admin.partner.attachChild')}
		</button>
	{/snippet}

	<p class="page-hint">
		{m('admin.partner.hint')}
		<HelpTip term="organization" />
	</p>

	<!-- Link-code panel: this workspace consents to being attached AS a child. A
	     partner then redeems the code to link us under their account. -->
	<section class="link-code-panel" data-testid="link-code-panel">
		<h2>{m('admin.partner.join.heading')}</h2>
		<p class="panel-hint">
			{m('admin.partner.join.hint')}
		</p>
		<div class="link-code-actions">
			<button type="button" class="btn-cancel" onclick={mintCode} disabled={minting}>
				{minting ? m('admin.partner.join.generating') : m('admin.partner.join.generate')}
			</button>
		</div>
		{#if mintedCode}
			<div class="minted" data-testid="minted-link-code">
				<code class="code-value">{mintedCode}</code>
				<button type="button" class="btn-cancel copy-btn" onclick={copyCode}>{m('admin.partner.join.copy')}</button>
				{#if mintExpiry !== null}
					<span class="expiry">{m('admin.partner.join.expiresIn', { minutes: mintExpiry })}</span>
				{/if}
			</div>
		{/if}
	</section>

	{#if loading}
		<LoadingState testId="partner-loading" />
	{:else if error}
		<LoadError message={error} onretry={load} testId="partner-error" />
	{:else if overview && !overview.is_partner}
		<div class="state" data-testid="partner-empty">
			<p>
				{m('admin.partner.notPartner')}
			</p>
		</div>
	{:else if overview}
		<DataTable columns={COLUMNS} isEmpty={overview.children.length === 0} empty={m('admin.partner.empty')}>
			{#snippet body()}
				{#each children as child (child.id)}
					<tr
						class="clickable"
						onclick={(e) => {
							if (isRowOpenClick(e)) openEdit(child);
						}}
					>
						<td>
							<RowLink
								onclick={() => openEdit(child)}
								ariaLabel={m('admin.partner.editBrandingAria', { name: child.name })}
							>
								{child.name}
							</RowLink>
						</td>
						<td class="mono">{child.slug}</td>
						<td>{child.plan}</td>
						<td>{child.product_name || '—'}</td>
						<td class="actions">
							<RowAction
								variant="danger"
								armed={confirmDetachId === child.id}
								busy={detachingId === child.id}
								disabled={detachingId !== null && detachingId !== child.id}
								onclick={() => handleDetach(child)}
								ariaLabel={m('common.rowActionAria', {
									action:
										confirmDetachId === child.id
											? m('admin.partner.row.confirmDetach')
											: m('admin.partner.row.detach'),
									name: child.name
								})}
							>
								{confirmDetachId === child.id ? m('admin.partner.row.confirmDetach') : m('admin.partner.row.detach')}
							</RowAction>
						</td>
					</tr>
				{/each}
			{/snippet}
		</DataTable>
	{/if}
</PageHeader>

<!-- Edit child branding modal -->
<Modal
	open={editingChild !== null}
	ariaLabel={m('admin.partner.branding.aria')}
	width="md"
	onclose={closeEdit}
>
	{#if editingChild}
		<h2>{m('admin.partner.branding.heading', { name: editingChild.name })}</h2>
		{#if brandLoading}
			<LoadingState label={m('admin.partner.branding.loading')} testId="brand-loading" />
		{:else if brandError}
			<LoadError
				message={brandError}
				onretry={() => openEdit(editingChild!)}
				testId="partner-brand-error"
			/>
		{:else if brand}
			<form
				onsubmit={(e) => {
					e.preventDefault();
					saveBrand();
				}}
			>
				<label>
					<span>{m('admin.partner.field.productName')}</span>
					<input
						type="text"
						bind:value={brand.product_name}
						maxlength="120"
						placeholder={m('admin.partner.field.productNamePlaceholder')}
					/>
				</label>
				<label>
					<span>{m('admin.partner.field.logoUrl')}</span>
					<input type="url" bind:value={brand.logo_url} placeholder={m('admin.partner.field.logoUrlPlaceholder')} />
				</label>
				<div class="color-row">
					<label>
						<span>{m('admin.partner.field.accentColor')}</span>
						<input type="text" bind:value={brand.accent_color} placeholder={m('admin.partner.field.accentColorPlaceholder')} />
					</label>
					<label>
						<span>{m('admin.partner.field.strongAccent')}</span>
						<input type="text" bind:value={brand.accent_strong_color} placeholder={m('admin.partner.field.strongAccentPlaceholder')} />
						<FieldWarning
							show={childAccentStrongFails}
							testId="child-accent-strong-contrast-warning"
							message={m('common.contrastWarning', {
								ratio: childAccentStrongRatio === null ? '' : formatRatio(childAccentStrongRatio)
							})}
						/>
					</label>
				</div>
				<label>
					<span>{m('admin.partner.field.supportUrl')}</span>
					<input type="url" bind:value={brand.support_url} placeholder={m('admin.partner.field.supportUrlPlaceholder')} />
				</label>
				<label>
					<span>{m('admin.partner.field.legalUrl')}</span>
					<input type="url" bind:value={brand.legal_url} placeholder={m('admin.partner.field.legalUrlPlaceholder')} />
				</label>
				<div class="modal-footer">
					<button type="button" class="btn-cancel" onclick={closeEdit}>{m('common.cancel')}</button>
					<button type="submit" class="btn-primary" disabled={saving}>
						{saving ? m('admin.partner.branding.saving') : m('admin.partner.branding.save')}
					</button>
				</div>
			</form>
		{/if}
	{/if}
</Modal>

<!-- Attach a consenting child by redeeming the code its admin minted -->
<Modal
	open={showAttach}
	ariaLabel={m('admin.partner.attach.aria')}
	title={m('admin.partner.attach.title')}
	width="sm"
	onclose={() => (showAttach = false)}
>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			submitAttach();
		}}
	>
		<p class="modal-hint">
			{m('admin.partner.attach.hint')}
		</p>
		<label>
			<span>{m('admin.partner.attach.linkCode')} <em class="required">*</em></span>
			<input
				type="text"
				bind:value={attachCodeInput}
				placeholder={m('admin.partner.attach.linkCodePlaceholder')}
				data-testid="attach-code-input"
			/>
		</label>
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={() => (showAttach = false)}>{m('common.cancel')}</button>
			<button type="submit" class="btn-primary" disabled={attaching}>
				{attaching ? m('admin.partner.attach.attaching') : m('admin.partner.attach.attach')}
			</button>
		</div>
	</form>
</Modal>

<!-- Provision a brand-new child tenant already parented to this partner -->
<Modal
	open={showProvision}
	ariaLabel={m('admin.partner.provision.aria')}
	title={m('admin.partner.provision.title')}
	width="sm"
	onclose={closeProvision}
>
	{#if provisioned}
		<!-- Result: the new tenant, and where its admin's set-password link went.
		     Never a credential — the link reaches only the admin's inbox. -->
		<div class="provisioned-result" data-testid="provisioned-result">
			<p class="modal-hint">
				{provisioned.set_password_email_sent && provisioned.set_password_link_expires_at
					? m('admin.partner.provision.resultSent', {
							name: provisioned.name,
							slug: provisioned.slug,
							expires: formatDate(provisioned.set_password_link_expires_at, '—', {
								month: 'short',
								day: 'numeric',
								hour: 'numeric',
								minute: '2-digit'
							})
						})
					: m('admin.partner.provision.resultNotSent', {
							name: provisioned.name,
							slug: provisioned.slug
						})}
			</p>
			<dl class="cred">
				<dt>{m('admin.partner.provision.adminEmail')}</dt>
				<dd class="mono">{provisioned.admin_email}</dd>
			</dl>
			<div class="modal-footer">
				<button type="button" class="btn-primary" onclick={closeProvision}>{m('admin.partner.provision.done')}</button>
			</div>
		</div>
	{:else}
		<form
			onsubmit={(e) => {
				e.preventDefault();
				submitProvision();
			}}
		>
			<p class="modal-hint">
				{m('admin.partner.provision.formHint')}
			</p>
			<label>
				<span>{m('admin.partner.field.companyName')} <em class="required">*</em></span>
				<input
					type="text"
					bind:value={provName}
					maxlength="200"
					placeholder={m('admin.partner.field.companyNamePlaceholder')}
					data-testid="provision-name-input"
				/>
			</label>
			<label>
				<span>{m('admin.partner.field.slug')} <em class="required">*</em></span>
				<input
					type="text"
					bind:value={provSlug}
					maxlength="63"
					placeholder={m('admin.partner.field.slugPlaceholder')}
					data-testid="provision-slug-input"
				/>
			</label>
			<label>
				<span>{m('admin.partner.field.adminEmail')} <em class="required">*</em></span>
				<input
					type="email"
					bind:value={provEmail}
					placeholder={m('admin.partner.field.adminEmailPlaceholder')}
					data-testid="provision-email-input"
				/>
			</label>
			<div class="modal-footer">
				<button type="button" class="btn-cancel" onclick={closeProvision}>{m('common.cancel')}</button>
				<button type="submit" class="btn-primary" disabled={provisioning}>
					{provisioning ? m('admin.partner.provision.creating') : m('admin.partner.provision.create')}
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

	.state {
		color: var(--text-muted);
		padding: 0.75rem 0;
	}

	.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}

	.color-row {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
	}

	.link-code-panel {
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 1rem 1.25rem;
		background: var(--surface);
	}

	.link-code-panel h2 {
		margin: 0 0 0.25rem;
		font-size: 1rem;
	}

	.panel-hint {
		margin: 0 0 0.75rem;
		color: var(--text-muted);
		font-size: 0.82rem;
		max-width: 640px;
	}

	.link-code-actions {
		display: flex;
		gap: 0.5rem;
	}

	.minted {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		margin-top: 0.75rem;
		flex-wrap: wrap;
	}

	.code-value {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.78rem;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 0.4rem 0.6rem;
		max-width: 100%;
		overflow-wrap: anywhere;
	}

	.expiry {
		color: var(--text-muted);
		font-size: 0.78rem;
	}

	.modal-hint {
		margin: 0 0 0.75rem;
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	.cred {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.4rem 0.9rem;
		align-items: center;
		margin: 0 0 0.5rem;
	}

	.cred dt {
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	.cred dd {
		margin: 0;
		font-size: 0.85rem;
	}

	.copy-btn {
		flex: none;
	}
</style>
