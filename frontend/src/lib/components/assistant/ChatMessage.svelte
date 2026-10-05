<script lang="ts">
	import ToolResultView from '#lib/components/assistant/ToolResultView.svelte';
	import { m } from '#lib/i18n/store.svelte.ts';
	import type { UiMessage } from '#lib/types/assistant.ts';

	let { message }: { message: UiMessage } = $props();

	let isUser = $derived(message.role === 'user');
</script>

<div class="msg" class:user={isUser} class:assistant={!isUser}>
	<div class="msg-role">{isUser ? m('assistant.role.you') : m('assistant.role.assistant')}</div>
	<div class="msg-bubble">
		<!-- Tool results render before the prose so charts/tables appear as
		     they stream in, ahead of the narrated answer. -->
		{#each message.tools as inv, i (i)}
			<ToolResultView invocation={inv} />
		{/each}

		{#if message.error}
			<p class="msg-error" role="alert">{message.error}</p>
		{:else if message.content}
			<!-- Plain-text only — never {@html}. Whitespace preserved for the
			     token-by-token streamed answer. -->
			<p class="msg-text">{message.content}</p>
		{/if}

		{#if message.streaming && !message.content && message.tools.length === 0}
			<span class="typing" role="img" aria-label={m('assistant.thinkingAria')}>
				<span aria-hidden="true"></span><span aria-hidden="true"></span><span aria-hidden="true"
				></span>
			</span>
		{/if}
	</div>
</div>

<style>
	.msg {
		display: flex;
		flex-direction: column;
		gap: 4px;
		max-width: 760px;
	}
	.msg.user {
		align-self: flex-end;
		align-items: flex-end;
	}
	.msg.assistant {
		align-self: flex-start;
		align-items: flex-start;
		width: 100%;
	}
	.msg-role {
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
	}
	.msg-bubble {
		border-radius: 12px;
		padding: 10px 14px;
		font-size: 0.92rem;
		line-height: 1.5;
	}
	.msg.user .msg-bubble {
		background: var(--accent-strong);
		color: #fff;
	}
	.msg.assistant .msg-bubble {
		background: rgba(128, 128, 128, 0.08);
		color: var(--text);
		width: 100%;
		box-sizing: border-box;
	}
	.msg-text {
		margin: 0;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.msg-error {
		margin: 0;
		color: var(--danger);
		font-size: 0.88rem;
	}
	.typing {
		display: inline-flex;
		gap: 4px;
		align-items: center;
		height: 18px;
	}
	.typing span {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--text-muted);
		animation: blink 1.2s infinite ease-in-out both;
	}
	.typing span:nth-child(2) {
		animation-delay: 0.2s;
	}
	.typing span:nth-child(3) {
		animation-delay: 0.4s;
	}
	@keyframes blink {
		0%,
		80%,
		100% {
			opacity: 0.3;
		}
		40% {
			opacity: 1;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.typing span {
			animation: none;
		}
	}
</style>
