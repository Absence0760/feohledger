<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { api, streamCashFlowCopilot, ApiError, budgetErrorOf, type AssistantBudgetError } from '#lib/api.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import HelpTip from '#lib/components/help/HelpTip.svelte';
	import UsageMeter from '#lib/components/assistant/UsageMeter.svelte';
	import ChatLayout from '#lib/components/assistant/ChatLayout.svelte';
	import ExamplePrompts from '#lib/components/assistant/ExamplePrompts.svelte';
	import CopilotChatMessage from '#lib/components/cash-flow/CopilotChatMessage.svelte';
	import SavedPlansPanel from '#lib/components/cash-flow/SavedPlansPanel.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { getActiveFormatLocale } from '#lib/i18n/formatLocale.ts';
	import type { ChatResponse, ToolInvocation, UiMessage, UsageResponse } from '#lib/types/assistant.ts';

	// Cash-flow copilot starter prompts. The first two route to `get_cash_position`
	// (running-balance chart); the third exercises the discount optimizer; the
	// fourth routes to the Phase 2 `propose_payment_plan` tool (plan card).
	// Each is a button label AND the message it sends, so it follows the active
	// locale like the rest of the page (they were English literals in every
	// locale); the copilot answers in the language it is asked in.
	const examples = $derived([
		m('cashFlow.example.runLow'),
		m('cashFlow.example.position'),
		m('cashFlow.example.discounts'),
		m('cashFlow.example.plan')
	]);

	let messages = $state<UiMessage[]>([]);
	let input = $state('');
	let busy = $state(false);
	let conversationId = $state<string | null>(null);
	let usage = $state<UsageResponse | null>(null);
	let budgetNotice = $state<string | null>(null);
	let scrollEl = $state<HTMLDivElement | null>(null);

	// Consolidated (whole-group) mode. A treasury question is usually a group
	// question, so the copilot can answer across every entity without the user
	// having to clear the sidebar's entity selector — the same posture the
	// projected-shortfall alert sweep already takes. It rides the request as a
	// query flag; a plan proposed in a consolidated turn carries the
	// consolidated plan_id, which is what the save / enact routes resolve
	// against.
	let consolidated = $state(false);

	let isEmpty = $derived(messages.length === 0);

	// Bubbles carry a stable client-side id (see `UiMessage.id`). A turn in
	// flight resolves its own placeholder through `assistantMessage()`, never
	// through a captured array index — `messages` can be REPLACED wholesale
	// while the turn is streaming, and an index into the old array then
	// addresses an unrelated historical message.
	let nextMessageId = 0;
	function makeMessage(msg: Omit<UiMessage, 'id'>): UiMessage {
		nextMessageId += 1;
		return { ...msg, id: `ui-${nextMessageId}` };
	}

	/** The in-progress bubble, or null once it is gone (the array was replaced,
	 *  or a budget error dropped it). A null means the turn's result has
	 *  nowhere to land and is discarded — the one safe outcome. */
	function assistantMessage(id: string): UiMessage | null {
		return messages.find((msg) => msg.id === id) ?? null;
	}

	/** Drop the empty in-progress bubble, by identity. */
	function dropMessage(id: string) {
		const idx = messages.findIndex((msg) => msg.id === id);
		if (idx !== -1) messages.splice(idx, 1);
	}

	async function loadUsage() {
		try {
			// The copilot shares the assistant orchestrator's control-plane token
			// budget, so the assistant usage endpoint reports the same figures.
			usage = await api.get<UsageResponse>('/api/assistant/usage');
		} catch {
			usage = null; // advisory — a failure here must not block chatting.
		}
	}

	onMount(() => {
		void loadUsage();
	});

	async function scrollToBottom() {
		await tick();
		if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
	}

	function newChat() {
		conversationId = null;
		messages = [];
		budgetNotice = null;
		input = '';
	}

	function pickPrompt(prompt: string) {
		input = prompt;
		void send();
	}

	/** Apply the authoritative `done`/`/copilot` payload to the in-progress
	 *  assistant message and refresh side state. */
	function applyFinal(payload: ChatResponse, assistantId: string) {
		conversationId = payload.conversation_id;
		const msg = assistantMessage(assistantId);
		if (msg) {
			msg.content = payload.answer;
			msg.tools = (payload.tool_invocations as ToolInvocation[]) ?? msg.tools;
			msg.streaming = false;
		}
	}

	/** Non-streaming fallback — used when the stream endpoint is unavailable
	 *  (404 during local dev) or fails mid-flight before any content arrived. */
	async function fallbackChat(message: string, assistantId: string) {
		// Same consolidated scope as the streaming call — the two paths must
		// answer the same question, or a stream failure would silently switch
		// the user from a group view to one entity's.
		const path = consolidated
			? '/api/cash-flow/copilot?consolidated=true'
			: '/api/cash-flow/copilot';
		const payload = await api.post<ChatResponse>(path, {
			message,
			conversation_id: conversationId ?? undefined
		});
		applyFinal(payload, assistantId);
	}

	/** A token count in the in-app locale. A bare `toLocaleString()` reads the
	 *  BROWSER's locale, so a German UI on an English browser printed
	 *  "12,000" inside a German sentence. */
	function formatCount(n: number): string {
		return new Intl.NumberFormat(getActiveFormatLocale()).format(n);
	}

	/** The over-budget notice, in place of the empty in-progress bubble. */
	function showBudgetNotice(err: AssistantBudgetError, assistantId: string) {
		budgetNotice = m('assistant.budget.reached', {
			used: formatCount(err.used),
			budget: formatCount(err.budget)
		});
		dropMessage(assistantId);
	}

	async function send() {
		const message = input.trim();
		if (!message || busy) return;
		busy = true;
		budgetNotice = null;
		input = '';

		messages.push(makeMessage({ role: 'user', content: message, tools: [] }));
		const placeholder = makeMessage({
			role: 'assistant',
			content: '',
			tools: [],
			streaming: true
		});
		const assistantId = placeholder.id;
		messages.push(placeholder);
		await scrollToBottom();

		let sawContent = false;
		try {
			await streamCashFlowCopilot(
				{ message, conversation_id: conversationId ?? undefined },
				{
					onTool: (frame) => {
						const msg = assistantMessage(assistantId);
						if (!msg) return;
						msg.tools = [
							...msg.tools,
							{
								tool: frame.tool,
								args: frame.args ?? {},
								result: frame.result ?? null,
								error: frame.error ?? null
							}
						];
						sawContent = true;
						void scrollToBottom();
					},
					onDelta: (text) => {
						const msg = assistantMessage(assistantId);
						if (!msg) return;
						msg.content += text;
						sawContent = true;
						void scrollToBottom();
					},
					onDone: (payload) => {
						applyFinal(payload as ChatResponse, assistantId);
						sawContent = true;
					},
					onError: (frame) => {
						const msg = assistantMessage(assistantId);
						if (msg && !sawContent) {
							msg.error = frame.detail || m('assistant.error.generic');
							msg.streaming = false;
						}
					}
				},
				undefined,
				{ consolidated }
			);
		} catch (err) {
			const budgetErr = budgetErrorOf(err);
			if (budgetErr) {
				showBudgetNotice(budgetErr, assistantId);
			} else if (err instanceof ApiError) {
				// A refusal from the stream endpoint itself — the shared rate
				// limiter's `rate_limited`, already localized. `/copilot` would
				// refuse the same turn, so state it instead of falling back.
				const msg = assistantMessage(assistantId);
				if (msg) {
					msg.error = err.message;
					msg.streaming = false;
				}
			} else if (!sawContent) {
				// Stream unavailable / failed before any content → non-streaming
				// fallback so the page works against `/copilot` alone.
				try {
					await fallbackChat(message, assistantId);
				} catch (fallbackErr) {
					const fallbackBudget = budgetErrorOf(fallbackErr);
					if (fallbackBudget) {
						showBudgetNotice(fallbackBudget, assistantId);
					} else {
						const msg = assistantMessage(assistantId);
						const detail =
							fallbackErr instanceof Error
								? fallbackErr.message
								: m('assistant.error.somethingWrong');
						if (msg) {
							msg.error = m('assistant.error.couldNotAnswer', { detail });
							msg.streaming = false;
						}
					}
				}
			}
		} finally {
			const msg = assistantMessage(assistantId);
			if (msg) msg.streaming = false;
			busy = false;
			void loadUsage();
			void scrollToBottom();
		}
	}

	function onKeydown(e: KeyboardEvent) {
		// Enter sends; Shift+Enter inserts a newline.
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			void send();
		}
	}
</script>

<PageHeader title={m('cashFlow.title')}>
	{#snippet actions()}
		<button type="button" class="btn-secondary" onclick={newChat} disabled={busy}
			>{m('cashFlow.newChat')}</button
		>
	{/snippet}

	<ChatLayout railLabel={m('cashFlow.rail.toggle')} railId="cash-flow-rail" {busy} bind:scrollEl>
		{#snippet rail()}
			<UsageMeter {usage} />

			<div class="help-row">
				<label class="rail-toggle">
					<input type="checkbox" bind:checked={consolidated} disabled={busy} />
					<span>{m('cashFlow.consolidated.label')}</span>
				</label>
				<HelpTip term="entity" />
			</div>
			<p class="rail-hint">{m('cashFlow.consolidated.hint')}</p>

			<SavedPlansPanel {consolidated} />

			<div class="rail-head">{m('cashFlow.tips.heading')}</div>
			<p class="rail-hint">{m('cashFlow.tips.body')}</p>
		{/snippet}

		{#snippet log()}
			{#if isEmpty}
				<ExamplePrompts
					prompts={examples}
					heading={m('cashFlow.empty.heading')}
					sub={m('cashFlow.empty.sub')}
					onpick={pickPrompt}
				/>
			{:else}
				<div class="msg-stream">
					{#each messages as message (message.id)}
						<CopilotChatMessage {message} />
					{/each}
				</div>
			{/if}
		{/snippet}

		{#snippet footer()}
			{#if budgetNotice}
				<div class="budget-notice" role="alert">{budgetNotice}</div>
			{/if}

			<form
				class="composer"
				onsubmit={(e) => {
					e.preventDefault();
					void send();
				}}
			>
				<textarea
					bind:value={input}
					onkeydown={onKeydown}
					placeholder={m('cashFlow.composer.placeholder')}
					rows="1"
					aria-label={m('cashFlow.composer.ariaLabel')}
					disabled={busy}
				></textarea>
				<button type="submit" class="btn-primary" disabled={busy || input.trim().length === 0}>
					{busy ? m('cashFlow.thinking') : m('cashFlow.send')}
				</button>
			</form>
		{/snippet}
	</ChatLayout>
</PageHeader>

<style>


	.rail-head {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		padding-top: 4px;
		border-top: 1px solid var(--border);
	}

	.rail-hint {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0;
		line-height: 1.5;
	}

	.rail-toggle {
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.85rem;
		color: var(--text);
		cursor: pointer;
	}

	.rail-toggle input:disabled {
		cursor: not-allowed;
	}


	.msg-stream {
		display: flex;
		flex-direction: column;
		gap: 18px;
	}

	.budget-notice {
		margin: 0 16px;
		padding: 10px 14px;
		border-radius: 8px;
		background: color-mix(in srgb, var(--danger) 10%, transparent);
		border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
		color: var(--danger);
		font-size: 0.85rem;
	}

	.composer {
		display: flex;
		gap: 10px;
		align-items: flex-end;
		padding: 14px 16px;
		border-top: 1px solid var(--border);
	}

	.composer textarea {
		flex: 1 1 auto;
		resize: none;
		min-height: 42px;
		max-height: 160px;
		padding: 10px 14px;
		border-radius: 10px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.92rem;
		line-height: 1.4;
	}
	.composer textarea:focus {
		outline: none;
		border-color: var(--accent);
	}
	.btn-secondary {
		padding: 8px 14px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		font-family: inherit;
		font-size: 0.85rem;
		font-weight: 500;
		cursor: pointer;
	}

	.btn-secondary:hover:not(:disabled) {
		border-color: var(--accent);
		color: var(--accent);
	}
	.btn-secondary:disabled,
	.composer .btn-primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.composer .btn-primary {
		align-self: stretch;
		padding: 0 22px;
	}

</style>
