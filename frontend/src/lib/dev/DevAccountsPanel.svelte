<script lang="ts">
	/**
	 * One-click seeded logins for LOCAL DEVELOPMENT (`routes/login`).
	 *
	 * Loaded only through a dynamic import inside `import.meta.env.DEV`, so it
	 * and `devAccounts.ts` are dead code in `pnpm build` and never reach the
	 * deployed bundle — `scripts/check_dev_accounts_stripped.mjs` proves that
	 * on the CI build. Renders nothing on a host with no seeded accounts, so
	 * it has no layout effect there.
	 */
	import { m } from '#lib/i18n/store.svelte.ts';
	import { ROLE_LABEL_KEYS } from '#lib/types/admin.ts';
	import { devAccountsFor, type DevAccount } from './devAccounts.ts';

	type Props = {
		slug: string | null;
		onpick: (account: DevAccount) => void;
	};

	let { slug, onpick }: Props = $props();

	const accounts = $derived(devAccountsFor(slug));
</script>

{#if accounts.length > 0}
	<section class="dev-accounts" aria-labelledby="dev-accounts-heading" data-dev-accounts>
		<h2 id="dev-accounts-heading">{m('auth.login.devAccounts.heading')}</h2>
		<p class="hint">{m('auth.login.devAccounts.hint')}</p>
		<ul>
			{#each accounts as account (account.email)}
				{@const role = m(ROLE_LABEL_KEYS[account.role])}
				<li>
					<button
						type="button"
						aria-label={m('auth.login.devAccounts.fill', { email: account.email, role })}
						onclick={() => onpick(account)}
					>
						<span class="email">{account.email}</span>
						<span class="role">{role}</span>
					</button>
				</li>
			{/each}
		</ul>
	</section>
{/if}

<style>
	.dev-accounts {
		margin-top: 20px;
		padding: 12px 14px;
		border: 1px dashed var(--border);
		border-radius: var(--radius);
	}
	h2 {
		margin: 0 0 4px;
		font-size: 0.85rem;
		font-weight: 600;
	}
	.hint {
		margin: 0 0 8px;
		font-size: 0.78rem;
		color: var(--text-muted);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	button {
		appearance: none;
		width: 100%;
		min-height: 32px;
		display: flex;
		justify-content: space-between;
		gap: 8px;
		padding: 6px 8px;
		border: 1px solid var(--border);
		border-radius: 6px;
		background: var(--surface);
		color: var(--text);
		font: inherit;
		font-size: 0.8rem;
		text-align: left;
		cursor: pointer;
	}
	button:hover {
		border-color: var(--accent);
	}
	button:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.email {
		overflow-wrap: anywhere;
	}
	.role {
		flex-shrink: 0;
		color: var(--text-muted);
	}
</style>
