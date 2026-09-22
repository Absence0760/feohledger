<script lang="ts">
	import { untrack } from 'svelte';
	import { page } from '$app/state';
	import { auth } from '$lib/stores/auth.svelte';
	import { api } from '$lib/api';
	import { toast } from '$lib/components/ui/Toast.svelte';
	import SettingsRail from '$lib/components/ui/SettingsRail.svelte';
	import { SUPPORTED_LOCALES, LOCALE_LABELS, type Locale } from '$lib/i18n/locale';
	import { currentLocale, setLocale, m } from '$lib/i18n/store.svelte';
	import type { MessageKey } from '$lib/i18n/messages';
	import { notificationStore } from '$lib/stores/notifications.svelte';
	import {
		EVENT_ORDER,
		EVENT_LABEL_KEYS,
		normalizePrefs,
		type ChannelPrefs,
		type NotificationEventType,
	} from '$lib/types/notification';
	import { roleLabelKey } from '$lib/types/admin';
	import { formatList } from '$lib/utils/list';

	interface EnrollResponse {
		secret: string;
		provisioning_uri: string;
		qr_code_data_url: string;
	}

	// ── Section navigation ────────────────────────────────────────────────
	// Seven panels, all genuinely one subject ("my account"), so the rail is
	// FLAT — no group headings, which is what `ui/SettingsRail.svelte` renders
	// when a group carries no label. `/organization` groups its fifteen because
	// they are fifteen unrelated concerns; seven coherent ones do not need it.
	//
	// The reason to give this page a rail at all is not the section count but
	// the three reads below: preferences, passkeys and signed-in devices each
	// fired on arrival, so opening the page to change a language fetched the
	// device list. They now wait for their own panel.
	//
	// A slug is part of the URL contract — it can be bookmarked and linked to.
	//
	// The order below is the RAIL's, not the order the panels sit in the markup:
	// identity, then credentials, then what is currently signed in, then
	// preferences. Language led the old scroll purely because it was the
	// shortest card to put at the top.
	const SECTIONS: { slug: string; labelKey: MessageKey }[] = [
		{ slug: 'account', labelKey: 'profile.account.heading' },
		{ slug: 'password', labelKey: 'profile.password.heading' },
		{ slug: 'mfa', labelKey: 'profile.mfa.heading' },
		{ slug: 'passkeys', labelKey: 'profile.passkeys.heading' },
		{ slug: 'sessions', labelKey: 'profile.sessions.heading' },
		{ slug: 'notifications', labelKey: 'profile.notifications.heading' },
		{ slug: 'language', labelKey: 'profile.language.heading' }
	];

	const SECTION_SLUGS = new Set(SECTIONS.map((s) => s.slug));
	const DEFAULT_SECTION = 'account';

	// Unknown slug falls back rather than rendering an empty page.
	const section = $derived.by(() => {
		const slug = page.url.searchParams.get('section');
		return slug && SECTION_SLUGS.has(slug) ? slug : DEFAULT_SECTION;
	});

	const railGroups = $derived([
		{ items: SECTIONS.map((s) => ({ slug: s.slug, label: m(s.labelKey) })) }
	]);

	// Notification preferences
	let prefsLoaded = $state(false);
	let savingPrefs = $state(false);

	$effect(() => {
		// `prefsLoaded` is the already-fetched guard; the section check is what
		// makes it lazy, so opening the page no longer reads preferences the
		// visitor may never look at.
		if (section === 'notifications' && !prefsLoaded) {
			void loadPrefs();
		}
	});

	async function loadPrefs() {
		try {
			await notificationStore.fetchPrefs();
		} catch {
			/* non-critical — keep the page usable */
		} finally {
			prefsLoaded = true;
		}
	}

	async function togglePref(event: NotificationEventType, channel: 'email' | 'in_app') {
		if (!notificationStore.prefs) return;
		// Read through the same normalisation the grid renders from, so an event
		// the server omitted toggles off its shown (default-on) state rather than
		// throwing on an undefined row.
		const current = normalizePrefs(notificationStore.prefs)[event];
		const next: ChannelPrefs = {
			...current,
			[channel]: !current[channel],
		};
		savingPrefs = true;
		try {
			await notificationStore.updatePrefs({ [event]: next });
		} catch {
			toast(m('profile.notifications.updateFailed'), 'error');
		} finally {
			savingPrefs = false;
		}
	}

	let enrollment = $state<EnrollResponse | null>(null);
	let verifyCode = $state('');
	// The proof typed into the disable form: the password where it is a step-up
	// proof, a current authenticator code where it is not (see `passwordIsProof`).
	let disableProof = $state('');
	let loading = $state(false);

	// --- Which proof a factor change may ask the member to TYPE --------------
	// The server drops an offered password from every step-up when the org has
	// closed password sign-in (`sso_only`, docs/decisions.md §191), so a password
	// field there is a control that can only ever be refused. `/auth/me`'s
	// `password_sign_in_closed` is the server's own predicate — the one login and
	// the step-up both call — rather than the login page's public `/config`
	// echo, which says "open" for a tenant whose IdP config does not resolve
	// even though the password is closed there too (§201). Where it is set, every
	// step-up prompt below asks for a current authenticator code or runs the
	// passkey ceremony instead.
	const passwordIsProof = $derived(!auth.user?.password_sign_in_closed);

	/** The server's own shape for a TOTP code (6-8 digits); a shorter entry is a
	 * 422, so a half-typed code must not count as an offered proof. */
	function isCompleteCode(value: string): boolean {
		return /^[0-9]{6,8}$/.test(value);
	}

	/** Package a typed proof under the field the server reads it from. */
	function typedStepUpProof(kind: 'password' | 'code', value: string): StepUpProof {
		return kind === 'code' ? { code: value } : { password: value };
	}

	// Account editing
	let fullName = $state('');
	let savingProfile = $state(false);
	let currentPassword = $state('');
	let newPassword = $state('');
	let confirmPassword = $state('');
	let savingPassword = $state(false);

	$effect(() => {
		// Seed the local edit field once the user record lands (and again if the
		// account itself changes). `fullName` is read through `untrack`: a tracked
		// read would make this effect depend on the very state it writes, so
		// backspacing the input to empty re-fired it and instantly re-filled the
		// field with the stored name — the user could never clear it to retype.
		// Depending on `auth.user` alone keeps the seed while leaving the field
		// entirely under the user's control once it exists.
		const u = auth.user;
		if (u && !untrack(() => fullName)) {
			fullName = u.full_name;
		}
	});

	async function saveProfile() {
		if (!fullName.trim() || fullName === auth.user?.full_name) return;
		savingProfile = true;
		try {
			await api.patch('/api/auth/me', { full_name: fullName.trim() });
			await auth.fetchUser();
			toast(m('profile.account.updated'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.account.updateFailed'), 'error');
		} finally {
			savingProfile = false;
		}
	}

	async function changePassword() {
		if (!currentPassword || !newPassword) return;
		if (newPassword !== confirmPassword) {
			toast(m('profile.password.mismatch'), 'error');
			return;
		}
		savingPassword = true;
		try {
			await api.patch('/api/auth/me', {
				current_password: currentPassword,
				password: newPassword,
			});
			currentPassword = '';
			newPassword = '';
			confirmPassword = '';
			toast(m('profile.password.updated'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.password.updateFailed'), 'error');
		} finally {
			savingPassword = false;
		}
	}

	async function startEnroll() {
		loading = true;
		try {
			// Adding TOTP to an account that already has a passkey is a factor
			// change, so the server wants a step-up. This card has no password
			// field (and an SSO-only account has no password anyway), so the
			// passkey the account already holds is the proof.
			const proof = hasPasskey ? await auth.passkeyStepUp('totp_enroll') : {};
			enrollment = await api.post<EnrollResponse>('/api/auth/mfa/enroll', proof);
			verifyCode = '';
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.mfa.enrollFailed'), 'error');
		} finally {
			loading = false;
		}
	}

	async function verifyEnroll() {
		if (!enrollment) return;
		loading = true;
		try {
			await api.post('/api/auth/mfa/enroll/verify', { code: verifyCode });
			await auth.fetchUser();
			enrollment = null;
			verifyCode = '';
			toast(m('profile.mfa.enabledToast'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.mfa.verifyFailed'), 'error');
		} finally {
			loading = false;
		}
	}

	/** Turning MFA off with a passkey rather than a typed proof — for an account
	 * with no password to type, or one whose org no longer accepts it. */
	async function disableWithPasskey() {
		await disable(await auth.passkeyStepUp('totp_disable'));
	}

	/** The kind of proof the disable form asks for. The factor being turned off
	 * is a live TOTP secret, so a current code from it is always on hand. */
	const disableProofKind = $derived<'password' | 'code'>(passwordIsProof ? 'password' : 'code');
	const disableProofReady = $derived(
		disableProofKind === 'code' ? isCompleteCode(disableProof) : Boolean(disableProof),
	);

	async function disable(proof: StepUpProof = typedStepUpProof(disableProofKind, disableProof)) {
		loading = true;
		try {
			await api.post('/api/auth/mfa/disable', proof);
			await auth.fetchUser();
			disableProof = '';
			toast(m('profile.mfa.disabledToast'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.mfa.disableFailed'), 'error');
		} finally {
			loading = false;
		}
	}

	function cancelEnroll() {
		enrollment = null;
		verifyCode = '';
	}

	// --- Passkeys (WebAuthn) — an additional MFA factor ----------------------
	import { isWebAuthnSupported } from '$lib/webauthn';
	import type { ActiveSession, Passkey, StepUpProof } from '$lib/stores/auth.svelte';
	import { formatDate } from '$lib/utils/time';

	let passkeys = $state<Passkey[] | null>(null);
	let passkeyName = $state('');
	// The typed proof for an add / remove: see `passkeyTypedProof`.
	let passkeyProof = $state('');
	let registeringPasskey = $state(false);
	let passkeysLoaded = $state(false);
	let passkeysError = $state(false);
	let passkeysBusy = $state(false);
	const webAuthnOk = isWebAuthnSupported();

	$effect(() => {
		if (section === 'passkeys' && !passkeysLoaded) {
			void loadPasskeys();
		}
	});

	async function loadPasskeys() {
		try {
			passkeys = await auth.listPasskeys();
			passkeysError = false;
		} catch {
			// Keep the list unknown (`null`), never an empty array. An empty array
			// is a *claim* — "this account has no passkeys" — and the step-up
			// decision below is built on it, so a failed fetch used to both tell
			// the user their passkeys were gone and disarm the proof the server
			// still demands, turning every Add/Remove into an opaque 400.
			passkeys = null;
			passkeysError = true;
		} finally {
			passkeysLoaded = true;
		}
	}

	/** Retry wrapper — `loadPasskeys` can't reset `passkeysLoaded` itself (the
	 * mount `$effect` keys off it and would re-fire), so the busy state carries
	 * the feedback instead. Mirrors `retrySessions` below. */
	async function retryPasskeys() {
		passkeysBusy = true;
		try {
			await loadPasskeys();
		} finally {
			passkeysBusy = false;
		}
	}

	// Adding a factor to an account that ALREADY has one is a step-up
	// operation server-side — otherwise a stolen session could bind an
	// attacker's authenticator. Mirror that here so the form asks for a
	// proof only when it's actually required.
	// Fails CLOSED on an unknown list: if the fetch failed we cannot rule out a
	// live factor, and the server will demand a proof regardless — so ask for
	// one rather than submit without and collect an opaque 400.
	const needsPasskeyStepUp = $derived(
		Boolean(auth.user?.mfa_enabled) || (passkeys?.length ?? 0) > 0 || passkeysError,
	);
	// A registered passkey is itself a step-up credential. That matters most for
	// an SSO-only account: no password, no authenticator code, so without this
	// its passkey is the only thing it can be challenged on — and factor
	// management would otherwise be closed to it entirely.
	// `passkeysError` counts here too: the step-up ceremony's options come from
	// the server (`POST /auth/mfa/step-up/passkey`), not from this list, so a
	// failed list fetch is no reason to withhold the one credential an SSO-only
	// account has. If there really is no passkey the server refuses — which is
	// the honest answer, not a bypass.
	const hasPasskey = $derived(((passkeys?.length ?? 0) > 0 || passkeysError) && webAuthnOk);

	/** What the card asks the member to type, if anything. The password where it
	 * is a proof; where it is not, a current authenticator code when TOTP is
	 * live, and nothing when it is not — the step-up is then the passkey
	 * ceremony, and `needsPasskeyStepUp` already guarantees there is a passkey
	 * to run it with (with no factor at all no step-up is asked, in any tenant). */
	const passkeyTypedProof = $derived<'password' | 'code' | null>(
		passwordIsProof ? 'password' : auth.user?.mfa_enabled ? 'code' : null,
	);
	// A blank field falls back to the passkey; a typed code must be a whole one.
	const canStepUp = $derived(
		!needsPasskeyStepUp ||
			(passkeyProof
				? passkeyTypedProof !== 'code' || isCompleteCode(passkeyProof)
				: hasPasskey),
	);

	/** Whichever proof the user has actually offered. A typed one wins (it needs
	 * no device interaction); otherwise fall back to the passkey prompt. */
	async function passkeyCardProof(
		operation: 'passkey_register' | 'passkey_delete',
	): Promise<StepUpProof> {
		if (!needsPasskeyStepUp) return {};
		if (passkeyProof && passkeyTypedProof) return typedStepUpProof(passkeyTypedProof, passkeyProof);
		return auth.passkeyStepUp(operation);
	}

	async function addPasskey() {
		registeringPasskey = true;
		try {
			const proof = await passkeyCardProof('passkey_register');
			// `'Passkey'` stays an English literal on purpose: it is the default
			// NAME persisted on the credential, not copy. The server stores it and
			// every later session reads it back, so translating it would make one
			// account's passkey list depend on which locale each device happened to
			// be in when its key was registered.
			await auth.registerPasskey(passkeyName.trim() || 'Passkey', proof);
			passkeyName = '';
			passkeyProof = '';
			await loadPasskeys();
			toast(m('profile.passkeys.added'), 'success');
		} catch (err) {
			// A user cancelling the browser prompt throws too — show a soft message.
			toast(err instanceof Error ? err.message : m('profile.passkeys.addFailed'), 'error');
		} finally {
			registeringPasskey = false;
		}
	}

	async function removePasskey(id: string) {
		try {
			// Removing a passkey always needs the step-up — the passkey itself is
			// a live factor, so the backend refuses a bare-session delete.
			await auth.deletePasskey(id, await passkeyCardProof('passkey_delete'));
			passkeyProof = '';
			await loadPasskeys();
			toast(m('profile.passkeys.removed'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.passkeys.removeFailed'), 'error');
		}
	}

	// --- Active sessions ----------------------------------------------------
	// The recovery path for a session you don't recognise. Revocation is armed
	// two-click (the same pattern as the API-key row action) rather than a
	// confirm dialog — signing a device out is reversible by signing back in,
	// but it shouldn't happen on a stray click either.

	let sessions = $state<ActiveSession[] | null>(null);
	let sessionsLoaded = $state(false);
	// A failed fetch must NOT read as "no other sessions are signed in" — on a
	// security screen that reassurance would be a lie, and the user would stop
	// looking for the session they came here to kill.
	let sessionsError = $state(false);
	let armedSessionId = $state<string | null>(null);
	let armedRevokeOthers = $state(false);
	let sessionBusy = $state(false);

	$effect(() => {
		if (section === 'sessions' && !sessionsLoaded) {
			void loadSessions();
		}
	});

	async function loadSessions() {
		try {
			sessions = await auth.listSessions();
			sessionsError = false;
		} catch {
			sessions = null;
			sessionsError = true;
		} finally {
			sessionsLoaded = true;
		}
	}

	/** Retry wrapper — `loadSessions` can't reset `sessionsLoaded` itself (the
	 * mount `$effect` keys off it and would re-fire), so the busy state carries
	 * the feedback instead. */
	async function retrySessions() {
		sessionBusy = true;
		try {
			await loadSessions();
		} finally {
			sessionBusy = false;
		}
	}

	const otherSessionCount = $derived((sessions ?? []).filter((s) => !s.current).length);

	async function revokeSession(id: string) {
		sessionBusy = true;
		try {
			await auth.revokeSession(id);
			armedSessionId = null;
			await loadSessions();
			toast(m('profile.sessions.revoked'), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.sessions.revokeFailed'), 'error');
		} finally {
			sessionBusy = false;
		}
	}

	async function revokeOtherSessions() {
		sessionBusy = true;
		try {
			const revoked = await auth.revokeOtherSessions();
			armedRevokeOthers = false;
			await loadSessions();
			toast(m('profile.sessions.revokedOthers', { n: revoked }), 'success');
		} catch (err) {
			toast(err instanceof Error ? err.message : m('profile.sessions.revokeOthersFailed'), 'error');
		} finally {
			sessionBusy = false;
		}
	}

	/**
	 * The signed-in user's roles, as a sentence they can read.
	 *
	 * This card printed `admin, ap_manager` — the raw slugs, ASCII-joined —
	 * while `/admin`'s user rows rendered the same roles as `Admin, AP Manager`.
	 * The join was correctly classified as identifiers in
	 * `listJoinAudit.test.ts`, and that classification was the OTHER half of the
	 * same defect: once the roles are labelled they are prose, so the separator
	 * has to be the locale's (Japanese enumerates with `、`) and the route leaves
	 * that file's `ALLOWED` table.
	 *
	 * A custom role falls back to its stored name — tenant data, never
	 * translated (`types/admin.ts::roleLabelKey`).
	 */
	function roleLabels(roles: string[] | undefined): string {
		return formatList(
			(roles ?? []).map((role) => {
				const key = roleLabelKey(role);
				return key ? m(key) : role;
			})
		);
	}

	function sessionLabel(s: ActiveSession): string {
		return s.device ?? m('profile.sessions.unknownDevice');
	}

	function sessionDetail(s: ActiveSession): string {
		const parts = [
			m('profile.sessions.signedIn', {
				date: formatDate(s.created_at, '—', {
					month: 'short',
					day: 'numeric',
					hour: 'numeric',
					minute: '2-digit',
				}),
			}),
		];
		if (s.ip) parts.push(s.ip);
		if (s.method) parts.push(s.method);
		return parts.join(' · ');
	}

	// Display-language picker. `currentLocale()` reads the reactive i18n rune,
	// so this stays in sync if the locale is changed elsewhere. The choice is
	// persisted to localStorage by `setLocale` (device-scoped, not account-roamed).
	const activeLocale = $derived(currentLocale());

	async function onLocaleChange(e: Event) {
		const value = (e.currentTarget as HTMLSelectElement).value as Locale;
		await setLocale(value);
	}
</script>

<!-- The one field a factor change asks the member to type. A snippet rather than
     a component so the card's scoped field styles still reach it. `password` is
     the account password; `code` a current authenticator code, asked for where
     the org has closed password sign-in (docs/decisions.md §201). -->
{#snippet stepUpField(
	kind: 'password' | 'code',
	label: string,
	value: string,
	onValue: (value: string) => void,
)}
	<label>
		<span>{label}</span>
		{#if kind === 'code'}
			<input
				type="text"
				inputmode="numeric"
				pattern="[0-9]*"
				maxlength="8"
				autocomplete="one-time-code"
				{value}
				oninput={(e) => onValue(e.currentTarget.value)}
			/>
		{:else}
			<input
				type="password"
				autocomplete="current-password"
				{value}
				oninput={(e) => onValue(e.currentTarget.value)}
			/>
		{/if}
	</label>
{/snippet}

<div class="workspace">
	<header class="toolbar">
		<h1>{m('shell.profileAndSecurity')}</h1>
	</header>

	<div class="settings-layout">
		<SettingsRail groups={railGroups} active={section} label={m('profile.rail.label')} />

		<div class="sections">
			{#if section === 'language'}
				<section class="card">
					<h2>{m('profile.language.heading')}</h2>
					<p class="hint">{m('profile.language.hint')}</p>
					<label>
						<span>{m('profile.language.label')}</span>
						<select
							value={activeLocale}
							onchange={onLocaleChange}
							aria-label={m('profile.language.label')}
						>
							{#each SUPPORTED_LOCALES as loc (loc)}
								<option value={loc}>{LOCALE_LABELS[loc]}</option>
							{/each}
						</select>
					</label>
				</section>
			{/if}

			{#if section === 'account'}
				<section class="card">
					<h2>{m('profile.account.heading')}</h2>
					<form
						onsubmit={(e) => {
							e.preventDefault();
							saveProfile();
						}}
					>
						<label>
							<span>{m('profile.account.fullName')}</span>
							<input type="text" bind:value={fullName} required autocomplete="name" />
						</label>
						<!-- Each term/definition pair is its own wrapping row (a <div> inside
						     a <dl> is valid and keeps the dt→dd association intact). A
						     two-column grid could not collapse at a 320px viewport: an
						     address like `someone@a-long-tenant.example` is one unbreakable
						     token, so the value column's min-content pushed the whole
						     document sideways — WCAG 1.4.10. -->
						<dl class="readonly">
							<div class="pair">
								<dt>{m('profile.account.email')}</dt>
								<dd>{auth.user?.email ?? '—'}</dd>
							</div>
							<div class="pair">
								<dt>{m('profile.account.roles')}</dt>
								<dd data-testid="profile-roles">{roleLabels(auth.user?.roles) || '—'}</dd>
							</div>
						</dl>
						<div class="actions">
							<button
								type="submit"
								disabled={savingProfile || !fullName.trim() || fullName === auth.user?.full_name}
							>
								{savingProfile ? m('common.saving') : m('common.save')}
							</button>
						</div>
					</form>
				</section>
			{/if}

			{#if section === 'password'}
				<section class="card">
					<h2>{m('profile.password.heading')}</h2>
					<p class="hint">{m('profile.password.hint')}</p>
					<form
						onsubmit={(e) => {
							e.preventDefault();
							changePassword();
						}}
					>
						<label>
							<span>{m('profile.password.current')}</span>
							<input
								type="password"
								bind:value={currentPassword}
								required
								autocomplete="current-password"
							/>
						</label>
						<label>
							<span>{m('profile.password.new')}</span>
							<input
								type="password"
								bind:value={newPassword}
								required
								minlength="6"
								autocomplete="new-password"
							/>
						</label>
						<label>
							<span>{m('profile.password.confirm')}</span>
							<input
								type="password"
								bind:value={confirmPassword}
								required
								minlength="6"
								autocomplete="new-password"
							/>
						</label>
						<div class="actions">
							<button
								type="submit"
								disabled={savingPassword || !currentPassword || !newPassword || newPassword !== confirmPassword}
							>
								{savingPassword ? m('common.saving') : m('profile.password.submit')}
							</button>
						</div>
					</form>
				</section>
			{/if}

			{#if section === 'mfa'}
				<section class="card">
					<h2>{m('profile.mfa.heading')}</h2>
					<p class="hint">{m('profile.mfa.hint')}</p>

					{#if auth.user?.mfa_enabled}
						<div class="status enabled">
							<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
							{m('profile.mfa.enabled')}
						</div>
						{#if auth.user?.mfa_required_by_org}
							<p class="hint">{m('profile.mfa.requiredNoDisable')}</p>
						{:else}
							<form
								onsubmit={(e) => {
									e.preventDefault();
									disable();
								}}
							>
								{#if !passwordIsProof}
									<p class="hint">{m('profile.mfa.ssoOnlyNoPassword')}</p>
								{/if}
								{@render stepUpField(
									disableProofKind,
									m(passwordIsProof ? 'profile.mfa.disablePassword' : 'profile.mfa.disableCode'),
									disableProof,
									(v) => (disableProof = v),
								)}
								<div class="actions">
									{#if hasPasskey}
										<!-- The typed proof's alternative: a registered passkey,
										     for an account with no password (or none the org
										     accepts) that would rather not type a code. -->
										<button
											type="button"
											class="secondary"
											disabled={loading}
											onclick={disableWithPasskey}
										>
											{m('profile.mfa.confirmWithPasskey')}
										</button>
									{/if}
									<button
										type="submit"
										class="danger"
										disabled={loading || !disableProofReady}
									>
										{loading ? m('profile.mfa.disabling') : m('profile.mfa.disable')}
									</button>
								</div>
							</form>
						{/if}
					{:else if enrollment}
						<div class="enroll">
							<p>
								<strong>{m('profile.mfa.step1Label')}</strong> {m('profile.mfa.step1Text')}
							</p>
							<img src={enrollment.qr_code_data_url} alt={m('profile.mfa.qrAlt')} class="qr" />
							<details>
								<summary>{m('profile.mfa.manualSecret')}</summary>
								<code class="secret">{enrollment.secret}</code>
							</details>
							<form
								onsubmit={(e) => {
									e.preventDefault();
									verifyEnroll();
								}}
							>
								<label>
									<span><strong>{m('profile.mfa.step2Label')}</strong> {m('profile.mfa.step2Text')}</span>
									<input
										type="text"
										inputmode="numeric"
										pattern="[0-9]*"
										bind:value={verifyCode}
										maxlength="8"
										autocomplete="one-time-code"
										required
									/>
								</label>
								<div class="actions">
									<button type="button" class="secondary" onclick={cancelEnroll}>{m('common.cancel')}</button>
									<button type="submit" disabled={loading || verifyCode.length < 6}>
										{loading ? m('profile.mfa.verifying') : m('profile.mfa.verifyAndEnable')}
									</button>
								</div>
							</form>
						</div>
					{:else}
						<div class="status disabled">{m('profile.mfa.notConfigured')}</div>
						{#if auth.user?.mfa_required_by_org}
							<p class="warn">{m('profile.mfa.requiredEnroll')}</p>
						{/if}
						<button onclick={startEnroll} disabled={loading}>
							{loading ? m('common.loading') : m('profile.mfa.setUp')}
						</button>
					{/if}
				</section>
			{/if}

			{#if section === 'passkeys'}
				<section class="card">
					<h2>{m('profile.passkeys.heading')}</h2>
					<p class="hint">{m('profile.passkeys.hint')}</p>

					{#if !webAuthnOk}
						<div class="status disabled">{m('profile.passkeys.unsupported')}</div>
					{:else}
						{#if needsPasskeyStepUp}
							<!-- One field for both operations: the backend requires a step-up
							     to add a factor to an account that already has one, and always
							     requires one to remove a passkey. Which proof it asks for is
							     `passkeyTypedProof` (docs/decisions.md §201). -->
							{#if !passwordIsProof}
								<p class="hint">{m('profile.mfa.ssoOnlyNoPassword')}</p>
							{/if}
							{#if passkeyTypedProof}
								{@render stepUpField(
									passkeyTypedProof,
									m(
										passkeyTypedProof === 'code'
											? 'profile.passkeys.stepUpCode'
											: 'profile.passkeys.stepUpPassword',
									),
									passkeyProof,
									(v) => (passkeyProof = v),
								)}
								{#if hasPasskey}
									<p class="hint">
										{m(
											passkeyTypedProof === 'code'
												? 'profile.passkeys.stepUpCodeBlankHint'
												: 'profile.passkeys.stepUpBlankHint',
										)}
									</p>
								{/if}
							{:else}
								<p class="hint">{m('profile.passkeys.stepUpPasskeyOnly')}</p>
							{/if}
						{/if}

						{#if passkeys && passkeys.length > 0}
							<ul class="entry-list">
								{#each passkeys as pk (pk.id)}
									<li>
										<div class="entry-meta">
											<span class="entry-name">{pk.name}</span>
											{#if pk.last_used_at}
												<span class="entry-sub">
													{m('profile.passkeys.lastUsed', {
														date: formatDate(pk.last_used_at, '—', {
															year: 'numeric',
															month: 'numeric',
															day: 'numeric'
														})
													})}
												</span>
											{:else}
												<span class="entry-sub">{m('profile.passkeys.neverUsed')}</span>
											{/if}
										</div>
										<button
											type="button"
											class="danger small"
											disabled={!canStepUp}
											onclick={() => removePasskey(pk.id)}
										>
											{m('profile.passkeys.remove')}
										</button>
									</li>
								{/each}
							</ul>
						{:else if passkeysError}
							<p class="warn">{m('profile.passkeys.loadFailed')}</p>
							<div class="actions">
								<button
									type="button"
									class="secondary"
									disabled={passkeysBusy}
									onclick={retryPasskeys}
								>
									{passkeysBusy ? m('common.retrying') : m('common.tryAgain')}
								</button>
							</div>
						{:else if passkeysLoaded}
							<div class="status disabled">{m('profile.passkeys.none')}</div>
						{/if}

						<form
							onsubmit={(e) => {
								e.preventDefault();
								addPasskey();
							}}
						>
							<label>
								<span>{m('profile.passkeys.nameLabel')}</span>
								<input
									type="text"
									bind:value={passkeyName}
									maxlength="120"
									placeholder={m('profile.passkeys.namePlaceholder')}
								/>
							</label>
							<div class="actions">
								<button
									type="submit"
									disabled={registeringPasskey || !canStepUp}
								>
									{registeringPasskey ? m('profile.passkeys.waiting') : m('profile.passkeys.add')}
								</button>
							</div>
						</form>
					{/if}
				</section>
			{/if}

			{#if section === 'sessions'}
				<section class="card">
					<h2>{m('profile.sessions.heading')}</h2>
					<p class="hint">{m('profile.sessions.hint')}</p>

					{#if !sessionsLoaded}
						<p class="hint">{m('common.loading')}</p>
					{:else if sessionsError}
						<p class="warn">{m('profile.sessions.loadFailed')}</p>
						<div class="actions">
							<button type="button" class="secondary" disabled={sessionBusy} onclick={retrySessions}>
								{sessionBusy ? m('common.retrying') : m('common.tryAgain')}
							</button>
						</div>
					{:else if sessions && sessions.length > 0}
						<ul class="entry-list">
							{#each sessions as s (s.id)}
								<li>
									<div class="entry-meta">
										<span class="entry-name">
											{sessionLabel(s)}
											{#if s.current}<span class="badge">{m('profile.sessions.thisDevice')}</span>{/if}
										</span>
										<span class="entry-sub">{sessionDetail(s)}</span>
									</div>
									{#if !s.current}
										<button
											type="button"
											class="danger small"
											disabled={sessionBusy}
											onclick={() => {
												if (armedSessionId === s.id) {
													revokeSession(s.id);
												} else {
													armedSessionId = s.id;
												}
											}}
										>
											{armedSessionId === s.id
												? m('profile.sessions.confirmSignOut')
												: m('profile.sessions.signOut')}
										</button>
									{/if}
								</li>
							{/each}
						</ul>

						{#if otherSessionCount > 0}
							<div class="actions">
								<button
									type="button"
									class="danger"
									disabled={sessionBusy}
									onclick={() => {
										if (armedRevokeOthers) {
											revokeOtherSessions();
										} else {
											armedRevokeOthers = true;
										}
									}}
								>
									{armedRevokeOthers
										? m('profile.sessions.confirmSignOutOthers', { n: otherSessionCount })
										: m('profile.sessions.signOutOthers')}
								</button>
							</div>
						{/if}
					{:else}
						<div class="status disabled">{m('profile.sessions.none')}</div>
					{/if}
				</section>
			{/if}

			{#if section === 'notifications'}
				<section class="card">
					<h2>{m('profile.notifications.heading')}</h2>
					<p class="hint">
						{m('profile.notifications.hint', {
							email: auth.user?.email ?? m('profile.notifications.yourAddress')
						})}
					</p>

					{#if !prefsLoaded}
						<p class="hint">{m('common.loading')}</p>
					{:else if notificationStore.prefs}
						{@const prefs = normalizePrefs(notificationStore.prefs)}
						<table class="prefs-table">
							<thead>
								<tr>
									<th>{m('profile.notifications.colEvent')}</th>
									<th class="center">{m('profile.notifications.colInApp')}</th>
									<th class="center">{m('profile.notifications.colEmail')}</th>
								</tr>
							</thead>
							<tbody>
								{#each EVENT_ORDER as event (event)}
									{@const label = m(EVENT_LABEL_KEYS[event])}
									<tr>
										<td>{label}</td>
										<td class="center">
											<input
												type="checkbox"
												checked={prefs[event].in_app}
												disabled={savingPrefs}
												onchange={() => togglePref(event, 'in_app')}
												aria-label={m('profile.notifications.inAppFor', { event: label })}
											/>
										</td>
										<td class="center">
											<input
												type="checkbox"
												checked={prefs[event].email}
												disabled={savingPrefs}
												onchange={() => togglePref(event, 'email')}
												aria-label={m('profile.notifications.emailFor', { event: label })}
											/>
										</td>
									</tr>
								{/each}
							</tbody>
						</table>
					{:else}
						<p class="warn">{m('profile.notifications.loadFailed')}</p>
					{/if}
				</section>
			{/if}
		</div>
	</div>
</div>

<style>
	.workspace {
		max-width: 1800px;
		margin: 0 auto;
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 16px;
		min-height: 100vh;
	}

	/* Rail beside panel — the same shape `/organization` uses, and the same
	   reason for `minmax(0, 1fr)`: a grid item's default `min-width: auto` is
	   its content width, so the widest panel (the notification-preference grid)
	   would push the track past the viewport and scroll the document sideways
	   (WCAG 1.4.10). One column below the breakpoint, where the rail collapses
	   into its own disclosure and sits above the panel. */
	.settings-layout {
		display: grid;
		gap: 20px;
	}

	@media (min-width: 60rem) {
		.settings-layout {
			grid-template-columns: 200px minmax(0, 1fr);
			gap: 28px;
			/* Not `stretch`: the rail is sticky, and a stretched grid item fills
			   the row, leaving it nothing to stick within. */
			align-items: start;
		}
	}

	.toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}

	.sections {
		display: flex;
		flex-direction: column;
		gap: 16px;
	}

	h1 {
		margin: 0;
		font-size: 1.3rem;
		font-weight: 700;
	}

	h2 {
		margin: 0 0 12px;
		font-size: 1rem;
		font-weight: 600;
	}

	.card {
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 24px;
	}

	.hint {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 0 0 16px;
	}

	.warn {
		color: var(--warning-on-tint);
		font-size: 0.85rem;
		background: var(--warning-tint);
		border: 1px solid rgba(255, 165, 0, 0.3);
		padding: 8px 12px;
		border-radius: 4px;
		margin: 0 0 12px;
	}

	dl {
		display: flex;
		flex-direction: column;
		gap: 8px;
		margin: 0;
	}

	/* One term/definition row. Above ~300px the pair sits side by side exactly
	   as the old `120px 1fr` grid did; below it the value drops onto its own
	   full-width line instead of overflowing (WCAG 1.4.10). No media query:
	   the flex basis is the breakpoint. */
	.pair {
		display: flex;
		flex-wrap: wrap;
		gap: 2px 16px;
	}

	.pair dt {
		flex: 0 1 120px;
	}

	.pair dd {
		flex: 1 1 12rem;
		min-width: 0;
		/* An email address is a single unbreakable token. */
		overflow-wrap: anywhere;
	}

	dl.readonly {
		margin-top: 4px;
		padding: 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
	}

	dt {
		color: var(--text-muted);
		font-size: 0.82rem;
	}

	dd {
		margin: 0;
		font-size: 0.9rem;
	}

	.status {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		border-radius: 4px;
		font-size: 0.82rem;
		font-weight: 500;
		margin-bottom: 12px;
	}

	.status.enabled {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.status.disabled {
		background: var(--bg);
		color: var(--text-muted);
	}

	.enroll {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.qr {
		width: 200px;
		height: 200px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: #fff;
		padding: 8px;
		align-self: flex-start;
	}

	details summary {
		cursor: pointer;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	.secret {
		display: block;
		margin-top: 8px;
		padding: 8px 12px;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		font-family: monospace;
		font-size: 0.85rem;
		word-break: break-all;
	}

	form {
		display: flex;
		flex-direction: column;
		gap: 12px;
		margin-top: 12px;
	}

	label {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.85rem;
		color: var(--text-muted);
	}

	/* The TEXT-entry recipe, carved away from the checkbox/radio it also
	   reached. Svelte scopes this to `.card.svelte-x input:where(.svelte-x)`,
	   which outranks the global control base in `app.css`, and `background:`
	   is a SHORTHAND — it reset `background-image`, the drawn tick, so the
	   two notification-preference toggle rendered identically checked and unchecked. */
	input:not([type='checkbox']):not([type='radio']) {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 4px;
		padding: 10px 12px;
		font-size: 0.9rem;
		color: var(--text);
		font-family: inherit;
	}

	/* Same carve-out: `outline: none` here would strip the checkbox/radio
	   focus ring `app.css` draws for them (WCAG 2.4.7). */
	input:not([type='checkbox']):not([type='radio']):focus {
		outline: none;
		border-color: var(--accent);
	}

	.actions {
		display: flex;
		gap: 8px;
	}

	button {
		padding: 9px 16px;
		border-radius: 4px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.88rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	button:disabled {
		opacity: 0.6;
		cursor: not-allowed;
	}

	button.secondary {
		background: transparent;
		border: 1px solid var(--border);
		color: var(--text);
	}

	button.danger {
		background: #e04040;
	}

	button.small {
		padding: 5px 12px;
		font-size: 0.8rem;
	}

	.entry-list {
		list-style: none;
		margin: 0 0 16px;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.entry-list li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		padding: 10px 14px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--bg);
	}

	.entry-meta {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.entry-name {
		font-weight: 600;
		color: var(--text);
	}

	.entry-sub {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.badge {
		margin-left: 8px;
		padding: 1px 8px;
		border-radius: 999px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--accent);
		background: color-mix(in srgb, var(--accent) 14%, transparent);
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
	}

	.prefs-table {
		width: 100%;
		border-collapse: collapse;
		font-size: 0.88rem;
	}

	.prefs-table th {
		text-align: left;
		padding: 8px 10px;
		color: var(--text-muted);
		font-size: 0.78rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		border-bottom: 1px solid var(--border);
	}

	.prefs-table td {
		padding: 10px;
		border-bottom: 1px solid var(--border);
	}

	.prefs-table .center {
		text-align: center;
		width: 90px;
	}

	.prefs-table input[type='checkbox'] {
		width: auto;
		cursor: pointer;
	}
</style>
