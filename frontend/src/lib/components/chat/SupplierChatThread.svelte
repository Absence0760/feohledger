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
	import LoadError from '#lib/components/ui/LoadError.svelte';
	import Badge from '#lib/components/ui/Badge.svelte';
	import type { BadgeTone } from '#lib/components/ui/badgeTone.ts';
	import { toast } from '#lib/components/ui/Toast.svelte';
	import { formatDate, timeAgo } from '#lib/utils/time.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
	import LoadingState from '#lib/components/ui/LoadingState.svelte';
	import { apiErrorMessage } from '#lib/utils/apiError.ts';
	import type { MessageKey } from '#lib/i18n/messages.ts';

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
		/** The thread could not be read: a localized message, rendered in
		 *  place of the conversation. Without it a failed read has to pose as
		 *  an empty thread ("No messages yet"), which tells the reader nobody
		 *  has written — a claim the failed read never established. */
		error?: string;
		/** Re-read the thread. Pair with `error`: the failure offers Retry. */
		onretry?: () => void;
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
		error,
		onretry,
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
					.filter((mention) => mention.is_active && mention.id !== currentUserId)
					.filter((mention) => !selectedMentions.some((s) => s.id === mention.id))
					// Name only. The source carries no email — matching on one
					// would mean fetching one, and an address is not something a
					// chat composer needs to hold.
					.filter((mention) => {
						const q = mentionQuery.trim().toLowerCase();
						return !q || mention.full_name.toLowerCase().includes(q);
					}),
	);

	// Resolve a mentioned id to a display name for the @mention highlight.
	let memberById = $derived.by(() => {
		const map: Record<string, string> = {};
		for (const mention of members) map[mention.id] = mention.full_name;
		return map;
	});

	function addMention(mention: ChatMentionCandidate) {
		selectedMentions = [...selectedMentions, mention];
		mentionQuery = '';
		mentionOpen = false;
	}

	function removeMention(id: string) {
		selectedMentions = selectedMentions.filter((mention) => mention.id !== id);
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
			const mentionIds = isAp ? selectedMentions.map((mention) => mention.id) : [];
			await onsend(body.trim(), mentionIds, file ?? undefined);
			body = '';
			selectedMentions = [];
			clearFile();
		} catch (err) {
			toast(apiErrorMessage(err, m('supplierChat.error.send')), 'error');
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
			toast(apiErrorMessage(err, m('supplierChat.error.resolve')), 'error');
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
			toast(apiErrorMessage(err, m('supplierChat.error.reopen')), 'error');
		} finally {
			busyStatus = false;
		}
	}

	function isOwn(msg: AnyMessage): boolean {
		return msg.author_role === ownRole;
	}

	// The thread state's label. Keyed on the wire value; one the build does not
	// know renders as-is rather than vanishing (the pill's `data-status` still
	// carries the raw value for the e2e and for styling).
	const STATUS_LABEL_KEYS: Record<string, MessageKey> = {
		open: 'supplierChat.status.open',
		resolved: 'supplierChat.status.resolved',
	};
	let statusLabel = $derived(STATUS_LABEL_KEYS[status] ? m(STATUS_LABEL_KEYS[status]) : status);
	const STATUS_TONES: Record<string, BadgeTone> = { open: 'accent', resolved: 'success' };
	let statusTone = $derived<BadgeTone>(STATUS_TONES[status] ?? 'neutral');

	const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

	/** Relative within the week ("5 min. ago" / "vor 5 Min."), a localized
	 *  date after it — both through the shared locale-aware formatters, so the
	 *  language picker moves them with the rest of the page. */
	function relativeTime(iso: string): string {
		if (!iso) return '';
		const then = new Date(iso).getTime();
		if (Number.isNaN(then)) return '';
		if (Date.now() - then < WEEK_MS) return timeAgo(iso, '');
		return formatDate(iso, '');
	}

	/** The exact instant, for the time's `title` / `datetime`. */
	function exactTime(iso: string): string {
		return formatDate(iso, '', {
			year: 'numeric',
			month: 'short',
			day: 'numeric',
			hour: 'numeric',
			minute: '2-digit',
		});
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
		let match: RegExpExecArray | null;
		while ((match = re.exec(msg.body)) !== null) {
			if (match.index > last) out.push({ text: msg.body.slice(last, match.index), mention: false });
			out.push({ text: match[0], mention: true });
			last = match.index + match[0].length;
		}
		if (last < msg.body.length) out.push({ text: msg.body.slice(last), mention: false });
		return out.length ? out : [{ text: msg.body, mention: false }];
	}

	async function downloadAttachment(fileUrl: string, filename: string) {
		if (ondownload) {
			try {
				await ondownload(fileUrl, filename);
			} catch (err) {
				toast(apiErrorMessage(err, m('supplierChat.error.download')), 'error');
			}
		}
	}
</script>

<section class="chat" data-testid="supplier-chat" aria-label={m('supplierChat.region')}>
	<div class="chat-head">
		<span class="chat-title">{m('supplierChat.title')}</span>
		<!-- The wrapper carries the e2e hooks (`<Badge>` takes no arbitrary
		     attributes): the label is the text, the raw wire value is
		     `data-status`. -->
		<span class="chat-status" data-testid="chat-status" data-status={status}
			><Badge tone={statusTone} variant="chat-status-pill {status}">{statusLabel}</Badge></span
		>
		{#if isAp}
			<div class="chat-status-actions">
				{#if status === 'open'}
					{#if onresolve}
						<RowAction
							variant="success"
							disabled={busyStatus || !thread?.messages?.length}
							onclick={resolve}
							ariaLabel={busyStatus ? undefined : m('supplierChat.resolveAria')}
						>
							{busyStatus ? m('supplierChat.resolving') : m('supplierChat.resolve')}
						</RowAction>
					{/if}
				{:else if onreopen}
					<RowAction
						disabled={busyStatus}
						onclick={reopen}
						ariaLabel={busyStatus ? undefined : m('supplierChat.reopenAria')}
					>
						{busyStatus ? m('supplierChat.reopening') : m('supplierChat.reopen')}
					</RowAction>
				{/if}
			</div>
		{/if}
	</div>

	<div class="chat-list" data-testid="chat-messages">
		{#if error && !loading}
			{#if onretry}
				<LoadError message={error} {onretry} testId="chat-load-error" />
			{:else}
				<p class="chat-empty" role="alert" data-testid="chat-load-error">{error}</p>
			{/if}
		{:else if loading && messages.length === 0}
			<LoadingState />
		{:else if messages.length === 0}
			<p class="chat-empty">{m('supplierChat.empty')}</p>
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
							<span class="chat-author">{msg.author_name ?? m('supplierChat.unknownAuthor')}</span>
							<time class="chat-time" datetime={msg.created_at} title={exactTime(msg.created_at)}
								>{relativeTime(msg.created_at)}</time
							>
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

	{#if error && !loading}
		<!-- No composer over a thread that failed to load: replying blind to a
		     conversation the reader cannot see is not a flow to offer. Retry
		     first. -->
	{:else if isAp && status === 'resolved'}
		<p class="chat-resolved-note">{m('supplierChat.resolvedNote')}</p>
	{:else}
		<div class="chat-composer">
			{#if isAp && templates.length > 0}
				<div class="chat-templates">
					<span class="chat-templates-label">{m('supplierChat.templates')}</span>
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
					{#each selectedMentions as mention (mention.id)}
						<span class="chat-mention-tag">
							@{mention.full_name}
							<button
								type="button"
								class="chat-mention-remove"
								aria-label={m('supplierChat.removeMention', { name: mention.full_name })}
								onclick={() => removeMention(mention.id)}>&times;</button
							>
						</span>
					{/each}
				</div>
			{/if}

			<textarea
				class="chat-input"
				placeholder={m('supplierChat.inputPlaceholder')}
				aria-label={m('supplierChat.inputLabel')}
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
							placeholder={m('supplierChat.mentionPlaceholder')}
							aria-label={m('supplierChat.mentionLabel')}
							bind:value={mentionQuery}
							onfocus={() => (mentionOpen = true)}
							oninput={() => (mentionOpen = true)}
							onblur={() => setTimeout(() => (mentionOpen = false), 150)}
							data-testid="chat-mention-input"
						/>
						{#if mentionOpen && mentionCandidates.length > 0}
							<ul class="chat-mention-list" data-testid="chat-mention-list">
								{#each mentionCandidates.slice(0, 6) as mention (mention.id)}
									<li>
										<button
											type="button"
											class="chat-mention-option"
											onclick={() => addMention(mention)}
										>
											{mention.full_name}
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
					{file ? m('supplierChat.changeFile') : m('supplierChat.attach')}
				</label>
				{#if file}
					<span class="chat-file-name">
						{file.name}
						<button type="button" class="chat-file-clear" aria-label={m('supplierChat.removeAttachment')} onclick={clearFile}>&times;</button>
					</span>
				{/if}

				<button
					type="button"
					class="chat-send"
					disabled={!canSend}
					onclick={send}
					data-testid="chat-send"
				>
					{sending ? m('supplierChat.sending') : m('supplierChat.send')}
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
		background: color-mix(in srgb, var(--accent) 12%, transparent);
		border-color: color-mix(in srgb, var(--accent) 30%, transparent);
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
		color: var(--accent);
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
		box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 15%, transparent);
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
