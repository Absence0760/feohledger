<script lang="ts">
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import AuthBrandMark from '#lib/components/auth/AuthBrandMark.svelte';
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import IconMail from '~icons/material-symbols/mark-email-unread-outline';
	import IconCheck from '~icons/material-symbols/check-circle';
	import IconClose from '~icons/material-symbols/cancel';
	import { tick, untrack } from 'svelte';
	import { api, ApiError, formatApiDetail } from '#lib/api.ts';
	import { onMount } from 'svelte';
	import { currentLocale, m } from '#lib/i18n/store.svelte.ts';
	import { codedRefusalText, refusalField } from '#lib/api/codedRefusals.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import { theme } from '#lib/stores/theme.svelte.ts';
	import type { ResolvedTheme } from '#lib/theme.ts';
	import { legalTitle } from '#lib/legal/pages.ts';
	import type { PublicConfig } from '#lib/types/publicConfig.ts';
	import {
		WORKSPACE_ADDRESS_MAX,
		WORKSPACE_ADDRESS_MIN,
		cleanWorkspaceAddressTyping,
		tenantHostSuffix,
		workspaceAddressProblem,
		type WorkspaceAddressProblem
	} from '#lib/utils/workspaceAddress.ts';

	interface StartResponse {
		status: string;
		message: string;
	}

	interface SlugCheckResponse {
		slug: string;
		available: boolean;
		// Why not: the English sentence, and the rule's code to state it in
		// the reader's language (`backend/app/utils/slug.py::SLUG_*`).
		reason?: string | null;
		code?: string | null;
	}

	let companyName = $state('');
	let slug = $state('');
	let adminName = $state('');
	let adminEmail = $state('');
	let captchaToken = $state<string | null>(null);
	let captchaSitekey = $state<string>('');
	// hCaptcha's API has loaded (its explicit-mode onload fired).
	let captchaApiReady = $state(false);
	// hCaptcha's script could not be loaded — almost always a content / ad
	// blocker or a browser privacy shield. Shown where the widget would be.
	let captchaLoadFailed = $state(false);
	// The widget currently drawn: which container, and in which palette. Plain,
	// not $state — only the captcha effect reads it, and it must not re-run
	// that effect by being written there.
	let captchaWidget: { id: string; el: HTMLDivElement; theme: ResolvedTheme } | null = null;
	const HCAPTCHA_ONLOAD = 'feohHCaptchaOnLoad';
	// Host/port suffix shown after the slug input, derived from the backend
	// tenant_url_template (e.g. "http://{slug}.localhost:7777" → ".localhost:7777").
	let tenantUrlSuffix = $state<string>('.localhost:7777');
	// An example address, not a placeholder word: "your-slug" put the internal
	// name in front of every visitor, in every language (issue #635 finding 18).
	let tenantExampleHost = $state<string>('acme.localhost:7777');

	let slugStatus = $state<'idle' | 'checking' | 'ok' | 'bad'>('idle');
	let slugError = $state<string | null>(null);

	let submitting = $state(false);
	let error = $state('');

	/** The `/api/signup/start` body fields, which a refusal can be about. */
	type Field = 'company_name' | 'slug' | 'admin_name' | 'admin_email';
	const FIELD_INPUT_IDS: Record<Field, string> = {
		company_name: 'signup-company',
		slug: 'signup-slug',
		admin_name: 'signup-name',
		admin_email: 'signup-email'
	};
	// A server refusal about one field, shown under that field. The slug's
	// lives in `slugError` instead, with its live availability state.
	let fieldErrors = $state<Partial<Record<Exclude<Field, 'slug'>, string>>>({});
	// The address the verification email went to, once `/start` accepted the
	// form. The confirmation is stated here, in the visitor's language, from
	// that address — not from the server's English `message`.
	let sentTo = $state<string | null>(null);

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
			const suffix = tenantHostSuffix(cfg.tenant_url_template);
			if (suffix) {
				tenantUrlSuffix = suffix;
				tenantExampleHost = `acme${suffix}`;
			}
		} catch {
			// Non-fatal — backend may be unreachable; form will surface errors on submit.
		}
	});

	/**
	 * Load hCaptcha in EXPLICIT mode: it draws nothing by itself, and calls
	 * `HCAPTCHA_ONLOAD` when its API is ready. Every widget is then drawn by the
	 * effect below, which is the only way to give it a theme after the fact —
	 * hCaptcha has no call to change a live widget's theme, so following the
	 * page means removing the widget and drawing it again.
	 *
	 * **When the script cannot load** — a content blocker, a browser privacy
	 * shield, a filtering proxy — the page used to say only "Please complete
	 * the captcha" with nothing on screen to complete. The `<script>` element's
	 * own events are the signal, not a timer: `error` when the request fails,
	 * and `load` with no `hcaptcha` global when a blocker answered it with an
	 * empty stand-in. Either way the failed element is removed, so the next
	 * visit to the page asks again (the visitor may have allowed the page in
	 * their blocker meanwhile) instead of finding an element that looks like it
	 * is still loading. The listeners go on whichever element is in the head —
	 * this visit's, or one an earlier visit added that is still in flight.
	 */
	function loadHCaptcha() {
		const w = window as any;
		w[HCAPTCHA_ONLOAD] = () => (captchaApiReady = true);
		// Already loaded by an earlier visit to this page in the same session.
		if (typeof w.hcaptcha?.render === 'function') {
			captchaApiReady = true;
			return;
		}
		let s = document.querySelector<HTMLScriptElement>('script[data-hcaptcha]');
		if (!s) {
			s = document.createElement('script');
			s.src = `https://hcaptcha.com/1/api.js?render=explicit&onload=${HCAPTCHA_ONLOAD}`;
			s.async = true;
			s.defer = true;
			s.setAttribute('data-hcaptcha', 'true');
			document.head.appendChild(s);
		}
		const script = s;
		const failed = () => {
			script.remove();
			captchaLoadFailed = true;
		};
		script.addEventListener('error', failed, { once: true });
		// hCaptcha's api.js defines its `hcaptcha` global synchronously as it
		// runs, so by `load` it exists; readiness itself is still the `onload`
		// callback above. Only a script that ran and defined NOTHING — a
		// blocker's empty stand-in — counts as failed here.
		script.addEventListener(
			'load',
			() => {
				if (!w.hcaptcha) failed();
			},
			{ once: true }
		);
	}

	/**
	 * What this form SAYS about each way an address can break the rule, which
	 * `#lib/utils/workspaceAddress.ts` owns (the client half of
	 * `backend/app/utils/slug.py::validate_slug_format`, shared with
	 * `/find-workspace`) — checked as the visitor types and in their language,
	 * instead of after a round trip in the server's English. The problems with
	 * no entry are the field's own business: an empty or short address is the
	 * input's `required`/`minlength`, a long one its `maxlength`, and a stray
	 * character is dropped as it is typed. The reserved-word list is left to the
	 * availability check, which reports it: it is the server's to extend.
	 */
	const SLUG_PROBLEM_KEYS: Partial<Record<WorkspaceAddressProblem, MessageKey>> = {
		startLetter: 'auth.signup.slugStartLetter',
		endHyphen: 'auth.signup.slugEndHyphen',
		doubleHyphen: 'auth.signup.slugDoubleHyphen'
	};

	function slugFormatProblem(value: string): MessageKey | null {
		const problem = workspaceAddressProblem(value);
		return problem ? (SLUG_PROBLEM_KEYS[problem] ?? null) : null;
	}

	function onSlugInput() {
		slug = cleanWorkspaceAddressTyping(slug);
		slugError = null;
		slugStatus = 'idle';
		if (slugCheckTimer) clearTimeout(slugCheckTimer);
		// A wrong first character is never a step on the way to a valid
		// address, so it is said at once. A trailing hyphen usually is ("my-"
		// on the way to "my-co"), so that waits for the pause like the
		// availability check does.
		if (workspaceAddressProblem(slug) === 'startLetter') {
			slugStatus = 'bad';
			slugError = m('auth.signup.slugStartLetter');
			return;
		}
		if (slug.length < WORKSPACE_ADDRESS_MIN) return;
		slugStatus = 'checking';
		slugCheckTimer = setTimeout(checkSlug, 400);
	}

	async function checkSlug() {
		const problem = slugFormatProblem(slug);
		if (problem) {
			slugStatus = 'bad';
			slugError = m(problem);
			return;
		}
		try {
			const res = await api.get<SlugCheckResponse>(
				`/api/signup/slug-check?slug=${encodeURIComponent(slug)}`
			);
			if (res.available) {
				slugStatus = 'ok';
				slugError = null;
			} else {
				slugStatus = 'bad';
				slugError =
					(res.code ? codedRefusalText(res.code, { slug: res.slug }, m) : null) ??
					res.reason ??
					m('auth.signup.slugUnavailable');
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
	 * navigation, so the confirmation survived and the click did nothing: a user
	 * whose verification email never arrived had no way back to the form short
	 * of a hard reload. Resetting the state is the navigation that link was
	 * trying to be. The values are kept and focus lands on the email field,
	 * because a mistyped address is the likeliest reason nothing arrived.
	 */
	async function backToForm() {
		sentTo = null;
		error = '';
		// The token the first submission carried has been spent server-side.
		// Kept, it would satisfy the client-side "captcha required" check and
		// ride along on the retry, which the backend then rejects with no widget
		// in view to solve again.
		// The form's fresh `.h-captcha` container gets its widget from the
		// effect below, which sees the new element.
		captchaToken = null;
		await tick();
		emailInput?.focus();
		emailInput?.select();
	}

	/**
	 * Keep exactly one hCaptcha widget in the form, drawn in the palette the
	 * page is showing (docs/decisions.md §289, §290).
	 *
	 * The widget is an iframe and cannot see our palette, so it is told which
	 * one is showing — a light widget on the dark form (or the reverse) is a
	 * glaring box. It re-runs when the container is mounted (first paint, and
	 * the form coming back from the confirmation screen), when the API finishes
	 * loading, and when `theme.resolved` changes — a choice on the auth shell's
	 * picker, or the OS switching scheme while System is chosen.
	 *
	 * A SOLVED widget is left alone on a theme change: redrawing it would
	 * throw away a token the visitor already earned, for a cosmetic reason. It
	 * is redrawn in the current palette once that token is gone — expired
	 * (hCaptcha resets the widget then anyway) or spent on a submission.
	 */
	$effect(() => {
		const el = captchaEl;
		const wanted = theme.resolved;
		const solved = captchaToken !== null;
		if (!captchaSitekey || !captchaApiReady || !el) return;
		untrack(() => syncCaptcha(el, wanted, solved));
	});

	function syncCaptcha(el: HTMLDivElement, wanted: ResolvedTheme, solved: boolean) {
		const hcaptcha = (window as any).hcaptcha;
		if (typeof hcaptcha?.render !== 'function') return;
		if (captchaWidget) {
			const sameContainer = captchaWidget.el === el;
			if (sameContainer && (captchaWidget.theme === wanted || solved)) return;
			try {
				hcaptcha.remove(captchaWidget.id);
			} catch {
				// Its container left the DOM with the form; nothing to remove.
			}
			captchaWidget = null;
		}
		const id = hcaptcha.render(el, {
			sitekey: captchaSitekey,
			theme: wanted,
			callback: (token: string) => (captchaToken = token),
			'expired-callback': () => (captchaToken = null)
		});
		captchaWidget = { id, el, theme: wanted };
	}

	/**
	 * Throw away a captcha token the server may already have spent.
	 *
	 * The backend verifies the captcha BEFORE it checks the slug is free or
	 * sends the email, so a 409 or a 502 arrives with the token used up while
	 * the widget still shows a tick. Kept, it satisfied the client-side check
	 * and rode along on the retry, which the server refused with "Captcha
	 * verification failed" — a misleading reason, with nothing on screen to
	 * solve again. Cleared here on every answer from the server (a refusal
	 * that came before verification costs the visitor one more tick; reusing
	 * a spent token costs them a confusing dead end), and the widget is reset
	 * so it looks as unsolved as it now is. The captcha effect redraws it
	 * instead if the theme changed while it was solved.
	 */
	function discardCaptchaToken() {
		if (captchaToken === null) return;
		captchaToken = null;
		const hcaptcha = (window as any).hcaptcha;
		if (captchaWidget && typeof hcaptcha?.reset === 'function') {
			try {
				hcaptcha.reset(captchaWidget.id);
			} catch {
				// Its container already left the DOM; the effect draws a new one.
			}
		}
	}

	/** A Pydantic validation item's message, in the reader's language where we know its type. */
	function validationMessage(item: { type?: unknown; msg?: unknown; ctx?: Record<string, unknown> }): string {
		const min = item.ctx?.min_length;
		const max = item.ctx?.max_length;
		if (item.type === 'string_too_short' && typeof min === 'number') {
			return m('auth.signup.fieldTooShort', { min });
		}
		if (item.type === 'string_too_long' && typeof max === 'number') {
			return m('auth.signup.fieldTooLong', { max });
		}
		return typeof item.msg === 'string' ? item.msg : m('auth.signup.failed');
	}

	/**
	 * Put a server refusal next to the field it is about, and move focus there.
	 * Returns the refusal's left-over text for the top banner — `null` when
	 * every part of it found a field.
	 *
	 * Three shapes are about one field: a coded refusal whose `params.field`
	 * names it (every field refusal `/start` makes — the workspace address, the
	 * email, the name; its text is already localized from its code), a Pydantic
	 * 422, whose items name their field in `loc`, and — from a backend that
	 * predates the codes — a bare 409, which `/start` only answers for a taken
	 * address. Anything else — a rate limit, the captcha, the email failing to
	 * send — stays in the banner.
	 */
	async function showRefusal(err: unknown): Promise<string | null> {
		if (!(err instanceof ApiError)) {
			return err instanceof Error ? err.message : m('auth.signup.failed');
		}
		let first: Field | null = null;
		let leftOver: string | null = err.message;
		const coded = refusalField(err.params);
		if (coded !== null && Object.hasOwn(FIELD_INPUT_IDS, coded)) {
			first = coded as Field;
			if (first === 'slug') {
				slugStatus = 'bad';
				slugError = err.message;
			} else {
				fieldErrors[first] = err.message;
			}
			leftOver = null;
		} else if (err.status === 409) {
			slugStatus = 'bad';
			slugError = err.message;
			first = 'slug';
			leftOver = null;
		} else if (err.status === 422 && Array.isArray(err.detail)) {
			const unplaced: unknown[] = [];
			const found: Partial<Record<Field, string>> = {};
			for (const raw of err.detail) {
				const item = (raw ?? {}) as { loc?: unknown; type?: unknown; msg?: unknown; ctx?: Record<string, unknown> };
				const loc = Array.isArray(item.loc) ? item.loc : [];
				const field = loc[loc.length - 1];
				if (typeof field === 'string' && Object.hasOwn(FIELD_INPUT_IDS, field)) {
					found[field as Field] ??= validationMessage(item);
				} else {
					unplaced.push({ ...item, msg: validationMessage(item) });
				}
			}
			for (const field of Object.keys(FIELD_INPUT_IDS) as Field[]) {
				const msg = found[field];
				if (!msg) continue;
				first ??= field;
				if (field === 'slug') {
					slugStatus = 'bad';
					slugError = msg;
				} else {
					fieldErrors[field] = msg;
				}
			}
			// Left-overs keep their `field: message` form, since they are shown
			// away from any field.
			leftOver = first
				? unplaced.length
					? formatApiDetail(unplaced, m('auth.signup.failed'))
					: null
				: err.message;
		}
		if (first) {
			// The field's error is its description (aria-describedby), so
			// landing focus on it is what announces it (WCAG 3.3.1, 4.1.3).
			await tick();
			document.getElementById(FIELD_INPUT_IDS[first])?.focus();
		}
		return leftOver;
	}

	function clearFieldError(field: Exclude<Field, 'slug'>) {
		if (fieldErrors[field]) fieldErrors[field] = undefined;
	}

	async function onSubmit(e: Event) {
		e.preventDefault();
		error = '';
		fieldErrors = {};
		// The form can be submitted inside the slug check's pause, before the
		// format was looked at.
		const slugProblem = slugFormatProblem(slug);
		if (slugProblem) {
			slugStatus = 'bad';
			slugError = m(slugProblem);
			document.getElementById(FIELD_INPUT_IDS.slug)?.focus();
			return;
		}
		if (captchaSitekey && !captchaToken) {
			error = captchaLoadFailed
				? m('auth.signup.captchaBlockedSubmit')
				: m('auth.signup.captchaRequired');
			return;
		}
		submitting = true;
		try {
			await api.post<StartResponse>('/api/signup/start', {
				company_name: companyName,
				slug,
				admin_name: adminName,
				admin_email: adminEmail,
				captcha_token: captchaToken,
				// The verification and welcome emails are written in it.
				locale: currentLocale()
			});
			sentTo = adminEmail;
			// The submit button had focus, and it has just been removed from the
			// DOM along with the form — which drops focus to <body> and leaves a
			// screen reader with nothing to say. Land it on the confirmation's
			// heading instead: that is announced, and it puts keyboard users at
			// the top of the new content (WCAG 2.4.3, 4.1.3).
			await tick();
			successHeading?.focus();
		} catch (err) {
			discardCaptchaToken();
			error = (await showRefusal(err)) ?? '';
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
     (the set-password link, decisions §283) is a support ticket. Not shown on a closed
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
		     which would 404 (docs/decisions.md §193). -->
		<div class="auth-stack">
			<div class="head">
				<AuthBrandMark />
				<h1>{m('auth.signup.closedHeading')}</h1>
			</div>
			<p class="sub">{m('auth.signup.closedBody')}</p>
		</div>
	{:else if sentTo}
		<!-- Focus is moved to this heading on arrival (see onSubmit): the form it
		     replaces took the focused button with it. `tabindex="-1"` makes it a
		     programmatic focus target without adding a tab stop. -->
		<div class="auth-stack success">
			<div class="success-icon" aria-hidden="true"><IconMail /></div>
			<h1 tabindex="-1" bind:this={successHeading}>{m('auth.signup.successHeading')}</h1>
			<p class="lead">{m('auth.signup.successLead', { email: sentTo })}</p>
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
				<AuthBrandMark />
				<h1>{m('auth.signup.heading')}</h1>
				<p class="sub">{m('auth.signup.subtitle')}</p>
			</div>

			<div role="alert" aria-live="assertive">
				{#if error}
					<div class="error">{error}</div>
				{/if}
			</div>

			<!-- Each field is a <label for> beside its input, not an input
			     wrapped in its label: a message under the field is then its
			     DESCRIPTION (aria-describedby) rather than part of its name, so a
			     screen reader says "Company name, invalid, Must be at least 2
			     characters" instead of folding the error into the label. The
			     `minlength`s mirror backend/app/schemas/signup.py. -->
			<div class="field">
				<label for="signup-company"><span>{m('auth.signup.companyName')}</span></label>
				<input
					id="signup-company"
					bind:value={companyName}
					oninput={() => clearFieldError('company_name')}
					required
					minlength="2"
					maxlength="255"
					autocomplete="organization"
					aria-invalid={fieldErrors.company_name ? 'true' : undefined}
					aria-describedby={fieldErrors.company_name ? 'signup-company-error' : undefined}
				/>
				{#if fieldErrors.company_name}
					<small id="signup-company-error" class="hint bad">{fieldErrors.company_name}</small>
				{/if}
			</div>

			<div class="field">
				<label for="signup-slug"><span>{m('auth.signup.workspaceUrl')}</span></label>
				<!-- The input and its host suffix read as ONE field: the row takes
				     the field chrome (border, focus ring) and the input inside it
				     drops its own, so the ring wraps the whole address the user is
				     composing rather than half of it. -->
				<div class="slug-row" class:ok={slugStatus === 'ok'} class:bad={slugStatus === 'bad'}>
					<input
						id="signup-slug"
						class="slug-input"
						bind:value={slug}
						oninput={onSlugInput}
						required
						minlength={WORKSPACE_ADDRESS_MIN}
						maxlength={WORKSPACE_ADDRESS_MAX}
						placeholder="acme"
						autocapitalize="off"
						autocorrect="off"
						spellcheck="false"
						aria-describedby="slug-suffix slug-hint"
						aria-invalid={slugStatus === 'bad'}
					/>
					<!-- Part of the field's description: the label says "Workspace URL",
					     and this is the rest of the address the slug becomes. -->
					<span class="slug-suffix" id="slug-suffix">{tenantUrlSuffix}</span>
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
			</div>

			<div class="field">
				<label for="signup-name"><span>{m('auth.signup.yourName')}</span></label>
				<input
					id="signup-name"
					bind:value={adminName}
					oninput={() => clearFieldError('admin_name')}
					required
					minlength="2"
					maxlength="255"
					autocomplete="name"
					aria-invalid={fieldErrors.admin_name ? 'true' : undefined}
					aria-describedby={fieldErrors.admin_name ? 'signup-name-error' : undefined}
				/>
				{#if fieldErrors.admin_name}
					<small id="signup-name-error" class="hint bad">{fieldErrors.admin_name}</small>
				{/if}
			</div>

			<div class="field">
				<label for="signup-email"><span>{m('auth.signup.email')}</span></label>
				<input
					id="signup-email"
					type="email"
					bind:this={emailInput}
					bind:value={adminEmail}
					oninput={() => clearFieldError('admin_email')}
					required
					maxlength="320"
					autocomplete="email"
					aria-invalid={fieldErrors.admin_email ? 'true' : undefined}
					aria-describedby={fieldErrors.admin_email ? 'signup-email-error' : undefined}
				/>
				{#if fieldErrors.admin_email}
					<small id="signup-email-error" class="hint bad">{fieldErrors.admin_email}</small>
				{/if}
			</div>

			{#if captchaSitekey}
				{#if captchaLoadFailed}
					<!-- Where the widget would have been, and announced on arrival:
					     without it the only thing on screen was a "complete the
					     captcha" request with nothing to complete. -->
					<div class="error captcha-blocked" id="captcha-blocked" role="alert">
						<strong>{m('auth.signup.captchaBlockedTitle')}</strong>
						<span>{m('auth.signup.captchaBlockedBody')}</span>
					</div>
				{:else}
					<!-- Drawn into by the captcha effect (explicit render), never by
					     hCaptcha scanning for the class. -->
					<div class="h-captcha" bind:this={captchaEl}></div>
				{/if}
			{/if}

			<!-- While the captcha cannot load, the button is described by the
			     notice, so pressing it says why nothing can be sent. -->
			<button
				type="submit"
				disabled={submitting || slugStatus === 'bad'}
				aria-describedby={captchaSitekey && captchaLoadFailed ? 'captcha-blocked' : undefined}
			>
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

	/* A label beside its input rather than around it (see the markup), laid
	   out the way AuthShell lays out a wrapping label. */
	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
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
		border-color: var(--border-strong);
	}
	.slug-row:focus-within {
		border-color: var(--accent);
		box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 22%, transparent);
	}
	.slug-row.ok {
		border-color: color-mix(in srgb, var(--success) 55%, transparent);
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
		border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
		background: linear-gradient(140deg, color-mix(in srgb, var(--accent) 24%, transparent), rgba(163, 125, 255, 0.14));
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
		background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 55%, transparent), rgba(231, 185, 94, 0.35));
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
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		background: color-mix(in srgb, var(--accent) 8%, var(--surface));
		color: var(--text);
		font-size: 0.78rem;
		font-weight: 700;
	}
	/* `min-width: 0`: a flex item will not shrink below its longest word
	   otherwise, and the panel clips what overflows (AuthShell § .panel). */
	.step-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
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
