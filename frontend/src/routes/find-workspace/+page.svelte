<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { goto } from '$app/navigation';
	import AuthShell from '#lib/components/auth/AuthShell.svelte';
	import AuthBrandMark from '#lib/components/auth/AuthBrandMark.svelte';
	import LinkedMessage from '#lib/components/ui/LinkedMessage.svelte';
	import { api } from '#lib/api.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';
	import {
		normalizeWorkspaceInput,
		ownDomainHost,
		tenantHostSuffix,
		workspaceAddressProblem,
		workspaceLoginUrl,
		type WorkspaceAddressProblem
	} from '#lib/utils/workspaceAddress.ts';
	import { DEFAULT_PRODUCT_NAME } from '#lib/stores/brandTheme.ts';
	import { hasTenantContext } from '#lib/tenant.ts';
	import type { PublicConfig } from '#lib/types/publicConfig.ts';

	/**
	 * Sign-in from the platform apex (#635 finding 4). The apex has no sign-in
	 * form of its own — every company signs in at its own workspace host — so
	 * this asks which workspace and sends the visitor to that host's `/login`.
	 * The landing page's "Sign in" links here, and the root layout routes an
	 * apex `/login` here too.
	 *
	 * **The visitor types the address; nothing is looked up.** An email →
	 * workspace search would tell anyone whether a person is a customer, and
	 * where — an account-enumeration oracle on a public page. See
	 * `#lib/utils/workspaceAddress.ts` and docs/multi-tenancy.md.
	 */

	const PROBLEM_KEYS: Record<WorkspaceAddressProblem, MessageKey> = {
		empty: 'marketing.findWorkspace.problem.empty',
		startLetter: 'marketing.findWorkspace.problem.startLetter',
		characters: 'marketing.findWorkspace.problem.characters',
		tooShort: 'marketing.findWorkspace.problem.tooShort',
		tooLong: 'marketing.findWorkspace.problem.tooLong',
		endHyphen: 'marketing.findWorkspace.problem.endHyphen',
		doubleHyphen: 'marketing.findWorkspace.problem.doubleHyphen'
	};

	const product = DEFAULT_PRODUCT_NAME;
	// An example address, not copy: the same in every language, like signup's.
	const EXAMPLE_ADDRESS = 'acme';

	let address = $state('');
	let problem = $state<WorkspaceAddressProblem | null>(null);
	// A whole domain that is not the platform's: a workspace on its own custom
	// domain. Offered as a link to that host's sign-in, never followed by itself.
	let ownDomain = $state<string | null>(null);
	// The deployment's `tenant_url_template`, from the public config. Until it
	// arrives there is no host to send anyone to, and nothing is guessed.
	let template = $state<string | null>(null);
	let configStatus = $state<'loading' | 'ready' | 'error'>('loading');
	// The host being opened, once the address has been accepted.
	let goingTo = $state<string | null>(null);
	let input = $state<HTMLInputElement | null>(null);

	const suffix = $derived(tenantHostSuffix(template));

	async function loadConfig() {
		configStatus = 'loading';
		try {
			const cfg = await api.get<PublicConfig>('/api/public-config');
			template = cfg.tenant_url_template;
			configStatus = tenantHostSuffix(template) === null ? 'error' : 'ready';
		} catch {
			configStatus = 'error';
		}
	}

	onMount(() => {
		// A workspace host already IS a workspace: its own sign-in is `/login`,
		// and `/` takes a signed-out visitor there.
		if (hasTenantContext()) {
			goto('/', { replace: true });
			return;
		}
		void loadConfig();
	});

	function onInput() {
		problem = null;
		ownDomain = null;
	}

	async function onSubmit(event: SubmitEvent) {
		event.preventDefault();
		if (goingTo) return;
		const slug = normalizeWorkspaceInput(address, suffix);
		ownDomain = ownDomainHost(slug, suffix);
		if (ownDomain) {
			problem = null;
			return;
		}
		problem = workspaceAddressProblem(slug);
		if (problem) {
			await tick();
			input?.focus();
			return;
		}
		const url = workspaceLoginUrl(slug, template);
		if (!url) {
			// The config never arrived (or names no usable host): the error
			// banner with its Retry is already on screen.
			configStatus = 'error';
			return;
		}
		address = slug;
		goingTo = new URL(url).host;
		window.location.assign(url);
	}
</script>

<!-- Back from the workspace host restores this page from the bfcache with
     the button still saying it is opening that host; give the form back. -->
<svelte:window
	onpageshow={(e) => {
		if (e.persisted) goingTo = null;
	}}
/>

<svelte:head>
	<title>{m('marketing.findWorkspace.pageTitle', { product })}</title>
</svelte:head>

{#snippet why()}
	<h3 class="why-heading">{m('marketing.findWorkspace.panelHeading')}</h3>
	<p class="why-body">{m('marketing.findWorkspace.panelBody')}</p>
{/snippet}

<!-- The panel stays on phones: it is the one answer to "why can't you just
     find it for me?", the question this page most invites. -->
<AuthShell panel={why} panelOnMobile>
	<form class="find-form" onsubmit={onSubmit} novalidate>
		<div class="head">
			<AuthBrandMark />
			<h1>{m('marketing.findWorkspace.heading')}</h1>
			<p class="sub">{m('marketing.findWorkspace.subtitle')}</p>
		</div>

		<div role="alert" aria-live="assertive">
			{#if configStatus === 'error'}
				<div class="error">
					<span>{m('marketing.findWorkspace.configError')}</span>
					<button type="button" class="link-btn" onclick={loadConfig}>
						{m('marketing.findWorkspace.retry')}
					</button>
				</div>
			{/if}
		</div>

		<div class="field">
			<label for="workspace-address"><span>{m('marketing.findWorkspace.label')}</span></label>
			<!-- The input and the platform's host read as ONE field, the way the
			     signup page composes the same address. -->
			<div class="address-row" class:bad={problem !== null}>
				<input
					id="workspace-address"
					class="address-input"
					bind:this={input}
					bind:value={address}
					oninput={onInput}
					required
					maxlength="253"
					placeholder={EXAMPLE_ADDRESS}
					autocomplete="off"
					autocapitalize="off"
					autocorrect="off"
					spellcheck="false"
					enterkeyhint="go"
					aria-invalid={problem !== null}
					aria-describedby={suffix ? 'workspace-suffix workspace-hint' : 'workspace-hint'}
				/>
				{#if suffix}
					<span class="address-suffix" id="workspace-suffix">{suffix}</span>
				{/if}
			</div>
			<div id="workspace-hint" aria-live="polite">
				{#if ownDomain}
					<!-- What the visitor typed, offered back as a link: a company on
					     its own domain signs in there, not under the platform host. -->
					<small class="hint own-domain">
						<LinkedMessage
							text={m('marketing.findWorkspace.ownDomain', { host: ownDomain })}
							links={{
								link: {
									href: `https://${ownDomain}/login`,
									label: m('marketing.findWorkspace.ownDomainLink', { host: ownDomain })
								}
							}}
						/>
					</small>
				{:else if problem}
					<small class="hint bad">{m(PROBLEM_KEYS[problem])}</small>
				{:else}
					<small class="hint">{m('marketing.findWorkspace.hint')}</small>
				{/if}
			</div>
		</div>

		<button type="submit" disabled={configStatus === 'loading' || goingTo !== null}>
			{goingTo ? m('marketing.findWorkspace.going', { host: goingTo }) : m('marketing.findWorkspace.submit')}
		</button>

		<p class="sub small">{m('marketing.findWorkspace.lost')}</p>

		<p class="footer">
			<LinkedMessage
				text={m('marketing.findWorkspace.noWorkspace', { product })}
				links={{ create: { href: '/signup', label: m('marketing.cta.create') } }}
			/>
		</p>
	</form>
</AuthShell>

<style>
	/* Field chrome, the entrance stagger, the submit button and the error
	   banner come from AuthShell; this is the composite address field (the
	   same recipe as signup's) and the page's own fine print. */
	.find-form {
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
	.sub.small {
		font-size: 0.82rem;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.address-row {
		display: flex;
		align-items: center;
		min-height: 46px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		transition: border-color 0.15s, box-shadow 0.15s;
	}
	.address-row:hover {
		border-color: var(--border-strong);
	}
	.address-row:focus-within {
		border-color: var(--accent);
		box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 22%, transparent);
	}
	.address-row.bad {
		border-color: var(--danger);
	}
	/* The row carries the chrome, so the input inside it carries none. Wins
	   over AuthShell's field defaults, which the shell writes under `:where()`. */
	.address-row .address-input {
		flex: 1;
		min-width: 0;
		min-height: 44px;
		border: none;
		background: transparent;
		box-shadow: none;
	}
	/* A long platform host must not push the field past a 320px screen: the
	   suffix may shrink behind an ellipsis, and the whole host stays in the
	   input's accessible description either way. */
	.address-suffix {
		flex: 0 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		padding-right: 12px;
		color: var(--text-muted);
		font-size: 0.88rem;
		white-space: nowrap;
	}

	.hint {
		display: block;
		margin-top: 2px;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	.hint.bad {
		color: var(--danger);
	}
	.hint.own-domain {
		font-size: 0.85rem;
		color: var(--text);
		overflow-wrap: anywhere;
	}
	.hint.own-domain :global(a) {
		color: var(--accent-on-tint);
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 2px;
	}

	.link-btn {
		align-self: flex-start;
		padding: 0;
		border: none;
		background: none;
		color: inherit;
		font: inherit;
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 2px;
		cursor: pointer;
	}

	.footer {
		margin: 0;
		padding-top: 16px;
		border-top: 1px solid var(--border);
		font-size: 0.85rem;
		color: var(--text-muted);
		text-align: center;
	}
	/* Underlined at rest: inside muted text, colour alone does not mark a link
	   (WCAG 1.4.1). */
	.footer :global(a) {
		color: var(--accent-on-tint);
		font-weight: 600;
		text-decoration: underline;
		text-underline-offset: 2px;
	}

	.why-heading {
		margin: 0 0 8px;
		font-size: 1rem;
		font-weight: 700;
		color: var(--text);
	}
	.why-body {
		margin: 0;
		font-size: 0.9rem;
		line-height: 1.6;
		color: var(--text-muted);
	}
</style>
