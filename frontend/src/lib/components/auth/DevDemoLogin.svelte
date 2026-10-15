<script lang="ts">
	// One-click sign-in as a seeded demo user — LOCAL DEV ONLY.
	//
	// The login page loads this with a dynamic import behind
	// `import.meta.env.DEV`, which `vite build` folds to `false` — the chunk is
	// never emitted, so neither the list nor the shared seed password reaches
	// the static bundle shipped to S3. The accounts mirror `backend/scripts/seed.py`
	// (every seeded user's password is "demo"); a tenant with no entry here —
	// the e2e workers, anything you provisioned yourself — renders nothing.
	//
	// The labels deliberately avoid the words "Sign in": e2e specs locate the
	// real submit with `getByRole('button', { name: /Sign in/ })`, and Playwright
	// also runs against `vite dev`. Copy is untranslated because no end user can
	// ever see it.
	import { getTenantSlug } from '#lib/tenant.ts';

	interface Props {
		onpick: (email: string, password: string) => void;
		disabled?: boolean;
	}

	let { onpick, disabled = false }: Props = $props();

	const SEED_PASSWORD = 'demo';

	const ACCOUNTS: Record<string, { email: string; name: string; role: string }[]> = {
		acme: [
			{ email: 'demo@acme.com', name: 'Alice Admin', role: 'admin' },
			{ email: 'demo+apmanager@acme.com', name: 'Marcus Manager', role: 'ap_manager' },
			{ email: 'demo+apclerk@acme.com', name: 'Clara Clerk', role: 'ap_clerk' },
			{ email: 'demo+cfo@acme.com', name: 'Frank CFO', role: 'cfo' }
		],
		techflow: [
			{ email: 'admin@techflow.com', name: 'Tina TechAdmin', role: 'admin' },
			{ email: 'clerk@techflow.com', name: 'Carlos Clerk', role: 'ap_clerk' }
		]
	};

	const accounts = ACCOUNTS[getTenantSlug() ?? ''] ?? [];
</script>

{#if accounts.length}
	<section class="dev-demo" aria-labelledby="dev-demo-heading">
		<h2 id="dev-demo-heading">Demo accounts <span>(dev only)</span></h2>
		<div class="list">
			{#each accounts as a (a.email)}
				<button type="button" {disabled} onclick={() => onpick(a.email, SEED_PASSWORD)}>
					<span class="name">{a.name}</span>
					<span class="role">{a.role}</span>
				</button>
			{/each}
		</div>
	</section>
{/if}

<style>
	.dev-demo {
		margin-top: 22px;
		padding: 14px;
		border: 1px dashed var(--border);
		border-radius: 10px;
	}
	h2 {
		margin: 0 0 10px;
		font-size: 0.8rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}
	h2 span {
		text-transform: none;
		letter-spacing: 0;
		font-weight: 400;
	}
	.list {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: 8px;
	}
	button {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 2px;
		min-height: 44px;
		padding: 8px 12px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		cursor: pointer;
		text-align: left;
	}
	button:hover:not(:disabled) {
		border-color: var(--accent);
	}
	button:disabled {
		cursor: progress;
		opacity: 0.6;
	}
	.name {
		font-size: 0.88rem;
		font-weight: 500;
	}
	.role {
		font-size: 0.75rem;
		color: var(--text-muted);
	}
</style>
