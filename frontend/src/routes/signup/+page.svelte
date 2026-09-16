<script lang="ts">
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import LinkedMessage from '$lib/components/ui/LinkedMessage.svelte';
	import { api } from '$lib/api';
	import { onMount } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { legalTitle } from '$lib/legal/pages';

	interface PublicConfig {
		hcaptcha_sitekey: string;
		tenant_url_template: string;
	}

	interface StartResponse {
		status: string;
		message: string;
	}

	interface SlugCheckResponse {
		slug: string;
		available: boolean;
		reason?: string | null;
	}

	let companyName = $state('');
	let slug = $state('');
	let adminName = $state('');
	let adminEmail = $state('');
	let captchaToken = $state<string | null>(null);
	let captchaSitekey = $state<string>('');
	// Host/port suffix shown after the slug input, derived from the backend
	// tenant_url_template (e.g. "http://{slug}.localhost:7777" → ".localhost:7777").
	let tenantUrlSuffix = $state<string>('.localhost:7777');
	let tenantExampleHost = $state<string>('your-slug.localhost:7777');

	let slugStatus = $state<'idle' | 'checking' | 'ok' | 'bad'>('idle');
	let slugError = $state<string | null>(null);

	let submitting = $state(false);
	let error = $state('');
	let successMessage = $state<string | null>(null);

	let slugCheckTimer: ReturnType<typeof setTimeout> | null = null;

	onMount(async () => {
		try {
			const cfg = await api.get<PublicConfig>('/api/public-config');
			captchaSitekey = cfg.hcaptcha_sitekey || '';
			if (captchaSitekey) loadHCaptcha();
			if (cfg.tenant_url_template) {
				// Strip protocol + split on {slug} to get just the host-suffix portion.
				const noProtocol = cfg.tenant_url_template.replace(/^https?:\/\//, '');
				const parts = noProtocol.split('{slug}');
				if (parts.length === 2) {
					tenantUrlSuffix = parts[1];
					tenantExampleHost = `your-slug${parts[1]}`;
				}
			}
		} catch {
			// Non-fatal — backend may be unreachable; form will surface errors on submit.
		}
	});

	function loadHCaptcha() {
		if (document.querySelector('script[data-hcaptcha]')) return;
		const s = document.createElement('script');
		s.src = 'https://hcaptcha.com/1/api.js';
		s.async = true;
		s.defer = true;
		s.setAttribute('data-hcaptcha', 'true');
		(window as any).hcaptchaCallback = (token: string) => {
			captchaToken = token;
		};
		(window as any).hcaptchaExpired = () => {
			captchaToken = null;
		};
		document.head.appendChild(s);
	}

	function onSlugInput() {
		slug = slug.toLowerCase().replace(/[^a-z0-9-]/g, '');
		slugError = null;
		slugStatus = 'idle';
		if (slugCheckTimer) clearTimeout(slugCheckTimer);
		if (slug.length < 3) return;
		slugStatus = 'checking';
		slugCheckTimer = setTimeout(checkSlug, 400);
	}

	async function checkSlug() {
		try {
			const res = await api.get<SlugCheckResponse>(
				`/api/signup/slug-check?slug=${encodeURIComponent(slug)}`
			);
			if (res.available) {
				slugStatus = 'ok';
				slugError = null;
			} else {
				slugStatus = 'bad';
				slugError = res.reason || m('auth.signup.slugUnavailable');
			}
		} catch {
			slugStatus = 'idle';
		}
	}

	async function onSubmit(e: Event) {
		e.preventDefault();
		error = '';
		submitting = true;
		try {
			if (captchaSitekey && !captchaToken) {
				throw new Error(m('auth.signup.captchaRequired'));
			}
			const res = await api.post<StartResponse>('/api/signup/start', {
				company_name: companyName,
				slug,
				admin_name: adminName,
				admin_email: adminEmail,
				captcha_token: captchaToken,
			});
			successMessage = res.message;
		} catch (err) {
			error = err instanceof Error ? err.message : m('auth.signup.failed');
		} finally {
			submitting = false;
		}
	}
</script>

<svelte:head>
	<title>{m('auth.signup.pageTitle')}</title>
</svelte:head>

<div class="page">
	{#if successMessage}
		<div class="card success">
			<BrandMark size={32} />
			<h1>{m('auth.signup.successHeading')}</h1>
			<p>{successMessage}</p>
			<p class="sub next">
				{m('auth.signup.successNext')}
			</p>
			<p class="sub">{m('auth.signup.successSpamPre')}<a href="/signup">{m('auth.signup.successSpamLink')}</a>.</p>
		</div>
	{:else}
		<form class="card" onsubmit={onSubmit}>
			<BrandMark size={32} />
			<h1>{m('auth.signup.heading')}</h1>
			<p class="sub">{m('auth.signup.subtitle')}</p>

			<div role="alert" aria-live="assertive">
				{#if error}
					<div class="error">{error}</div>
				{/if}
			</div>

			<label>
				<span>{m('auth.signup.companyName')}</span>
				<input bind:value={companyName} required maxlength="255" autocomplete="organization" />
			</label>

			<label>
				<span>{m('auth.signup.workspaceUrl')}</span>
				<div class="slug-row">
					<input
						class="slug-input"
						bind:value={slug}
						oninput={onSlugInput}
						required
						minlength="3"
						maxlength="30"
						placeholder="acme"
						autocapitalize="off"
						autocorrect="off"
						spellcheck="false"
						aria-describedby="slug-hint"
						aria-invalid={slugStatus === 'bad'}
					/>
					<span class="slug-suffix">{tenantUrlSuffix}</span>
				</div>
				<div id="slug-hint" aria-live="polite">
					{#if slugStatus === 'checking'}
						<small class="hint">{m('auth.signup.slugChecking')}</small>
					{:else if slugStatus === 'ok'}
						<small class="hint ok">{m('auth.signup.slugAvailable')}</small>
					{:else if slugStatus === 'bad'}
						<small class="hint bad">{slugError}</small>
					{:else}
						<small class="hint">{m('auth.signup.slugHint')}</small>
					{/if}
				</div>
			</label>

			<label>
				<span>{m('auth.signup.yourName')}</span>
				<input bind:value={adminName} required maxlength="255" autocomplete="name" />
			</label>

			<label>
				<span>{m('auth.signup.email')}</span>
				<input type="email" bind:value={adminEmail} required maxlength="320" autocomplete="email" />
			</label>

			{#if captchaSitekey}
				<div
					class="h-captcha"
					data-sitekey={captchaSitekey}
					data-callback="hcaptchaCallback"
					data-expired-callback="hcaptchaExpired"
				></div>
			{/if}

			<button type="submit" disabled={submitting || slugStatus === 'bad'}>
				{submitting ? m('auth.signup.submitting') : m('auth.signup.submit')}
			</button>

			<!--
				Contract formation happens here — this button creates a tenant and
				binds the signer's organisation — so the terms it forms under have
				to be presented at the point of assent rather than only from a
				footer elsewhere. It sits below the button so the button stays the
				page's primary target, and above the existing footer note so the
				last thing read before submitting is what submitting agrees to.

				The sentence is translated; the three link LABELS are the document
				titles from `lib/legal/pages.ts`, which are English on purpose —
				the documents themselves are (`frontend/CLAUDE.md` § i18n,
				`docs/decisions.md` §174), and naming one in French would promise a
				French text that does not exist. Taking the titles from the
				registry rather than typing them keeps the link text and the page's
				own `<h1>` the same string.
			-->
			<p class="legal-consent">
				<LinkedMessage
					text={m('auth.signup.legalConsent')}
					links={{
						terms: { href: '/legal/terms', label: legalTitle('/legal/terms') },
						privacy: { href: '/legal/privacy', label: legalTitle('/legal/privacy') },
						dpa: { href: '/legal/dpa', label: legalTitle('/legal/dpa') }
					}}
				/>
			</p>

			<p class="footer">
				{m('auth.signup.footerPre')}<code>{tenantExampleHost}</code>{m('auth.signup.footerPost')}
			</p>
		</form>
	{/if}
</div>

<style>
	.page {
		min-height: 100vh;
		display: grid;
		place-items: center;
		background: var(--bg);
		padding: 40px 20px;
	}
	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 40px 36px;
		width: min(480px, 92vw);
		display: flex;
		flex-direction: column;
		gap: 16px;
	}
	h1 {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
		color: var(--text);
	}
	.sub {
		margin: -8px 0 8px;
		font-size: 0.88rem;
		color: var(--text-muted);
	}
	.error {
		background: rgba(224, 64, 64, 0.1);
		border: 1px solid rgba(224, 64, 64, 0.3);
		color: var(--danger);
		padding: 10px 14px;
		border-radius: 4px;
		font-size: 0.85rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	label span {
		font-size: 0.78rem;
		font-weight: 500;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}
	input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}
	input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}
	.slug-row {
		display: flex;
		align-items: center;
		border: 1px solid var(--border);
		border-radius: 4px;
		background: var(--bg);
	}
	.slug-input {
		border: none;
		background: transparent;
		flex: 1;
	}
	.slug-input:focus {
		box-shadow: none;
	}
	.slug-suffix {
		padding: 0 12px;
		color: var(--text-muted);
		font-size: 0.85rem;
	}
	.hint {
		font-size: 0.75rem;
		color: var(--text-muted);
	}
	.hint.ok {
		color: #2e9960;
	}
	.hint.bad {
		color: var(--danger);
	}
	button {
		margin-top: 8px;
		padding: 10px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.9rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}
	button:hover:not(:disabled) {
		opacity: 0.9;
	}
	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}
	.legal-consent {
		margin: 16px 0 0;
		font-size: 0.8rem;
		line-height: 1.5;
		color: var(--text-muted);
		text-align: center;
	}
	.legal-consent a {
		color: var(--accent-on-tint);
	}

	.footer {
		margin: 16px 0 0;
		font-size: 0.8rem;
		color: var(--text-muted);
		text-align: center;
	}
	.footer code {
		background: var(--bg);
		padding: 2px 6px;
		border-radius: 3px;
	}
	.success h1 {
		color: var(--accent);
	}
</style>
