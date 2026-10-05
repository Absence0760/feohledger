<script lang="ts">
	import type {
		ChatThread,
		ChatMessage,
		ChatTemplate,
		PortalChatThread,
		PortalChatMessage,
	} from '#lib/types/supplierChat.ts';
	import type { ChatMentionCandidate } from '#lib/api/supplierChat.ts';
	import RowAction from '#lib/components/ui/RowAction.svelte';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { formatDate } from '#lib/utils/time.ts';

	// A message is one of the two surface shapes. Reads are guarded so the
	// component works against either: AP messages carry author_user_id +
	// mention_user_ids; portal messages don't.
	type AnyMessage = ChatMessage | PortalChatMessage;

	type Props = {
		thread: ChatThread | PortalChatThread | null;
		surface: 'ap' | 'vendor';
		currentUserId?: string;
		/** Colleagues the AP composer may @mention. Narrow ON PURPOSE — see
		 *  `ChatMentionCandidate`. The vendor surface passes none and the
		 *  picker is `{#if isAp}`-gated besides, so a supplier can never be
		 *  handed an employee roster. */
		members?: ChatMentionCandidate[];
		templates?: ChatTemplate[];
		onsend: (body: string, mentionUserIds: string[], file?: File) => Promise<void>;
		onresolve?: () => Promise<void>;
		onreopen?: () => Promise<void>;
		loading?: boolean;
		/** Caller-supplied attachment download. Receives the on-wire file_url. */
		ondownload?: (fileUrl: string, filename: string) => Promise<void> | void;
	};

	let {
		thread,
		surface,
		currentUserId,
		members = [],
		templates = [],
		onsend,
		onresolve,
		onreopen,
		loading = false,
		ondownload,
	}: Props = $props();

	let body = $state('');
	let file = $state<File | null>(null);
	let fileInput: HTMLInputElement | undefined = $state();
	let sending = $state(false);
	let busyStatus = $state(false);

	// The role whose own messages right-align on THIS surface.
	let ownRole = $derived(surface === 'ap' ? 'ap_team' : 'supplier');
	let isAp = $derived(surface === 'ap');

	let messages = $derived((thread?.messages ?? []) as AnyMessage[]);
	let status = $derived(thread?.status ?? 'open');

	// --- @mention autocomplete (AP only). Mirrors the InvoiceModal approver
	// filter: only active members, excluding the current user. ---
	let mentionQuery = $state('');
	let mentionOpen = $state(false);
	let selectedMentions = $state<ChatMentionCandidate[]>([]);

	let mentionCandidates = $derived(
		!isAp
			? []
			: members
					.filter((m) => m.is_active && m.id !== currentUserId)
					.filter((m) => !selectedMentions.some((s) => s.id === m.id))
					// Name only. The source carries no email — matching on one
					// would mean fetching one, and an address is not something a
					// chat composer needs to hold.
					.filter((m) => {
						const q = mentionQuery.trim().toLowerCase();
						return !q || m.full_name.toLowerCase().includes(q);
					}),
	);

	// Resolve a mentioned id to a display name for the @mention highlight.
	let memberById = $derived.by(() => {
		const map: Record<string, string> = {};
		for (const m of members) map[m.id] = m.full_name;
		return map;
	});

	function addMention(m: ChatMentionCandidate) {
		selectedMentions = [...selectedMentions, m];
		mentionQuery = '';
		mentionOpen = false;
	}

	function removeMention(id: string) {
		selectedMentions = selectedMentions.filter((m) => m.id !== id);
	}

	function applyTemplate(tpl: ChatTemplate) {
		// Insert the canned body; if the composer already has text, keep it.
		body = body.trim() ? `${body.trim()}\n\n${tpl.body}` : tpl.body;
	}

	function onFileChange(e: Event) {
		const input = e.target as HTMLInputElement;
		file = input.files?.[0] ?? null;
	}

	function clearFile() {
		file = null;
		if (fileInput) fileInput.value = '';
	}

	let canSend = $derived(!sending && (!!body.trim() || !!file));

	async function send() {
		if (!canSend) return;
		sending = true;
		try {
			const mentionIds = isAp ? selectedMentions.map((m) => m.id) : [];
			await onsend(body.trim(), mentionIds, file ?? undefined);
			body = '';
			selectedMentions = [];
			clearFile();
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Could not send message', 'error');
		} finally {
			sending = false;
		}
	}

	async function resolve() {
		if (!onresolve) return;
		busyStatus = true;
		try {
			await onresolve();
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Could not resolve thread', 'error');
		} finally {
			busyStatus = false;
		}
	}

	async function reopen() {
		if (!onreopen) return;
		busyStatus = true;
		try {
			await onreopen();
		} catch (err) {
			toast(err instanceof Error ? err.message : 'Could not reopen thread', 'error');
		} finally {
			busyStatus = false;
		}
	}

	function isOwn(msg: AnyMessage): boolean {
		return msg.author_role === ownRole;
	}

	function relativeTime(iso: string): string {
		if (!iso) return '';
		const then = new Date(iso).getTime();
		if (Number.isNaN(then)) return '';
		const diff = Date.now() - then;
		const sec = Math.round(diff / 1000);
		if (sec < 60) return 'just now';
		const min = Math.round(sec / 60);
		if (min < 60) return `${min}m ago`;
		const hr = Math.round(min / 60);
		if (hr < 24) return `${hr}h ago`;
		const day = Math.round(hr / 24);
		if (day < 7) return `${day}d ago`;
		return formatDate(iso, '', { year: 'numeric', month: 'numeric', day: 'numeric' });
	}

	function mentionIdsOf(msg: AnyMessage): string[] {
		return 'mention_user_ids' in msg ? msg.mention_user_ids : [];
	}

	// Split a body into plain-text segments so @mention tokens that match a
	// known member render highlighted — WITHOUT ever using {@html}. Each
	// segment is bound as text, so user content can never inject markup.
	type Segment = { text: string; mention: boolean };
	function segments(msg: AnyMessage): Segment[] {
		const ids = mentionIdsOf(msg);
		if (ids.length === 0 || !isAp) return [{ text: msg.body, mention: false }];
		const names = ids.map((id) => memberById[id]).filter((n): n is string => !!n);
		if (names.length === 0) return [{ text: msg.body, mention: false }];
		// Build a regex of "@Name" tokens to highlight (escaped).
		const escaped = names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
		const re = new RegExp(`(@(?:${escaped.join('|')}))`, 'g');
		const out: Segment[] = [];
		let last = 0;
		let m: RegExpExecArray | null;
		while ((m = re.exec(msg.body)) !== null) {
			if (m.index > last) out.push({ text: msg.body.slice(last, m.index), mention: false });
			out.push({ text: m[0], mention: true });
			last = m.index + m[0].length;
		}
		if (last < msg.body.length) out.push({ text: msg.body.slice(last), mention: false });
		return out.length ? out : [{ text: msg.body, mention: false }];
	}

	async function downloadAttachment(fileUrl: string, filename: string) {
		if (ondownload) {
			try {
				await ondownload(fileUrl, filename);
			} catch (err) {
				toast(err instanceof Error ? err.message : 'Download failed', 'error');
			}
		}
	}
</script>

<section class="chat" data-testid="supplier-chat" aria-label="Supplier chat">
	<div class="chat-head">
		<span class="chat-title">Supplier Chat</span>
		<span class="chat-status-pill {status}" data-testid="chat-status">{status}</span>
		{#if isAp}
			<div class="chat-status-actions">
				{#if status === 'open'}
					{#if onresolve}
						<RowAction
							variant="success"
							disabled={busyStatus || !thread?.messages?.length}
							onclick={resolve}
							ariaLabel="Resolve chat thread"
						>
							{busyStatus ? 'Resolving…' : 'Resolve'}
						</RowAction>
					{/if}
				{:else if onreopen}
					<RowAction
						disabled={busyStatus}
						onclick={reopen}
						ariaLabel="Reopen chat thread"
					>
						{busyStatus ? 'Reopening…' : 'Reopen'}
					</RowAction>
				{/if}
			</div>
		{/if}
	</div>

	<div class="chat-list" data-testid="chat-messages">
		{#if loading && messages.length === 0}
			<p class="chat-empty">Loading…</p>
		{:else if messages.length === 0}
			<p class="chat-empty">No messages yet. Start the conversation below.</p>
		{:else}
			{#each messages as msg (msg.id)}
				<div
					class="chat-msg"
					class:own={isOwn(msg)}
					data-testid="chat-msg"
					data-own={isOwn(msg)}
					data-role={msg.author_role}
				>
					<div class="chat-bubble">
						<div class="chat-meta">
							<span class="chat-author">{msg.author_name ?? 'Unknown'}</span>
							<span class="chat-time">{relativeTime(msg.created_at)}</span>
						</div>
						<!-- Plain text only — never {@html}. @mention tokens are
						     highlighted via segmented text spans (XSS-safe). -->
						<p class="chat-body">{#each segments(msg) as seg}{#if seg.mention}<span
										class="chat-mention">{seg.text}</span>{:else}{seg.text}{/if}{/each}</p>
						{#if msg.attachments.length > 0}
							<div class="chat-attachments">
								{#each msg.attachments as att}
									<button
										type="button"
										class="chat-chip"
										onclick={() => downloadAttachment(att.file_url, att.filename)}
									>
										<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
										<span class="chat-chip-name">{att.filename}</span>
									</button>
								{/each}
							</div>
						{/if}
					</div>
				</div>
			{/each}
		{/if}
	</div>

	{#if isAp && status === 'resolved'}
		<p class="chat-resolved-note">This thread is resolved. Reopen it to post a new message.</p>
	{:else}
		<div class="chat-composer">
			{#if isAp && templates.length > 0}
				<div class="chat-templates">
					<span class="chat-templates-label">Templates:</span>
					{#each templates as tpl}
						<button
							type="button"
							class="chat-template-btn"
							onclick={() => applyTemplate(tpl)}
							data-testid="chat-template"
						>
							{tpl.label}
						</button>
					{/each}
				</div>
			{/if}

			{#if isAp && selectedMentions.length > 0}
				<div class="chat-mentions-selected">
					{#each selectedMentions as m (m.id)}
						<span class="chat-mention-tag">
							@{m.full_name}
							<button
								type="button"
								class="chat-mention-remove"
								aria-label={`Remove mention ${m.full_name}`}
								onclick={() => removeMention(m.id)}>&times;</button
							>
						</span>
					{/each}
				</div>
			{/if}

			<textarea
				class="chat-input"
				placeholder="Write a message…"
				aria-label="Write a message"
				rows="2"
				bind:value={body}
				data-testid="chat-input"
			></textarea>

			<div class="chat-composer-row">
				{#if isAp}
					<div class="chat-mention-picker">
						<input
							type="text"
							class="chat-mention-input"
							placeholder="@mention…"
							aria-label="Mention a teammate"
							bind:value={mentionQuery}
							onfocus={() => (mentionOpen = true)}
							oninput={() => (mentionOpen = true)}
							onblur={() => setTimeout(() => (mentionOpen = false), 150)}
							data-testid="chat-mention-input"
						/>
						{#if mentionOpen && mentionCandidates.length > 0}
							<ul class="chat-mention-list" data-testid="chat-mention-list">
								{#each mentionCandidates.slice(0, 6) as m (m.id)}
									<li>
										<button
											type="button"
											class="chat-mention-option"
											onclick={() => addMention(m)}
										>
											{m.full_name}
										</button>
									</li>
								{/each}
							</ul>
						{/if}
					</div>
				{/if}

				<label class="chat-file-btn">
					<input
						type="file"
						bind:this={fileInput}
						onchange={onFileChange}
						data-testid="chat-file"
					/>
					{file ? 'Change file' : 'Attach'}
				</label>
				{#if file}
					<span class="chat-file-name">
						{file.name}
						<button type="button" class="chat-file-clear" aria-label="Remove attachment" onclick={clearFile}>&times;</button>
					</span>
				{/if}

				<button
					type="button"
					class="chat-send"
					disabled={!canSend}
					onclick={send}
					data-testid="chat-send"
				>
					{sending ? 'Sending…' : 'Send'}
				</button>
			</div>
		</div>
	{/if}
</section>

<style>
	.chat {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}

	.chat-head {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.chat-title {
		font-size: 0.95rem;
		font-weight: 600;
		color: var(--text);
	}

	.chat-status-actions {
		margin-left: auto;
	}

	/* Not `<Badge>`: the pill carries `data-testid="chat-status"`, which the
	   supplier-chat e2e reads the thread state off — `<Badge>` takes a tone, a
	   variant class and a title, deliberately not arbitrary attributes, so
	   converting would mean either wrapping it in a second element or widening
	   the primitive's API for one caller. It keeps its own smaller metrics
	   (0.7rem, sitting inline in the thread header) and takes its colour from
	   the palette pairs. */
	.chat-status-pill {
		display: inline-block;
		padding: 2px 9px;
		border-radius: 12px;
		font-size: 0.7rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.chat-status-pill.open {
		background: var(--accent-tint);
		color: var(--accent-on-tint);
	}

	.chat-status-pill.resolved {
		background: var(--success-tint);
		color: var(--success-on-tint);
	}

	.chat-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-height: 320px;
		overflow-y: auto;
		padding: 4px 2px;
	}

	.chat-empty {
		color: var(--text-muted);
		font-size: 0.85rem;
		margin: 8px 0;
		text-align: center;
	}

	.chat-msg {
		display: flex;
		justify-content: flex-start;
	}

	.chat-msg.own {
		justify-content: flex-end;
	}

	.chat-bubble {
		max-width: 80%;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 8px 12px;
	}

	.chat-msg.own .chat-bubble {
		background: rgba(99, 140, 255, 0.12);
		border-color: rgba(99, 140, 255, 0.3);
	}

	.chat-meta {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 3px;
	}

	.chat-author {
		font-size: 0.78rem;
		font-weight: 600;
		color: var(--text);
	}

	.chat-time {
		font-size: 0.7rem;
		color: var(--text-muted);
	}

	.chat-body {
		margin: 0;
		font-size: 0.88rem;
		line-height: 1.4;
		color: var(--text);
		white-space: pre-wrap;
		word-break: break-word;
	}

	.chat-mention {
		color: #638cff;
		font-weight: 600;
	}

	.chat-attachments {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 6px;
	}

	.chat-chip {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 3px 8px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.76rem;
		cursor: pointer;
		max-width: 200px;
	}

	.chat-chip:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.chat-chip-name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.chat-resolved-note {
		font-size: 0.82rem;
		color: var(--text-muted);
		font-style: italic;
		margin: 4px 0;
	}

	.chat-composer {
		display: flex;
		flex-direction: column;
		gap: 8px;
		border-top: 1px solid var(--border);
		padding-top: 10px;
	}

	.chat-templates {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}

	.chat-templates-label {
		font-size: 0.74rem;
		color: var(--text-muted);
		text-transform: uppercase;
		letter-spacing: 0.03em;
	}

	.chat-template-btn {
		padding: 3px 10px;
		border-radius: 12px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.76rem;
		cursor: pointer;
		font-family: inherit;
	}

	.chat-template-btn:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.chat-mentions-selected {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	/* Not `<Badge>`: a composer @mention token that wraps its own remove
	   button — a flex container with a child control, not a text pill. Colour
	   comes from the palette pair. */
	.chat-mention-tag {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 2px 8px;
		border-radius: 12px;
		background: var(--accent-tint);
		color: var(--accent-on-tint);
		font-size: 0.76rem;
	}

	.chat-mention-remove,
	.chat-file-clear {
		background: none;
		border: none;
		color: inherit;
		cursor: pointer;
		font-size: 0.95rem;
		line-height: 1;
		padding: 0;
	}

	.chat-input {
		width: 100%;
		box-sizing: border-box;
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 8px 10px;
		font-size: 0.88rem;
		color: var(--text);
		font-family: inherit;
		resize: vertical;
	}

	.chat-input:focus {
		outline: none;
		border-color: var(--accent);
		box-shadow: 0 0 0 2px rgba(99, 140, 255, 0.15);
	}

	.chat-composer-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}

	.chat-mention-picker {
		position: relative;
	}

	.chat-mention-input {
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 6px;
		padding: 6px 9px;
		font-size: 0.82rem;
		color: var(--text);
		font-family: inherit;
		width: 140px;
	}

	.chat-mention-list {
		position: absolute;
		bottom: calc(100% + 4px);
		left: 0;
		min-width: 200px;
		max-height: 180px;
		overflow-y: auto;
		list-style: none;
		margin: 0;
		padding: 4px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 6px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		z-index: 5;
	}

	.chat-mention-option {
		display: flex;
		flex-direction: column;
		width: 100%;
		text-align: left;
		background: none;
		border: none;
		padding: 6px 8px;
		border-radius: 4px;
		cursor: pointer;
		color: var(--text);
		font-family: inherit;
		font-size: 0.84rem;
	}

	.chat-mention-option:hover {
		background: var(--bg);
	}

	.chat-file-btn {
		padding: 6px 12px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text-muted);
		font-size: 0.82rem;
		cursor: pointer;
	}

	.chat-file-btn:hover {
		border-color: var(--accent);
		color: var(--accent);
	}

	.chat-file-btn input {
		display: none;
	}

	.chat-file-name {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 0.78rem;
		color: var(--text-muted);
		max-width: 180px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.chat-send {
		margin-left: auto;
		padding: 7px 18px;
		border-radius: 6px;
		border: none;
		background: var(--accent-strong);
		color: #fff;
		font-size: 0.84rem;
		font-weight: 600;
		cursor: pointer;
		font-family: inherit;
	}

	.chat-send:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
</style>
