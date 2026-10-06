<script lang="ts">
	import Modal from '#lib/components/ui/Modal.svelte';
	import { auth, type StepUpOperation, type StepUpProof } from '#lib/stores/auth.svelte.ts';
	import { authErrorMessage } from '#lib/api/authRefusals.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { isCompleteStepUpCode } from '#lib/utils/stepUpCode.ts';
	import { isWebAuthnSupported } from '#lib/webauthn.ts';

	/**
	 * Collect a SECOND-FACTOR proof for a sensitive action — a current
	 * authenticator code, or a passkey ceremony — and hand it to the caller,
	 * which resends its request with the proof attached.
	 *
	 * The backend half is `api/auth.require_sensitive_step_up`, which accepts
	 * only those two proofs and never the password, in every tenant. So this
	 * prompt never asks for one: unlike `/profile`'s factor-change step-up
	 * (where the password IS a proof outside an SSO-only tenant), there is no
	 * tenant in which a password field here could succeed.
	 *
	 * The passkey ceremony runs here, bound to `operation`: the server mints the
	 * challenge for that operation and the mutating call verifies it under the
	 * same one, so a proof collected for one action cannot authorize another.
	 *
	 * The caller owns the verdict. `error` is what it shows when the server
	 * refused the proof it sent (already localized — `authErrorMessage`); a
	 * ceremony that fails before anything is sent is reported here.
	 */
	type Props = {
		open: boolean;
		operation: StepUpOperation;
		/** Why this action needs a second factor — shown under the title. */
		hint: string;
		/** The caller is resending with a proof; disables the controls. */
		busy?: boolean;
		/** The server's localized refusal of the last proof, if any. */
		error?: string | null;
		onproof: (proof: StepUpProof) => void;
		oncancel: () => void;
	};

	let { open, operation, hint, busy = false, error = null, onproof, oncancel }: Props = $props();

	let code = $state('');
	let ceremonyError = $state<string | null>(null);
	let ceremonyRunning = $state(false);

	// A fresh prompt starts empty: a code typed for an earlier attempt is stale
	// (single-use on the server) and an old ceremony error no longer applies.
	$effect(() => {
		if (open) {
			code = '';
			ceremonyError = null;
		}
	});

	const working = $derived(busy || ceremonyRunning);
	const shownError = $derived(ceremonyError ?? error);
	const passkeyAvailable = isWebAuthnSupported();

	function submitCode() {
		if (working || !isCompleteStepUpCode(code)) return;
		ceremonyError = null;
		onproof({ code });
	}

	async function usePasskey() {
		if (working) return;
		ceremonyError = null;
		ceremonyRunning = true;
		try {
			const proof = await auth.passkeyStepUp(operation);
			onproof(proof);
		} catch (err) {
			ceremonyError = authErrorMessage(err, m, 'stepUpPrompt.failed');
		} finally {
			ceremonyRunning = false;
		}
	}
</script>

<Modal {open} ariaLabel={m('stepUpPrompt.title')} width="sm" onclose={() => !working && oncancel()}>
	<h2>{m('stepUpPrompt.title')}</h2>
	<p class="modal-hint">{hint}</p>
	<form
		onsubmit={(e) => {
			e.preventDefault();
			submitCode();
		}}
	>
		<label>
			<span>{m('stepUpPrompt.codeLabel')}</span>
			<input
				type="text"
				inputmode="numeric"
				pattern="[0-9]*"
				maxlength="8"
				autocomplete="one-time-code"
				data-testid="step-up-code"
				bind:value={code}
				disabled={working}
			/>
		</label>
		{#if shownError}
			<p class="step-up-error" role="alert" data-testid="step-up-error">{shownError}</p>
		{/if}
		{#if passkeyAvailable}
			<button type="button" class="btn-cancel passkey" onclick={usePasskey} disabled={working}>
				{m('stepUpPrompt.usePasskey')}
			</button>
		{/if}
		<div class="modal-footer">
			<button type="button" class="btn-cancel" onclick={oncancel} disabled={working}>
				{m('stepUpPrompt.cancel')}
			</button>
			<button type="submit" class="btn-primary" disabled={working || !isCompleteStepUpCode(code)}>
				{working ? m('stepUpPrompt.confirming') : m('stepUpPrompt.confirm')}
			</button>
		</div>
	</form>
</Modal>

<style>
	.step-up-error {
		margin: 0;
		color: var(--danger);
		font-size: 0.85rem;
	}

	.passkey {
		align-self: flex-start;
	}
</style>
