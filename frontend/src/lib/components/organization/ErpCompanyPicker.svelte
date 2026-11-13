<script lang="ts">
	/**
	 * Organization → ERP: choose which company a consent connects.
	 *
	 * Shown by `ErpConnectionPanel` when the OAuth callback returns with
	 * `?erp_choose_company=` — the consent reached several companies (a Xero
	 * login that authorised several organisations, a Sage login with several
	 * businesses) and FeohLedger never picks one of a customer's ledgers for
	 * them (decisions §355). The list comes from the server, which holds the
	 * token; this component only says which company, and the panel makes the
	 * calls (`#lib/api/erpConnection.ts`).
	 *
	 * A radio group in a `fieldset` whose `legend` is the question, so a screen
	 * reader announces it on entering the group (WCAG 1.3.1). Focus moves to
	 * the heading when it appears: the admin has just come back from the ERP
	 * and this is the one thing left to do (WCAG 2.4.3).
	 */
	import { onMount } from 'svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatDate } from '#lib/utils/time.ts';
	import type { ErpPendingConnect } from '#lib/types/erpConnection.ts';

	let {
		erp,
		pending,
		submitting,
		onconnect,
		oncancel
	}: {
		/** The ERP's display name. */
		erp: string;
		pending: ErpPendingConnect;
		/** A pick or a cancel is in flight. */
		submitting: boolean;
		onconnect: (companyId: string) => void;
		oncancel: () => void;
	} = $props();

	const uid = $props.id();
	let chosen = $state('');
	let missing = $state(false);
	let heading = $state<HTMLHeadingElement | null>(null);

	onMount(() => heading?.focus());

	function submit(e: SubmitEvent) {
		e.preventDefault();
		if (!chosen) {
			missing = true;
			document.getElementById(`${uid}-0`)?.focus();
			return;
		}
		onconnect(chosen);
	}
</script>

<form class="picker" data-testid="erp-company-picker" onsubmit={submit} novalidate>
	<h3 id="{uid}-title" tabindex="-1" bind:this={heading}>{m('org.erp.oauth.pick.title', { erp })}</h3>
	<p class="hint" id="{uid}-hint">
		{m('org.erp.oauth.pick.hint', { erp })}
		{m('org.erp.oauth.pick.expiresAt', {
			time: formatDate(pending.expires_at, '—', { hour: 'numeric', minute: '2-digit' })
		})}
	</p>
	<fieldset
		aria-labelledby="{uid}-title"
		aria-describedby={missing ? `${uid}-hint ${uid}-missing` : `${uid}-hint`}
		disabled={submitting}
	>
		{#each pending.companies as company, i (company.id)}
			<label class="option" class:selected={chosen === company.id}>
				<input
					id="{uid}-{i}"
					type="radio"
					name="{uid}-company"
					value={company.id}
					bind:group={chosen}
					onchange={() => (missing = false)}
					data-testid="erp-company-option"
				/>
				<span class="option-text">
					<span class="name">{company.name}</span>
					{#if company.name !== company.id}
						<span class="id">{m('org.erp.oauth.pick.companyId', { id: company.id })}</span>
					{/if}
				</span>
			</label>
		{/each}
	</fieldset>
	{#if missing}
		<p class="missing" id="{uid}-missing" role="alert">{m('org.erp.oauth.pick.noneSelected')}</p>
	{/if}
	<div class="actions">
		<button type="submit" class="btn-primary" disabled={submitting} data-testid="erp-company-connect">
			{submitting ? m('org.erp.oauth.pick.submitting') : m('org.erp.oauth.pick.submit')}
		</button>
		<button
			type="button"
			class="btn-secondary"
			disabled={submitting}
			onclick={oncancel}
			data-testid="erp-company-cancel"
		>
			{m('org.erp.oauth.pick.cancel')}
		</button>
	</div>
</form>

<style>
	.picker {
		margin-top: 16px;
		padding: 14px 16px;
		border: 1px solid var(--accent);
		border-radius: 6px;
	}

	h3 {
		font-size: 0.95rem;
		font-weight: 600;
		margin: 0 0 6px;
	}

	h3:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0 0 12px;
		line-height: 1.5;
	}

	fieldset {
		border: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.option {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 6px;
		cursor: pointer;
		/* 24px minimum target (WCAG 2.5.8): the whole row is the label. */
		min-height: 24px;
	}

	.option.selected {
		border-color: var(--accent);
	}

	.option input {
		margin: 2px 0 0;
		flex: none;
	}

	.option-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		/* A long company name or id wraps at 320px (WCAG 1.4.10). */
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.name {
		font-size: 0.88rem;
		color: var(--text);
	}

	.id {
		font-size: 0.78rem;
		color: var(--text-muted);
	}

	.missing {
		font-size: 0.82rem;
		color: var(--danger);
		margin: 8px 0 0;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		margin-top: 14px;
	}

	.btn-primary,
	.btn-secondary {
		padding: 8px 18px;
		border-radius: 6px;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
		font-family: inherit;
	}

	.btn-primary {
		border: none;
		background: var(--accent-strong);
		color: #fff;
	}

	.btn-secondary {
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
	}

	.btn-primary:disabled,
	.btn-secondary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
