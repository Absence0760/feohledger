<script lang="ts">
	import type { Snippet } from 'svelte';
	import Modal from './Modal.svelte';
	import { toast } from './Toast.svelte';

	/**
	 * One-time secret reveal dialog.
	 *
	 * The single place the app shows a credential the server will never return
	 * again — an API-key mint, a webhook signing secret at create time, and the
	 * replacement minted by a secret rotation. There is exactly one chance to
	 * get the value to the user, so the handling rules are the same every time
	 * and belong in one component rather than in three copies that drift:
	 *
	 *  - the plaintext is a **prop**, never state here. This component neither
	 *    stores, caches, nor logs it; the caller drops its own copy on close and
	 *    the value leaves the DOM with the dialog.
	 *  - a copy button (clipboard only — the value is never written anywhere
	 *    else), with a "Copied" acknowledgement.
	 *  - an unmissable warning that it is shown once.
	 *  - `testId` lands on the element holding the plaintext; the e2e specs
	 *    assert it is gone after dismissal, which is how "never echoed later"
	 *    is actually enforced.
	 *
	 * Every user-facing string is passed in already-localized, so the component
	 * stays i18n-agnostic and each caller keeps its own `admin.*` key namespace.
	 */

	type MetaRow = {
		label: string;
		value: string;
		/** Render the value in the monospace face (prefixes, ids). */
		mono?: boolean;
	};

	type Props = {
		open: boolean;
		/** The dialog's `aria-label` — e2e specs select modals by this exact string. */
		ariaLabel: string;
		heading: string;
		/** Lead-in rendered bold inside the warning banner. */
		warningStrong: string;
		warning: string;
		/** The plaintext credential. Shown verbatim; never persisted. */
		secret: string;
		/** `data-testid` for the element holding the plaintext. */
		testId: string;
		copyLabel: string;
		copiedLabel: string;
		copiedToast: string;
		copyFailedToast: string;
		doneLabel: string;
		/** Non-secret context rows (name, prefix) under the value. */
		meta?: MetaRow[];
		/** Optional extra content below the meta rows (e.g. a rotation's overlap notice). */
		note?: Snippet;
		onclose: () => void;
	};

	let {
		open,
		ariaLabel,
		heading,
		warningStrong,
		warning,
		secret,
		testId,
		copyLabel,
		copiedLabel,
		copiedToast,
		copyFailedToast,
		doneLabel,
		meta = [],
		note,
		onclose
	}: Props = $props();

	let copied = $state(false);

	// Reset the acknowledgement when the dialog closes, so the next reveal
	// doesn't open already reading "Copied".
	$effect(() => {
		if (!open) copied = false;
	});

	async function copy() {
		try {
			await navigator.clipboard.writeText(secret);
			copied = true;
			toast(copiedToast, 'success');
		} catch {
			// Clipboard access can be denied (permissions, insecure context). The
			// value is still selectable in the DOM, so say so rather than failing
			// silently — this is the user's only chance to capture it.
			toast(copyFailedToast, 'error');
		}
	}
</script>

<Modal {open} {ariaLabel} width="md" {onclose}>
	<h2>{heading}</h2>
	<div class="reveal-warning" role="alert">
		<strong>{warningStrong}</strong>
		{warning}
	</div>
	<div class="key-reveal">
		<code class="key-value" data-testid={testId}>{secret}</code>
		<button type="button" class="btn-primary copy-btn" onclick={copy}>
			{copied ? copiedLabel : copyLabel}
		</button>
	</div>
	{#if meta.length > 0}
		<dl class="reveal-meta">
			{#each meta as row (row.label)}
				<div>
					<dt>{row.label}</dt>
					<dd class:mono={row.mono}>{row.value}</dd>
				</div>
			{/each}
		</dl>
	{/if}
	{#if note}
		{@render note()}
	{/if}
	<div class="modal-footer">
		<button type="button" class="btn-primary" onclick={onclose}>{doneLabel}</button>
	</div>
</Modal>

<style>
	.reveal-warning {
		background: color-mix(in srgb, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
		color: var(--warning);
		border-radius: 8px;
		padding: 0.75rem 1rem;
		font-size: 0.85rem;
		margin-bottom: 1rem;
	}

	.key-reveal {
		display: flex;
		align-items: stretch;
		gap: 0.5rem;
		margin-bottom: 1rem;
	}

	.key-value {
		flex: 1;
		background: var(--surface-2);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.6rem 0.75rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
		word-break: break-all;
		user-select: all;
	}

	.copy-btn {
		white-space: nowrap;
	}

	.reveal-meta {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
		gap: 0.75rem;
		margin: 0 0 0.5rem;
	}

	.reveal-meta dt {
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.reveal-meta dd {
		margin: 0.15rem 0 0;
		font-weight: 600;
	}

	.reveal-meta dd.mono {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.85rem;
	}
</style>
