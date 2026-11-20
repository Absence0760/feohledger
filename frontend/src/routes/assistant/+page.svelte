<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { api, streamAssistantChat, ApiError, budgetErrorOf, type AssistantBudgetError } from '#lib/api.ts';
	import PageHeader from '#lib/components/ui/PageHeader.svelte';
	import ChatMessage from '#lib/components/assistant/ChatMessage.svelte';
	import ExamplePrompts from '#lib/components/assistant/ExamplePrompts.svelte';
	import UsageMeter from '#lib/components/assistant/UsageMeter.svelte';
	import ChatLayout from '#lib/components/assistant/ChatLayout.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import { formatCount } from '#lib/utils/count.ts';
	import type {
		ChatResponse,
		ConversationDetail,
		ConversationListResponse,
		ConversationSummary,
		ToolInvocation,
		UiMessage,
		UsageResponse
	} from '#lib/types/assistant.ts';

	let messages = $state<UiMessage[]>([]);
	let input = $state('');
	let busy = $state(false);
	let conversationId = $state<string | null>(null);
	let usage = $state<UsageResponse | null>(null);
	let conversations = $state<ConversationSummary[]>([]);
	let budgetNotice = $state<string | null>(null);
	let scrollEl = $state<HTMLDivElement | null>(null);

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
			usage = await api.get<UsageResponse>('/api/assistant/usage');
		} catch {
			// Usage is advisory — a failure here must not block chatting.
			usage = null;
		}
	}

	async function loadConversations() {
		try {
			const res = await api.get<ConversationListResponse>('/api/assistant/conversations');
			conversations = res.items;
		} catch {
			conversations = [];
		}
	}

	onMount(() => {
		void loadUsage();
		void loadConversations();
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

	async function openConversation(id: string) {
		if (busy) return;
		// Hold `busy` for the WHOLE load, not just guard on it. This GET replaces
		// `messages` wholesale, and the composer is disabled by `busy` — without
		// setting it, a send fired while the thread was still loading pushed its
		// user + placeholder bubbles into an array this response was about to
		// throw away. The dead guard above shows that was always the intent.
		busy = true;
		try {
			const detail = await api.get<ConversationDetail>(`/api/assistant/conversations/${id}`);
			conversationId = detail.conversation.id;
			messages = detail.messages
				.filter((msg) => msg.role === 'user' || msg.role === 'assistant')
				.map((msg) =>
					makeMessage({
						role: msg.role === 'user' ? 'user' : 'assistant',
						content: msg.content,
						tools: msg.tool_calls ?? [],
						streaming: false
					})
				);
			budgetNotice = null;
			await scrollToBottom();
		} catch {
			budgetNotice = m('assistant.error.loadConversation');
		} finally {
			busy = false;
		}
	}

	function pickPrompt(prompt: string) {
		input = prompt;
		void send();
	}

	/** Apply the authoritative `done`/`/chat` payload to the in-progress
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
		const payload = await api.post<ChatResponse>('/api/assistant/chat', {
			message,
			conversation_id: conversationId ?? undefined
		});
		applyFinal(payload, assistantId);
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
			await streamAssistantChat(
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
				}
			);
		} catch (err) {
			const budgetErr = budgetErrorOf(err);
			if (budgetErr) {
				showBudgetNotice(budgetErr, assistantId);
			} else if (err instanceof ApiError) {
				// A refusal from the stream endpoint itself — the shared rate
				// limiter's `rate_limited`, already localized. `/chat` would
				// refuse the same turn, so state it instead of falling back.
				const msg = assistantMessage(assistantId);
				if (msg) {
					msg.error = err.message;
					msg.streaming = false;
				}
			} else if (!sawContent) {
				// Stream unavailable / failed before any content → non-streaming
				// fallback so the page works against `/chat` alone.
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
			void loadConversations();
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

<PageHeader title={m('assistant.title')}>
	{#snippet actions()}
		<button type="button" class="btn-secondary" onclick={newChat} disabled={busy}>{m('assistant.newChat')}</button>
	{/snippet}

	<ChatLayout railLabel={m('assistant.rail.toggle')} railId="assistant-rail" {busy} bind:scrollEl>
		{#snippet rail()}
			<UsageMeter {usage} />
			<div class="convo-head">{m('assistant.recent')}</div>
			{#if conversations.length === 0}
				<p class="convo-empty">{m('assistant.noConversations')}</p>
			{:else}
				<ul class="convo-list">
					{#each conversations as c (c.id)}
						<li>
							<button
								type="button"
								class="convo-item"
								class:active={c.id === conversationId}
								aria-current={c.id === conversationId ? 'true' : undefined}
								onclick={() => openConversation(c.id)}
								disabled={busy}
							>
								<span class="convo-title">{c.title || m('assistant.untitledChat')}</span>
								<span class="convo-count">{m('assistant.msgCount', { count: c.message_count })}</span>
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		{/snippet}

		{#snippet log()}
			{#if isEmpty}
				<ExamplePrompts onpick={pickPrompt} />
			{:else}
				<div class="msg-stream">
					{#each messages as message (message.id)}
						<ChatMessage {message} />
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
					placeholder={m('assistant.composer.placeholder')}
					rows="1"
					aria-label={m('assistant.composer.ariaLabel')}
					disabled={busy}
				></textarea>
				<button type="submit" class="btn-primary" disabled={busy || input.trim().length === 0}>
					{busy ? m('assistant.thinking') : m('assistant.send')}
				</button>
			</form>
		{/snippet}
	</ChatLayout>
</PageHeader>

<style>
	.convo-head {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		padding-top: 4px;
		border-top: 1px solid var(--border);
	}
	.convo-empty {
		font-size: 0.82rem;
		color: var(--text-muted);
		margin: 0;
	}
	.convo-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}
	.convo-item {
		width: 100%;
		text-align: left;
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: 8px 10px;
		border-radius: 8px;
		border: 1px solid transparent;
		background: none;
		color: var(--text);
		cursor: pointer;
		font-family: inherit;
	}
	.convo-item:hover {
		background: color-mix(in srgb, var(--accent) 8%, transparent);
	}
	.convo-item.active {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 10%, transparent);
	}
	.convo-title {
		font-size: 0.84rem;
		font-weight: 500;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.convo-count {
		font-size: 0.72rem;
		color: var(--text-muted);
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
	.composer .btn-primary {
		align-self: stretch;
		padding: 0 22px;
	}

</style>
