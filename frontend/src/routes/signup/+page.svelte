<script lang="ts">
	import AuthShell from '$lib/components/auth/AuthShell.svelte';
	import BrandMark from '$lib/components/ui/BrandMark.svelte';
	import LinkedMessage from '$lib/components/ui/LinkedMessage.svelte';
	import IconMail from '~icons/material-symbols/mark-email-unread-outline';
	import IconCheck from '~icons/material-symbols/check-circle';
	import IconClose from '~icons/material-symbols/cancel';
	import { tick } from 'svelte';
	import { api } from '$lib/api';
	import { onMount } from 'svelte';
	import { m } from '$lib/i18n/store.svelte';
	import { legalTitle } from '$lib/legal/pages';
	import type { PublicConfig } from '$lib/types/publicConfig';

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

	// `FEOH_SIGNUP_ENABLED` off: every `/api/signup/*` route answers 404, so the
	// form could only ever be refused. Set from `/api/public-config` and only on
	// an explicit `false` — until the config arrives, or if it cannot be fetched,
	// the page stays the form it has always been and the server remains the
	// authority (a closed deployment still refuses the submit).
	let signupClosed = $state(false);

	let slugCheckTimer: ReturnType<typeof setTimeout> | null = null;

	let emailInput = $state<HTMLInputElement | null>(null);
	let captchaEl = $state<HTMLDivElement | null>(null);
	let successHeading = $state<HTMLHeadingElement | null>(null);

	onMount(async () => {
		try {
			const cfg = await api.get<PublicConfig>('/api/public-config');
			if (cfg.signup_enabled === false) {
				// No widget to load and no slug to check: there is no form.
				signupClosed = true;
				return;
			}
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

	/**
	 * Back from "Check your email" to the form, with everything still filled in.
	 *
	 * This used to be `<a href="/signup">` — a link to the page already showing.
	 * A client-routed SPA reuses the mounted component for a same-route
	 * navigation, so `successMessage` survived and the click did nothing: a user
	 * whose verification email never arrived had no way back to the form short
	 * of a hard reload. Resetting the state is the navigation that link was
	 * trying to be. The values are kept and focus lands on the email field,
	 * because a mistyped address is the likeliest reason nothing arrived.
	 */
	async function backToForm() {
		successMessage = null;
		error = '';
		// The token the first submission carried has been spent server-side.
		// Kept, it would satisfy the client-side "captcha required" check and
		// ride along on the retry, which the backend then rejects with no widget
		// in view to solve again.
		captchaToken = null;
		await tick();
		renderCaptcha();
		emailInput?.focus();
		emailInput?.select();
	}

	/**
	 * Render the hCaptcha widget into the form's container, when there is one.
	 *
	 * hCaptcha's implicit mode renders the `.h-captcha` elements present when its
	 * script LOADS. That covers first paint — the container exists before the
	 * async script arrives — but not the form coming back from the confirmation
	 * screen: that is a fresh `.h-captcha` element mounted long after the
	 * script ran, and nothing would ever draw a widget into it. So a remount
	 * renders explicitly. If the script has not loaded yet, implicit mode will
	 * still find the element when it does.
	 */
	function renderCaptcha() {
		const hcaptcha = (window as any).hcaptcha;
		if (!captchaSitekey || !captchaEl || typeof hcaptcha?.render !== 'function') return;
		hcaptcha.render(captchaEl, {
			sitekey: captchaSitekey,
			callback: (token: string) => (captchaToken = token),
			'expired-callback': () => (captchaToken = null)
		});
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
			// The submit button had focus, and it has just been removed from the
			// DOM along with the form — which drops focus to <body> and leaves a
			// screen reader with nothing to say. Land it on the confirmation's
			// heading instead: that is announced, and it puts keyboard users at
			// the top of the new content (WCAG 2.4.3, 4.1.3).
			await tick();
			successHeading?.focus();
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

<!-- What pressing the button starts. Worth saying before the press: the flow
     crosses an inbox twice, and a user who does not expect the second email
     (the temporary password) is a support ticket. Not shown on a closed
     deployment, where there is no button to press. -->
{#snippet nextSteps()}
	<h3 class="next-heading">{m('auth.signup.nextHeading')}</h3>
	<ol class="next-steps">
		<li>
			<span class="step-dot" aria-hidden="true">1</span>
			<span class="step-text">
				<strong>{m('auth.signup.step1Title')}</strong>
				<span>{m('auth.signup.step1Body')}</span>
			</span>
		</li>
		<li>
			<span class="step-dot" aria-hidden="true">2</span>
			<span class="step-text">
				<strong>{m('auth.signup.step2Title')}</strong>
				<span>{m('auth.signup.step2Body')}</span>
			</span>
		</li>
		<li>
			<span class="step-dot" aria-hidden="true">3</span>
			<span class="step-text">
				<strong>{m('auth.signup.step3Title')}</strong>
				<span>{m('auth.signup.step3Body')}</span>
			</span>
		</li>
	</ol>
{/snippet}

<AuthShell panelOnMobile={!signupClosed} panel={signupClosed ? undefined : nextSteps}>
	{#if signupClosed}
		<!-- The Landing page's calls to action still lead here, so this is where a
		     visitor learns signup is by invitation — an honest dead end with the
		     way on for an existing customer, rather than a form every submit of
		     which would 404 (docs/decisions.md §194). -->
		<div class="auth-stack">
			<div class="head">
				<BrandMark size={40} />
				<h1>{m('auth.signup.closedHeading')}</h1>
			</div>
			<p class="sub">{m('auth.signup.closedBody')}</p>
		</div>
	{:else if successMessage}
		<!-- Focus is moved to this heading on arrival (see onSubmit): the form it
		     replaces took the focused button with it. `tabindex="-1"` makes it a
		     programmatic focus target without adding a tab stop. -->
		<div class="auth-stack success">
			<div class="success-icon" aria-hidden="true"><IconMail /></div>
			<h1 tabindex="-1" bind:this={successHeading}>{m('auth.signup.successHeading')}</h1>
			<p class="lead">{successMessage}</p>
			<p class="sub next">{m('auth.signup.successNext')}</p>
			<p class="sub">
				{m('auth.signup.successSpamPre')}<button type="button" class="link-btn" onclick={backToForm}
					>{m('auth.signup.successSpamLink')}</button
				>.
			</p>
		</div>
	{:else}
		<form class="signup-form" onsubmit={onSubmit}>
			<div class="head">
				<BrandMark size={40} />
				<h1>{m('auth.signup.heading')}</h1>
				<p class="sub">{m('auth.signup.subtitle')}</p>
			</div>

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
				<!-- The input and its host suffix read as ONE field: the row takes
				     the field chrome (border, focus ring) and the input inside it
				     drops its own, so the ring wraps the whole address the user is
				     composing rather than half of it. -->
				<div class="slug-row" class:ok={slugStatus === 'ok'} class:bad={slugStatus === 'bad'}>
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
					<span class="slug-state" aria-hidden="true">
						{#if slugStatus === 'checking'}
							<span class="spinner"></span>
						{:else if slugStatus === 'ok'}
							<IconCheck />
						{:else if slugStatus === 'bad'}
							<IconClose />
						{/if}
					</span>
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
				<input
					type="email"
					bind:this={emailInput}
					bind:value={adminEmail}
					required
					maxlength="320"
					autocomplete="email"
				/>
			</label>

			{#if captchaSitekey}
				<div
					class="h-captcha"
					bind:this={captchaEl}
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
</AuthShell>

<style>
	/* Field chrome, the entrance stagger, the submit button and the error
	   banner all come from AuthShell. What is here is only what this page has
	   that the others do not: the composite slug field, its live availability
	   state, the consent line, and the confirmation screen. */
	.signup-form,
	.auth-stack {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}
	.head {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 10px;
		margin-bottom: 6px;
	}
	.sub {
		margin: 0;
		font-size: 0.93rem;
		line-height: 1.55;
		color: var(--text-muted);
	}

	/* ------------------------------ slug field ----------------------------- */
	.slug-row {
		display: flex;
		align-items: center;
		min-height: 46px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		transition: border-color 0.15s, box-shadow 0.15s;
	}
	.slug-row:hover {
		border-color: #3a3e4f;
	}
	.slug-row:focus-within {
		border-color: var(--accent);
		box-shadow: 0 0 0 4px rgba(99, 140, 255, 0.22);
	}
	.slug-row.ok {
		border-color: rgba(38, 185, 119, 0.55);
	}
	.slug-row.bad {
		border-color: var(--danger);
	}
	/* The row carries the chrome, so the input inside it carries none — the
	   focus ring above is on the row. Wins over AuthShell's field defaults on
	   plain specificity, because the shell writes those under `:where()`. */
	.slug-row .slug-input {
		flex: 1;
		min-width: 0;
		min-height: 44px;
		border: none;
		background: transparent;
		box-shadow: none;
	}
	.slug-suffix {
		padding-right: 6px;
		color: var(--text-muted);
		font-size: 0.88rem;
		white-space: nowrap;
	}
	.slug-state {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 34px;
		font-size: 19px;
		flex: none;
	}
	.slug-row.ok .slug-state {
		color: var(--success);
	}
	.slug-row.bad .slug-state {
		color: var(--danger);
	}
	.spinner {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		border: 2px solid var(--border);
		border-top-color: var(--accent);
		animation: spin 0.7s linear infinite;
	}
	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	.hint {
		display: block;
		margin-top: 2px;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.hint.ok {
		color: var(--success);
	}
	.hint.bad {
		color: var(--danger);
	}

	/* ------------------------------ fine print ----------------------------- */
	.legal-consent {
		margin: 2px 0 0;
		font-size: 0.8rem;
		line-height: 1.55;
		color: var(--text-muted);
		text-align: center;
	}
	.legal-consent :global(a) {
		color: var(--accent-on-tint);
	}
	.footer {
		margin: 0;
		padding-top: 16px;
		border-top: 1px solid var(--border);
		font-size: 0.8rem;
		color: var(--text-muted);
		text-align: center;
	}
	.footer code {
		background: var(--surface);
		border: 1px solid var(--border);
		padding: 2px 6px;
		border-radius: 5px;
		font-family: var(--font-mono);
		font-size: 0.78rem;
		color: var(--text);
	}

	/* ---------------------------- confirmation ----------------------------- */
	.success-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 60px;
		height: 60px;
		border-radius: 18px;
		border: 1px solid rgba(99, 140, 255, 0.3);
		background: linear-gradient(140deg, rgba(99, 140, 255, 0.24), rgba(163, 125, 255, 0.14));
		color: var(--accent-on-tint);
		font-size: 30px;
		animation: pop 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) backwards;
	}
	@keyframes pop {
		from { transform: scale(0.6); opacity: 0; }
		to { transform: scale(1); opacity: 1; }
	}
	.success h1:focus {
		outline: none;
	}
	.lead {
		margin: 0;
		font-size: 1rem;
		line-height: 1.55;
		color: var(--text);
	}
	.next {
		padding: 14px 16px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--surface);
	}
	.link-btn {
		display: inline;
		padding: 0;
		border: none;
		background: none;
		color: var(--accent-on-tint);
		font: inherit;
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
	}
	.link-btn:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: 2px;
	}

	/* ------------------------------- panel --------------------------------- */
	.next-heading {
		margin: 0 0 16px;
		font-size: 0.74rem;
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.next-steps {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 18px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	/* The rail joining the three dots: the steps are one sequence, not three
	   separate facts, and the line is what says so. */
	.next-steps::before {
		content: '';
		position: absolute;
		left: 13px;
		top: 14px;
		bottom: 14px;
		width: 1px;
		background: linear-gradient(180deg, rgba(99, 140, 255, 0.55), rgba(231, 185, 94, 0.35));
	}
	.next-steps li {
		position: relative;
		display: flex;
		gap: 14px;
		align-items: flex-start;
	}
	.step-dot {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex: none;
		width: 27px;
		height: 27px;
		border-radius: 50%;
		border: 1px solid rgba(99, 140, 255, 0.45);
		background: #151a2b;
		color: var(--text);
		font-size: 0.78rem;
		font-weight: 700;
	}
	.step-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding-top: 3px;
	}
	.step-text strong {
		font-size: 0.92rem;
		font-weight: 600;
		color: var(--text);
	}
	.step-text span {
		font-size: 0.85rem;
		line-height: 1.5;
		color: var(--text-muted);
	}
</style>
