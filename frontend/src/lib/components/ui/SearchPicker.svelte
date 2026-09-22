<script lang="ts" generics="T extends { id: string }">
	/**
	 * A searchable, SERVER-PAGED combobox — the control under `ui/VendorPicker`
	 * and `ui/InvoicePicker`.
	 *
	 * Extracted from VendorPicker when the `/credit-memos` invoice selects needed
	 * the same control (`docs/decisions.md` §202): the keyboard contract, the
	 * Escape-capture binding (§146), the typed/committed split and the honest
	 * count line are each a bug that was found and fixed once, and a second copy
	 * would have to be kept in step by hand. Wrappers own only what differs —
	 * where the options come from, how one is labelled and drawn, and the copy.
	 *
	 * Three properties it must keep:
	 *
	 * 1. **Reach.** Filtering happens on the server (`load({search})`), so an
	 *    option on page 40 is one query away. Filtering a truncated client page
	 *    would not be.
	 * 2. **Honesty.** The count line says how many of the matching set is on
	 *    screen. A subset is never presented as the whole, and a load failure is
	 *    never presented as an empty set.
	 * 3. **Keyboard + AT parity with the `<select>` it replaced.** WAI-ARIA 1.2
	 *    combobox: roving `aria-activedescendant` over a real `role="listbox"`,
	 *    Arrow/Home/End/Enter/Escape, an accessible name from a real `<label>`,
	 *    and the count line wired in as the input's description.
	 *
	 * The rules live in `$lib/utils/searchPicker.ts` so they are testable without
	 * a DOM; this file owns fetching, focus and markup.
	 */
	import type { Snippet } from 'svelte';
	import { untrack } from 'svelte';
	import { appendUnique } from '$lib/utils/pagination';
	import { createRequestSequencer } from '$lib/utils/requestSequence';
	import {
		SEARCH_PICKER_PAGE_SIZE,
		hasMoreOptions,
		nextActiveIndex,
		resolveEnterSelection,
		revertedQuery,
		searchPickerCount,
		searchPickerDelay,
		searchPickerStatus,
		searchPickerStatusText,
		type SearchPickerFailure,
		type SearchPickerLoad,
		type SearchPickerText
	} from '$lib/utils/searchPicker';

	let {
		value = $bindable(''),
		load,
		optionLabel,
		option,
		text,
		preload = false,
		label,
		ariaLabel,
		hint,
		selectedLabel = null,
		placeholder,
		required = false,
		disabled = false,
		fullWidth = false,
		compact = false,
		class: className = '',
		testid,
		onselect
	}: {
		/** The chosen option's id — `''` for none. This is what the form submits. */
		value?: string;
		/** Fetches one page. A new function identity is a new SOURCE: the picker
		 *  drops the options it holds for the old one (a changed vendor, say). */
		load: SearchPickerLoad<T>;
		/** The input's text once an option is committed. */
		optionLabel: (option: T) => string;
		/** The inside of one option row. Its text is the option's accessible name. */
		option: Snippet<[T]>;
		/** Every string the control renders, already localized. */
		text: SearchPickerText;
		/**
		 * Fetch the unfiltered first page as soon as the control is enabled
		 * with a source (and again whenever the source changes), rather than
		 * on first open — so the CLOSED control can already say the set is
		 * empty or unreachable. For a field whose set IS the dialog's purpose.
		 */
		preload?: boolean;
		/** Visible field label. Omit for an inline control and pass `ariaLabel`. */
		label?: string;
		/** Accessible name when there is no visible label. */
		ariaLabel?: string;
		/** A standing explanation under the field. Wired into the input's
		 *  description beside the count line, never into its NAME — a hint
		 *  folded into the label is re-read on every focus. */
		hint?: string;
		/** The committed option's label when the form opened on an existing row.
		 *  The option may sit on any page of the set, so the picker cannot look
		 *  it up from the first page — the parent already has it. */
		selectedLabel?: string | null;
		placeholder?: string;
		required?: boolean;
		disabled?: boolean;
		/** Span both columns of a `.form-grid`. */
		fullWidth?: boolean;
		/** Narrow sizing for an inline row of controls. */
		compact?: boolean;
		/** Extra class on the root — the wrapper's selector hook. */
		class?: string;
		testid?: string;
		/** Fired on every commit — the chosen option, or null when cleared. */
		onselect?: (option: T | null) => void;
	} = $props();

	const uid = $props.id();
	const inputId = `${uid}-input`;
	const labelId = `${uid}-label`;
	const listId = `${uid}-list`;
	const countId = `${uid}-count`;
	const hintId = `${uid}-hint`;
	const optionId = (i: number) => `${uid}-opt-${i}`;

	/**
	 * The settled answer the picker holds, stamped with the SOURCE and the term
	 * it answers. The source stamp is what makes a changed source safe: options
	 * fetched for the previous vendor are simply not this source's, so they are
	 * never drawn, never committed by Enter, and never announced — without an
	 * effect racing to clear them.
	 *
	 * `failed` is a failed LIST (nothing on screen, and the count line must say
	 * so rather than let an empty popup read as an empty set). `moreFailed` is a
	 * failed NEXT page: the page that landed stays rendered and still valid, so
	 * the count line keeps reporting it and only the footer says what could not
	 * be added.
	 */
	type Answer = {
		source: SearchPickerLoad<T>;
		term: string;
		options: T[];
		total: number;
		page: number;
		failed: boolean;
		moreFailed: boolean;
	};
	let answer = $state.raw<Answer | null>(null);
	let loading = $state(false);
	let open = $state(false);
	let activeIndex = $state(-1);
	let inputEl = $state<HTMLInputElement | null>(null);

	const current = $derived(answer && answer.source === load ? answer : null);
	const options = $derived(current?.options ?? []);
	const total = $derived(current?.total ?? 0);
	const failure = $derived<SearchPickerFailure>(
		current?.failed ? 'list' : current?.moreFailed ? 'more' : 'none'
	);

	/** The option the user picked in THIS session of the control. Kept so the
	 *  committed label survives the next search replacing `options`. */
	let picked = $state<T | null>(null);

	/**
	 * What the user has typed, or `null` when they have not typed since the box
	 * last settled. `''` (they cleared it) is a THIRD state and must not collapse
	 * into `null` — that is why this is nullable rather than a companion boolean.
	 *
	 * It also keeps the search term separate from the display text. At rest the
	 * box shows the committed option's LABEL, which is not a search term:
	 * issuing it as one returns no matches and reads as "there are none".
	 */
	let typed = $state<string | null>(null);

	const committedLabel = $derived.by(() => {
		if (!value) return null;
		if (picked?.id === value) return optionLabel(picked);
		const inPage = options.find((o) => o.id === value);
		if (inPage) return optionLabel(inPage);
		return selectedLabel;
	});

	/** The input's text: what was typed, else the reverted (committed) label. */
	const query = $derived(typed ?? revertedQuery(committedLabel));
	const searchTerm = $derived(typed ?? '');

	const count = $derived(searchPickerCount(options.length, total, loading));
	const showMore = $derived(hasMoreOptions(options.length, total));
	/** The listbox exists only when it has options. With none, the count line
	 *  under the input already says why (loading / no matches / load failed), so
	 *  a floating empty box would only restate it — and `aria-expanded` must
	 *  agree with whether a listbox is actually there. */
	const expanded = $derived(open && options.length > 0);

	/**
	 * A value is committed but nothing on this screen can name it.
	 *
	 * Only reachable when the consuming row's API shape carries no label for it
	 * (today: `Catalog`'s vendor). The native `<select>` this replaced hit the
	 * same case and fell back to rendering its empty first option — reading as
	 * "nothing selected" while the form still held one. Saying it is the fix;
	 * the value is never dropped either way.
	 */
	const unresolvedSelection = $derived(!!value && committedLabel === null);

	const countText = $derived(
		searchPickerStatusText(
			searchPickerStatus({
				open,
				count,
				failure,
				searchTerm,
				answeredTerm: current && !loading ? current.term : null,
				unresolvedSelection,
				announceAtRest: preload
			}),
			text
		)
	);

	const fetchSequence = createRequestSequencer();

	async function fetchPage(nextPage: number, replace: boolean) {
		const source = load;
		const term = searchTerm;
		const token = fetchSequence.start();
		loading = true;
		try {
			const res = await source({ search: term, page: nextPage, page_size: SEARCH_PICKER_PAGE_SIZE });
			if (!fetchSequence.canCommit(token)) return;
			const base = !replace && answer && answer.source === source ? answer : null;
			answer = base
				? {
						...base,
						// `appendUnique`, not a bare concat: offset pagination can
						// re-surface a row when the set shifts between fetches, and a
						// duplicate id crashes the keyed `{#each}` (`each_key_duplicate`).
						options: appendUnique(base.options, res.items),
						total: res.total,
						page: nextPage,
						moreFailed: false
					}
				: {
						source,
						term,
						options: res.items,
						total: res.total,
						page: nextPage,
						failed: false,
						moreFailed: false
					};
			// A replaced result set drops the highlight: carrying it onto whichever
			// row now occupies that position would let Enter commit an option the
			// user never looked at.
			if (!base) activeIndex = -1;
		} catch {
			if (!fetchSequence.canCommit(token)) return;
			// Surfaced, never swallowed. "You may not list these" (403) and "there
			// are none" are different answers and the picker must not present the
			// first as the second.
			if (!replace && answer && answer.source === source) {
				answer = { ...answer, moreFailed: true };
			} else {
				answer = { source, term, options: [], total: 0, page: 1, failed: true, moreFailed: false };
				activeIndex = -1;
			}
		} finally {
			// `isCurrentRequest`, not `canCommit` — see `utils/requestSequence.ts`.
			if (fetchSequence.isCurrentRequest(token)) loading = false;
		}
	}

	// Re-query on open, on every search edit (debounced) and on a new source. A
	// cleared box fires immediately; typing waits (`searchPickerDelay`).
	let searchTimer: ReturnType<typeof setTimeout>;
	$effect(() => {
		if (!open) return;
		const q = searchTerm;
		void load;
		// Set synchronously, not inside the debounced fetch. Until the timer
		// fires, `count` would otherwise be recomputed off the PREVIOUS result
		// set — 0 of 0 on a first open — and the count line would announce "none"
		// through its live region before replacing it with "Loading".
		loading = true;
		clearTimeout(searchTimer);
		searchTimer = setTimeout(() => void fetchPage(1, true), searchPickerDelay(q));
		return () => clearTimeout(searchTimer);
	});

	// `preload`: fetch the unfiltered first page while CLOSED whenever the
	// control has a source it has no answer for. Runs on the source, the
	// enablement and the flag only — `open` and `answer` are read untracked, so
	// opening or answering never re-fires it (the open effect above owns those).
	$effect(() => {
		if (!preload || disabled) return;
		const source = load;
		untrack(() => {
			if (!open && answer?.source !== source) void fetchPage(1, true);
		});
	});

	function commit(chosen: T) {
		picked = chosen;
		value = chosen.id;
		// Back to "not typing" — the box now displays this option's label via
		// `committedLabel`, so the text and the committed value cannot disagree.
		typed = null;
		open = false;
		activeIndex = -1;
		onselect?.(chosen);
	}

	function clear() {
		picked = null;
		value = '';
		typed = null;
		activeIndex = -1;
		onselect?.(null);
		inputEl?.focus();
	}

	function closeAndRevert() {
		open = false;
		activeIndex = -1;
		// Dropping back to "not typing" reverts the text through `query`'s
		// `revertedQuery(committedLabel)`, so the box can never be left showing a
		// search term that disagrees with the value the form will submit.
		typed = null;
	}

	/**
	 * Bound with `onkeydownCAPTURE`, deliberately (`docs/decisions.md` §146).
	 *
	 * `ui/Modal` traps focus with `actions/focusTrap`, which registers a REAL
	 * `keydown` listener on the dialog box — while Svelte 5 delegates a plain
	 * `onkeydown` to one listener at the app root. Real bubbling reaches the
	 * dialog first, so a bubble-phase handler here would run AFTER the trap had
	 * already seen Escape and shut the whole modal, and stopping propagation
	 * from it would be too late to matter. A capture listener is attached to the
	 * input directly and fires in the TARGET phase, before the bubble phase
	 * reaches any ancestor — which is what lets the Escape branch below close
	 * the popup and keep the dialog open.
	 */
	function onKeydown(e: KeyboardEvent) {
		if (disabled) return;

		if (e.key === 'Escape') {
			if (!open) return; // let it through — the enclosing Modal closes
			// Close the popup, not the dialog around it. See the CAPTURE note on
			// the binding below for why stopping propagation here actually works.
			e.stopPropagation();
			e.preventDefault();
			closeAndRevert();
			return;
		}

		if (e.key === 'Tab') {
			if (open) closeAndRevert();
			return;
		}

		if (e.key === 'Enter') {
			if (!open) return; // closed: let the form submit as normal
			e.preventDefault();
			const selection = resolveEnterSelection(activeIndex, options);
			if (selection) commit(selection);
			return;
		}

		const next = nextActiveIndex(e.key, activeIndex, options.length);
		if (next === null) return;
		e.preventDefault();
		if (!open) {
			open = true;
			return;
		}
		activeIndex = next;
	}

	function onInput(e: Event) {
		open = true;
		typed = (e.currentTarget as HTMLInputElement).value;
	}

	/**
	 * Focus SELECTS but deliberately does not OPEN.
	 *
	 * `ui/Modal` focus-traps, and `actions/focusTrap` moves focus to the first
	 * focusable element as the dialog mounts. Where this picker is the first
	 * field — `/credit-memos`' Apply dialog, `/recurring`, `/vendor-statements`,
	 * `/contracts` — opening on focus dropped the listbox over the rest of the
	 * form before the user had done anything, and every control beneath it was
	 * then unclickable until something dismissed the popup. That is also what
	 * the ARIA 1.2 combobox pattern says: the listbox expands on an explicit
	 * user action, not on focus arriving.
	 *
	 * No intentional path loses its popup — a click opens it (`onClick`),
	 * typing opens it (`onInput`), and Arrow/Home/End open it (`onKeyDown`).
	 * A click on an unfocused input fires `focus` then `click`, so
	 * click-to-open still works in one gesture.
	 */
	function onFocus() {
		if (disabled) return;
		// Select the committed label so the first keystroke replaces it rather
		// than appending to a label the user is not trying to extend.
		inputEl?.select();
	}

	/**
	 * Re-open on a click into a field that already holds focus.
	 *
	 * Committing a choice closes the popup WITHOUT moving focus, so from then on
	 * the input is the active element — and clicking an already-focused element
	 * fires no `focus` event. Without this the control was one-shot: choose an
	 * option, then clicking the field to change it did nothing at all.
	 * Deliberately does NOT re-`select()`; a second click is usually the user
	 * placing a cursor, not restarting the search.
	 */
	function onClick() {
		if (!disabled) open = true;
	}

	function onFocusOut(e: FocusEvent) {
		// Ignore focus moving WITHIN the control (the clear button, Load more).
		const next = e.relatedTarget as Node | null;
		if (next && (e.currentTarget as HTMLElement).contains(next)) return;
		closeAndRevert();
	}
</script>

<div
	class="search-picker {className}"
	class:full-width={fullWidth}
	class:compact
	onfocusout={onFocusOut}
	data-testid={testid ? `${testid}-field` : undefined}
>
	{#if label}
		<label class="sp-label" id={labelId} for={inputId}>
			{label}{#if required}<em class="required">*</em>{/if}
		</label>
	{/if}
	<div class="sp-control">
		<input
			bind:this={inputEl}
			id={inputId}
			type="text"
			role="combobox"
			class="sp-input"
			autocomplete="off"
			aria-expanded={expanded}
			aria-controls={expanded ? listId : undefined}
			aria-haspopup="listbox"
			aria-autocomplete="list"
			aria-activedescendant={expanded && activeIndex >= 0 ? optionId(activeIndex) : undefined}
			aria-describedby={hint ? `${countId} ${hintId}` : countId}
			aria-labelledby={label ? labelId : undefined}
			aria-label={label ? undefined : ariaLabel}
			{placeholder}
			{required}
			{disabled}
			value={query}
			oninput={onInput}
			onfocus={onFocus}
			onclick={onClick}
			onkeydowncapture={onKeydown}
			data-testid={testid}
		/>
		{#if value && !disabled}
			<button type="button" class="sp-clear" aria-label={text.clearAria} onclick={clear}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
			</button>
		{/if}

		<!-- Always in the DOM so the live region exists before its text changes;
		     `:empty` hides it when there is nothing to say. It is also the
		     input's `aria-describedby` target, so "showing 25 of 137" is part of
		     the field's description rather than a sighted-only footnote. -->
		<p class="sp-count" id={countId} aria-live="polite">{countText}</p>
		{#if hint}
			<p class="sp-hint" id={hintId}>{hint}</p>
		{/if}

		{#if expanded}
			<div class="sp-popup">
				<ul class="sp-list" id={listId} role="listbox" aria-label={label ?? ariaLabel ?? text.listAria}>
					{#each options as opt, i (opt.id)}
						<li
							class="sp-option"
							class:active={i === activeIndex}
							class:selected={opt.id === value}
							id={optionId(i)}
							role="option"
							aria-selected={opt.id === value}
							onmousedown={(e) => {
								// Keep focus on the input: a blur here would close the
								// popup before the click landed.
								e.preventDefault();
								commit(opt);
							}}
						>
							{@render option(opt)}
						</li>
					{/each}
				</ul>
				{#if showMore}
					<div class="sp-foot">
						<span class="sp-foot-note">
							{failure === 'more' ? text.loadMoreFailed : text.refineHint}
						</span>
						<button
							type="button"
							class="sp-more"
							onmousedown={(e) => e.preventDefault()}
							onclick={() => void fetchPage((current?.page ?? 1) + 1, false)}
							disabled={loading}
							data-testid={testid ? `${testid}-more` : undefined}
						>
							{loading ? text.loading : text.loadMore}
						</button>
					</div>
				{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	/* The control replicates the `.form-grid label` recipe its consumers use
	   (column flex, 4px gap, 0.82rem muted label) because Svelte scopes that
	   rule to the PARENT component — `label:where(.svelte-parent)` never
	   matches a child component's root. Owning it here also means every modal
	   renders an identical field instead of near-copies. */
	.search-picker {
		display: flex;
		flex-direction: column;
		gap: 4px;
		font-size: 0.82rem;
		color: var(--text-muted);
		min-width: 0;
	}

	.search-picker.full-width {
		grid-column: 1 / -1;
	}

	.search-picker.compact {
		flex: 0 1 190px;
		min-width: 150px;
	}

	.sp-label {
		color: inherit;
	}

	.sp-control {
		position: relative;
	}

	.sp-input {
		width: 100%;
		padding: 7px 30px 7px 9px;
		border-radius: 5px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.88rem;
	}

	.compact .sp-input {
		padding: 6px 26px 6px 8px;
		font-size: 0.82rem;
	}

	.sp-input:disabled {
		opacity: 0.7;
		cursor: not-allowed;
	}

	.sp-clear {
		position: absolute;
		top: 50%;
		right: 6px;
		transform: translateY(-50%);
		display: grid;
		place-items: center;
		width: 20px;
		height: 20px;
		padding: 0;
		border: none;
		border-radius: 4px;
		background: none;
		/* `--text-muted` is 5.38:1 on `--bg` — an icon-only control still has to
		   meet 1.4.11 against its own background, and dimming with `opacity`
		   would drag it under (app.css § .row-muted). */
		color: var(--text-muted);
		cursor: pointer;
	}

	.sp-clear:hover {
		color: var(--text);
	}

	.sp-count {
		margin: 0;
		font-size: 0.72rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.sp-count:empty {
		display: none;
	}

	/* Muted on `--surface` clears 4.5:1; no `opacity` — the token has already
	   done that job and a fade only spends contrast. */
	.sp-hint {
		margin: 2px 0 0;
		font-size: 0.78rem;
		line-height: 1.4;
		color: var(--text-muted);
	}

	.sp-popup {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		z-index: 61;
		margin-top: 4px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface);
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		overflow: hidden;
	}

	.sp-list {
		list-style: none;
		margin: 0;
		padding: 4px;
		max-height: 240px;
		overflow-y: auto;
	}

	.sp-option {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		padding: 7px 9px;
		border-radius: 5px;
		font-size: 0.85rem;
		color: var(--text);
		cursor: pointer;
	}

	.sp-option:hover,
	.sp-option.active {
		background: rgba(99, 140, 255, 0.12);
	}

	.sp-option.selected {
		color: var(--accent);
		font-weight: 600;
	}

	.sp-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 7px 9px;
		border-top: 1px solid var(--border);
		font-size: 0.72rem;
		color: var(--text-muted);
	}

	.sp-foot-note {
		min-width: 0;
	}

	.sp-more {
		flex-shrink: 0;
		padding: 4px 10px;
		border: 1px solid var(--border);
		border-radius: 5px;
		background: var(--bg);
		color: var(--text);
		font-family: inherit;
		font-size: 0.72rem;
		cursor: pointer;
	}

	.sp-more:hover:not(:disabled) {
		border-color: var(--accent);
	}

	.sp-more:disabled {
		cursor: default;
	}
</style>
