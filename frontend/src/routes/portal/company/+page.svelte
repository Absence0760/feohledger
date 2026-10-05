<script lang="ts">
	import { portalCompany } from '$lib/stores/portalCompany.svelte';
	import { portalAuth } from '$lib/stores/portalAuth.svelte';
	import { onMount } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';

	let email = $state('');
	let phone = $state('');
	let address = $state('');
	let contactSaving = $state(false);
	let contactMsg = $state('');
	let contactErr = $state('');

	// --- MFA (two-factor) ---
	const mfaEnabled = $derived(portalAuth.user?.mfa_enabled ?? false);
	let mfaEnroll = $state<{ secret: string; qr_code_data_url: string } | null>(null);
	let mfaCode = $state('');
	let mfaBusy = $state(false);
	let mfaErr = $state('');
	let mfaMsg = $state('');
	// Disable form: a current code re-verifies before MFA is turned off.
	let disableMode = $state(false);
	let disableCode = $state('');

	async function startEnroll() {
		mfaErr = '';
		mfaMsg = '';
		mfaBusy = true;
		try {
			const res = await portalAuth.startMfaEnrollment();
			mfaEnroll = { secret: res.secret, qr_code_data_url: res.qr_code_data_url };
			mfaCode = '';
		} catch (err) {
			mfaErr = err instanceof Error ? err.message : m('portal.company.mfa.startFailed');
		} finally {
			mfaBusy = false;
		}
	}

	async function confirmEnroll(e: Event) {
		e.preventDefault();
		mfaErr = '';
		mfaBusy = true;
		try {
			await portalAuth.verifyMfaEnrollment(mfaCode);
			mfaEnroll = null;
			mfaCode = '';
			mfaMsg = m('portal.company.mfa.enabledMsg');
		} catch (err) {
			mfaErr = err instanceof Error ? err.message : m('portal.company.mfa.invalidCode');
		} finally {
			mfaBusy = false;
		}
	}

	async function confirmDisable(e: Event) {
		e.preventDefault();
		mfaErr = '';
		mfaBusy = true;
		try {
			await portalAuth.disableMfa(disableCode);
			disableMode = false;
			disableCode = '';
			mfaMsg = m('portal.company.mfa.disabledMsg');
		} catch (err) {
			mfaErr = err instanceof Error ? err.message : m('portal.company.mfa.invalidCode');
		} finally {
			mfaBusy = false;
		}
	}

	// Bank-detail change fields (staged, not applied live).
	let bankName = $state('');
	let accountNumber = $state('');
	let routingNumber = $state('');
	// Larger US banks publish a SEPARATE ABA for incoming wires. Optional: left
	// blank, the wire rail falls back to the ACH routing number above — which is
	// what a bank publishing a single number means.
	let wireRoutingNumber = $state('');
	let sortCode = $state('');
	let bankCountry = $state('');
	// GB uses a 6-digit sort code, not a 9-digit US ABA routing number — the
	// country field picks which one this form collects + submits. Backend
	// validation mirrors this: `schemas.portal.PortalBankChangeRequest` checks
	// whichever key is actually present via `validate_aba_routing` /
	// `validate_uk_sort_code`, never both required.
	const bankIsUK = $derived(bankCountry.trim().toUpperCase() === 'GB');
	let bankSaving = $state(false);
	let bankErr = $state('');

	// Tax-ID change (staged, not applied live).
	let taxId = $state('');
	let taxSaving = $state(false);
	let taxErr = $state('');

	// Tax-form (W-9 / W-8) upload — applies live (it's a document, not a routing target).
	let taxFormType = $state('w9');
	let taxFormFile = $state<File | null>(null);
	let taxFormSaving = $state(false);
	let taxFormErr = $state('');
	let taxFormMsg = $state('');
	let downloadingForm = $state(false);

	const info = $derived(portalCompany.info);
	const pending = $derived(portalCompany.info?.pending_change ?? null);
	const taxForm = $derived(portalCompany.taxForm);

	const FORM_TYPE_LABEL: Record<string, string> = {
		w9: m('portal.company.taxForm.w9Label'),
		w8: m('portal.company.taxForm.w8Label')
	};

	async function load() {
		await portalCompany.fetchCompany();
		const i = portalCompany.info;
		if (i) {
			email = i.email ?? '';
			phone = i.phone ?? '';
			address = i.address ?? '';
		}
		try {
			await portalCompany.fetchTaxForm();
			if (portalCompany.taxForm) taxFormType = portalCompany.taxForm.suggested_form_type;
		} catch {
			// Non-fatal — the rest of the page still renders.
		}
	}

	function onTaxFormFile(e: Event) {
		const target = e.target as HTMLInputElement;
		taxFormFile = target.files && target.files.length ? target.files[0] : null;
	}

	async function submitTaxForm(e: Event) {
		e.preventDefault();
		if (!taxFormFile) return;
		taxFormSaving = true;
		taxFormErr = '';
		taxFormMsg = '';
		try {
			await portalCompany.uploadTaxForm(taxFormFile, taxFormType);
			taxFormFile = null;
			taxFormMsg = m('portal.company.taxForm.uploaded');
		} catch (err) {
			taxFormErr = err instanceof Error ? err.message : m('portal.company.taxForm.uploadFailed');
		} finally {
			taxFormSaving = false;
		}
	}

	async function downloadTaxForm() {
		downloadingForm = true;
		taxFormErr = '';
		try {
			const blob = await portalCompany.downloadTaxForm();
			const url = URL.createObjectURL(blob);
			const a = document.createElement('a');
			a.href = url;
			a.download = `tax-form-${taxForm?.form_type ?? 'document'}`;
			document.body.appendChild(a);
			a.click();
			a.remove();
			URL.revokeObjectURL(url);
		} catch (err) {
			taxFormErr = err instanceof Error ? err.message : m('portal.company.taxForm.downloadFailed');
		} finally {
			downloadingForm = false;
		}
	}

	async function saveContact(e: Event) {
		e.preventDefault();
		contactSaving = true;
		contactMsg = '';
		contactErr = '';
		try {
			await portalCompany.updateContact({ email, phone, address });
			contactMsg = m('portal.company.contact.saved');
		} catch (err) {
			contactErr = err instanceof Error ? err.message : m('portal.company.contact.saveFailed');
		} finally {
			contactSaving = false;
		}
	}

	async function submitBankChange(e: Event) {
		e.preventDefault();
		bankSaving = true;
		bankErr = '';
		try {
			await portalCompany.requestBankChange({
				bank_name: bankName,
				account_number: accountNumber,
				...(bankIsUK
					? { sort_code: sortCode }
					: {
							routing_number: routingNumber,
							// Omitted entirely when blank, so an empty string can't
							// be stored and then fail the ABA check at approval.
							...(wireRoutingNumber.trim()
								? { wire_routing_number: wireRoutingNumber.trim() }
								: {})
						}),
				...(bankCountry ? { country: bankCountry } : {})
			});
			accountNumber = '';
			routingNumber = '';
			wireRoutingNumber = '';
			sortCode = '';
			bankName = '';
			bankCountry = '';
		} catch (err) {
			bankErr = err instanceof Error ? err.message : m('portal.company.bank.requestFailed');
		} finally {
			bankSaving = false;
		}
	}

	async function submitTaxChange(e: Event) {
		e.preventDefault();
		taxSaving = true;
		taxErr = '';
		try {
			await portalCompany.requestTaxIdChange(taxId);
			taxId = '';
		} catch (err) {
			taxErr = err instanceof Error ? err.message : m('portal.company.tax.requestFailed');
		} finally {
			taxSaving = false;
		}
	}

	onMount(load);
</script>

<div class="page">
	<header>
		<h1>{m('portal.company.title')}</h1>
		{#if info}<p class="sub">{info.name}</p>{/if}
	</header>

	{#if pending}
		<div class="banner">
			{m('portal.company.pendingBanner', { changeType: pending.change_type.replace('_', ' ') })}
		</div>
	{/if}

	<!-- Live-apply contact fields -->
	<section class="card">
		<h2>{m('portal.company.contact.title')}</h2>
		<p class="note">{m('portal.company.contact.note')}</p>
		{#if contactErr}<div class="error" role="alert">{contactErr}</div>{/if}
		<div role="status">{#if contactMsg}<div class="message">{contactMsg}</div>{/if}</div>
		<form onsubmit={saveContact}>
			<label>
				{m('portal.company.contact.email')}
				<input type="email" bind:value={email} />
			</label>
			<label>
				{m('portal.company.contact.phone')}
				<input type="text" bind:value={phone} />
			</label>
			<label>
				{m('portal.company.contact.address')}
				<input type="text" bind:value={address} />
			</label>
			<button type="submit" class="btn-primary" disabled={contactSaving}>
				{contactSaving ? m('portal.company.contact.saving') : m('portal.company.contact.save')}
			</button>
		</form>
	</section>

	<!-- Staged: bank details -->
	<section class="card">
		<h2>{m('portal.company.bank.title')}</h2>
		<p class="note">
			{m('portal.company.bank.note')}
			{#if info?.has_bank_details}<span class="tag">{m('portal.company.bank.onFile')}</span>{/if}
		</p>
		{#if bankErr}<div class="error" role="alert">{bankErr}</div>{/if}
		<form onsubmit={submitBankChange}>
			<label>
				{m('portal.company.bank.name')}
				<input type="text" bind:value={bankName} />
			</label>
			<label>
				{m('portal.company.bank.account')}
				<input type="text" bind:value={accountNumber} autocomplete="off" />
			</label>
			<label>
				{m('portal.company.bank.country')}
				<input type="text" maxlength="2" bind:value={bankCountry} autocomplete="off" />
			</label>
			{#if bankIsUK}
				<label>
					{m('portal.company.bank.sortCode')}
					<input type="text" bind:value={sortCode} autocomplete="off" />
				</label>
			{:else}
				<label>
					{m('portal.company.bank.routing')}
					<input type="text" bind:value={routingNumber} autocomplete="off" />
				</label>
				<label>
					{m('portal.company.bank.wireRouting')}
					<input type="text" bind:value={wireRoutingNumber} autocomplete="off" />
				</label>
				<p class="note">{m('portal.company.bank.wireRoutingHint')}</p>
			{/if}
			<button
				type="submit"
				class="btn-primary"
				disabled={bankSaving || !!pending || !accountNumber}
			>
				{bankSaving ? m('portal.company.bank.submitting') : m('portal.company.bank.request')}
			</button>
		</form>
	</section>

	<!-- Staged: tax ID -->
	<section class="card">
		<h2>{m('portal.company.tax.title')}</h2>
		<p class="note">
			{m('portal.company.tax.note')}
			{#if info?.tax_id_last4}<span class="tag">{m('portal.company.tax.ending', { last4: info.tax_id_last4 })}</span>{/if}
		</p>
		{#if taxErr}<div class="error" role="alert">{taxErr}</div>{/if}
		<form onsubmit={submitTaxChange}>
			<label>
				{m('portal.company.tax.new')}
				<input type="text" bind:value={taxId} autocomplete="off" />
			</label>
			<button type="submit" class="btn-primary" disabled={taxSaving || !!pending || !taxId}>
				{taxSaving ? m('portal.company.tax.submitting') : m('portal.company.tax.request')}
			</button>
		</form>
	</section>

	<!-- Tax forms: W-9 (US) / W-8 (foreign) — uploaded live -->
	<section class="card">
		<h2>{m('portal.company.taxForm.title')}</h2>
		<p class="note">
			{m('portal.company.taxForm.note')}
			{#if taxForm?.on_file}
				<span class="tag">
					{FORM_TYPE_LABEL[taxForm.form_type ?? ''] ?? m('portal.company.taxForm.onFile')}
					{#if taxForm.received_date}{m('portal.company.taxForm.received', { date: taxForm.received_date })}{/if}
				</span>
			{/if}
		</p>
		{#if taxFormErr}<div class="error" role="alert">{taxFormErr}</div>{/if}
		<div role="status">{#if taxFormMsg}<div class="message">{taxFormMsg}</div>{/if}</div>

		{#if taxForm?.on_file}
			<p class="note">
				{m('portal.company.taxForm.onFileNote')}
				<button
					type="button"
					class="btn-link"
					onclick={downloadTaxForm}
					disabled={downloadingForm}
				>
					{downloadingForm ? m('portal.company.taxForm.preparing') : m('portal.company.taxForm.download')}
				</button>
			</p>
		{/if}

		<form onsubmit={submitTaxForm}>
			<label>
				{m('portal.company.taxForm.formType')}
				<select bind:value={taxFormType}>
					<option value="w9">{m('portal.company.taxForm.optionW9')}</option>
					<option value="w8">{m('portal.company.taxForm.optionW8')}</option>
				</select>
			</label>
			<label>
				{m('portal.company.taxForm.signedForm')}
				<input
					type="file"
					accept="application/pdf,image/png,image/jpeg,image/tiff"
					onchange={onTaxFormFile}
				/>
			</label>
			<button type="submit" class="btn-primary" disabled={taxFormSaving || !taxFormFile}>
				{taxFormSaving ? m('portal.company.taxForm.uploading') : taxForm?.on_file ? m('portal.company.taxForm.replace') : m('portal.company.taxForm.upload')}
			</button>
		</form>
	</section>

	<!-- Security: two-factor authentication (TOTP) -->
	<section class="card">
		<h2>{m('portal.company.mfa.title')}</h2>
		<p class="note">
			{m('portal.company.mfa.note')}
			{#if mfaEnabled}<span class="tag">{m('portal.company.mfa.on')}</span>{/if}
		</p>
		{#if mfaErr}<div class="error" role="alert">{mfaErr}</div>{/if}
		<div role="status">{#if mfaMsg}<div class="message">{mfaMsg}</div>{/if}</div>

		{#if mfaEnabled && !disableMode}
			<button type="button" class="btn-danger" onclick={() => (disableMode = true)}>
				{m('portal.company.mfa.turnOff')}
			</button>
		{:else if disableMode}
			<form onsubmit={confirmDisable}>
				<label>
					{m('portal.company.mfa.enterCodeToDisable')}
					<input
						type="text"
						inputmode="numeric"
						autocomplete="one-time-code"
						bind:value={disableCode}
						maxlength="8"
					/>
				</label>
				<div class="btn-row">
					<button
						type="button"
						class="btn-cancel"
						onclick={() => {
							disableMode = false;
							disableCode = '';
							mfaErr = '';
						}}>{m('portal.company.mfa.cancel')}</button
					>
					<button type="submit" class="btn-danger" disabled={mfaBusy || !disableCode}>
						{mfaBusy ? m('portal.company.mfa.turningOff') : m('portal.company.mfa.turnOffBtn')}
					</button>
				</div>
			</form>
		{:else if mfaEnroll}
			<p class="note">{m('portal.company.mfa.scanHint')}</p>
			<img class="qr" src={mfaEnroll.qr_code_data_url} alt={m('portal.company.mfa.qrAlt')} />
			<p class="note">
				{m('portal.company.mfa.manualKey')} <code class="secret">{mfaEnroll.secret}</code>
			</p>
			<form onsubmit={confirmEnroll}>
				<label>
					{m('portal.company.mfa.authCode')}
					<input
						type="text"
						inputmode="numeric"
						autocomplete="one-time-code"
						bind:value={mfaCode}
						maxlength="8"
					/>
				</label>
				<div class="btn-row">
					<button
						type="button"
						class="btn-cancel"
						onclick={() => {
							mfaEnroll = null;
							mfaCode = '';
							mfaErr = '';
						}}>{m('portal.company.mfa.cancel')}</button
					>
					<button type="submit" class="btn-primary" disabled={mfaBusy || !mfaCode}>
						{mfaBusy ? m('portal.company.mfa.verifying') : m('portal.company.mfa.confirm')}
					</button>
				</div>
			</form>
		{:else}
			<button type="button" class="btn-primary" onclick={startEnroll} disabled={mfaBusy}>
				{mfaBusy ? m('portal.company.mfa.starting') : m('portal.company.mfa.setUp')}
			</button>
		{/if}
	</section>
</div>

<style>
	.page {
		max-width: 700px;
		margin: 0 auto;
	}
	header {
		margin-bottom: 20px;
	}
	h1 {
		margin: 0;
		font-size: 1.25rem;
	}
	.sub {
		margin: 4px 0 0;
		color: var(--text-muted);
		font-size: 0.9rem;
	}
	.banner {
		background: rgba(214, 158, 46, 0.12);
		border: 1px solid rgba(214, 158, 46, 0.4);
		color: #d4940a;
		padding: 10px 14px;
		border-radius: 4px;
		margin-bottom: 16px;
		font-size: 0.9rem;
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 18px 20px;
		margin-bottom: 18px;
	}
	h2 {
		margin: 0 0 4px;
		font-size: 1rem;
	}
	.note {
		margin: 0 0 14px;
		color: var(--text-muted);
		font-size: 0.82rem;
	}
	.tag {
		display: inline-block;
		margin-left: 6px;
		padding: 1px 8px;
		border-radius: 999px;
		background: var(--bg);
		border: 1px solid var(--border);
		font-size: 0.72rem;
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
	}
	input {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--bg);
		color: var(--text);
		font-size: 0.9rem;
	}
	.btn-primary {
		align-self: flex-start;
		padding: 8px 16px;
		border: none;
		border-radius: 4px;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.85rem;
		cursor: pointer;
	}
	.btn-primary:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.btn-link {
		background: none;
		border: none;
		color: var(--accent);
		cursor: pointer;
		padding: 0;
		font-size: inherit;
		text-decoration: underline;
	}
	.btn-link:disabled {
		opacity: 0.55;
		cursor: default;
	}
	select {
		padding: 8px 10px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--bg);
		color: var(--text);
		font-size: 0.9rem;
	}
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 8px 12px;
		border-radius: 4px;
		margin-bottom: 10px;
		font-size: 0.85rem;
	}
	.message {
		background: rgba(40, 160, 80, 0.12);
		border: 1px solid rgba(40, 160, 80, 0.35);
		color: var(--success);
		padding: 8px 12px;
		border-radius: 4px;
		margin-bottom: 10px;
		font-size: 0.85rem;
	}
	.btn-danger {
		align-self: flex-start;
		padding: 8px 16px;
		border: 1px solid rgba(224, 64, 64, 0.5);
		border-radius: 4px;
		background: transparent;
		color: var(--danger);
		font-size: 0.85rem;
		cursor: pointer;
	}
	.btn-danger:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.btn-cancel {
		padding: 8px 16px;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font-size: 0.85rem;
		cursor: pointer;
	}
	.btn-row {
		display: flex;
		gap: 8px;
	}
	.qr {
		display: block;
		width: 168px;
		height: 168px;
		border-radius: 6px;
		background: #fff;
		padding: 8px;
		margin-bottom: 12px;
	}
	.secret {
		font-family: monospace;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 2px 6px;
		font-size: 0.8rem;
		word-break: break-all;
	}
</style>
