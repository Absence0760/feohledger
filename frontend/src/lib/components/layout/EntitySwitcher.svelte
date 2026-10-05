<script lang="ts">
	import { entityStore, ALL_ENTITIES } from '#lib/stores/entity.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';

	let { collapsed = false }: { collapsed?: boolean } = $props();

	let open = $state(false);
	let triggerBtn = $state<HTMLButtonElement | null>(null);

	// Default entity first, then the rest alphabetically (matches the backend
	// list order); "All entities" is rendered as a fixed first option.
	//
	// ACTIVE entities only. An entity deactivated on `/admin/entities` is
	// archived, and the backend does not police that end: `get_entity_id`
	// validates only that the id exists, so a retired entity left in this menu
	// stayed selectable and `get_write_entity_id` kept filing new invoices,
	// vendors and payments under it.
	let entities = $derived(entityStore.activeEntities);
	let selectedId = $derived(entityStore.selectedId);
	// The entity that was selected until this session's load found it retired.
	// The store has already dropped the selection back to the consolidated view;
	// this is what stops that being silent (see the disabled option below).
	let retired = $derived(entityStore.deactivatedSelection);
	// The store's `selectedLabel` falls back to an English literal, so the label
	// is derived here instead: `m()` is reactive to a locale switch and the store
	// stays free of i18n. `selected` is null exactly when "all entities" is active.
	let label = $derived(entityStore.selected?.name ?? m('entity.all'));

	function choose(id: string) {
		open = false;
		entityStore.select(id); // persists + reloads when changed
	}

	// Esc closes the entity menu and restores focus to its trigger, matching the
	// backdrop-click dismissal — so a keyboard user isn't trapped in the open menu.
	function onWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			open = false;
			triggerBtn?.focus();
		}
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if entityStore.multiEntity}
	<div class="entity-switcher">
		{#if open}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="entity-backdrop" onclick={() => (open = false)} onkeydown={() => {}}></div>
			<div class="entity-menu" role="listbox" aria-label={m('entity.selectAria')}>
				<button
					class="entity-option"
					class:selected={selectedId === ALL_ENTITIES}
					role="option"
					aria-selected={selectedId === ALL_ENTITIES}
					onclick={() => choose(ALL_ENTITIES)}
				>
					{m('entity.all')}
					<span class="entity-option-sub">{m('entity.consolidated')}</span>
				</button>
				{#each entities as e (e.id)}
					<button
						class="entity-option"
						class:selected={selectedId === e.id}
						role="option"
						aria-selected={selectedId === e.id}
						onclick={() => choose(e.id)}
					>
						{e.name}
						{#if e.is_default}<span class="entity-option-sub">{m('entity.default')}</span>{/if}
					</button>
				{/each}
				{#if retired}
					<!-- The entity this session started on, deactivated while it was in
					     use. Listed once, disabled, rather than silently disappearing:
					     the store has already moved the scope back to "All entities"
					     (so nothing new lands in a retired subsidiary), and this menu
					     is where the user goes looking for the choice they made. It is
					     gone on the next load, because that reset is persisted — a
					     change notice, not a permanent row. -->
					<button
						class="entity-option retired"
						role="option"
						aria-selected="false"
						aria-disabled="true"
						disabled
						data-testid="entity-option-retired"
					>
						{retired.name}
						<span class="entity-option-sub">{m('entity.deactivated')}</span>
						<!-- The sentence lives INSIDE the option, not beside it: this
						     container is a `role="listbox"`, whose only permitted
						     children are options, so a loose <p> here would break the
						     role contract. As option content it is also part of the
						     option's accessible name, which is what a change notice
						     wants — the row is announced as the entity, its state, and
						     why the scope moved. -->
						<span class="entity-option-note">{m('entity.deactivatedNote')}</span>
					</button>
				{/if}
			</div>
		{/if}
		<button
			bind:this={triggerBtn}
			class="entity-btn"
			class:collapsed
			title={collapsed ? m('entity.triggerTitle', { name: label }) : ''}
			aria-haspopup="listbox"
			aria-expanded={open}
			onclick={() => (open = !open)}
		>
			<span class="entity-icon" aria-hidden="true">
				<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 21h18"/><path d="M5 21V7l8-4v18"/><path d="M19 21V11l-6-4"/><path d="M9 9v.01"/><path d="M9 12v.01"/><path d="M9 15v.01"/></svg>
			</span>
			{#if !collapsed}
				<span class="entity-text">
					<span class="entity-name">{label}</span>
					<span class="entity-hint">{m('entity.hint')}</span>
				</span>
				<svg class="entity-caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class:flipped={open}><polyline points="6 9 12 15 18 9"/></svg>
			{/if}
		</button>
	</div>
{/if}

<style>
	.entity-switcher {
		position: relative;
		margin: 0 0 10px;
	}

	.entity-btn {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 10px;
		border-radius: 6px;
		border: 1px solid var(--border);
		background: var(--surface);
		color: var(--text);
		cursor: pointer;
		font-family: inherit;
		transition: all 0.12s;
	}

	.entity-btn:hover {
		border-color: var(--accent);
	}

	.entity-btn.collapsed {
		justify-content: center;
		padding: 8px 0;
		border-color: transparent;
	}

	.entity-icon {
		display: grid;
		place-items: center;
		flex-shrink: 0;
		width: 18px;
		height: 18px;
		color: var(--accent);
	}

	.entity-text {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		overflow: hidden;
		flex: 1;
		min-width: 0;
	}

	.entity-name {
		font-size: 0.85rem;
		font-weight: 600;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 100%;
	}

	.entity-hint {
		font-size: 0.65rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}

	.entity-caret {
		flex-shrink: 0;
		color: var(--text-muted);
		transition: transform 0.2s ease;
	}

	.entity-caret.flipped {
		transform: rotate(180deg);
	}

	.entity-backdrop {
		position: fixed;
		inset: 0;
		z-index: 60;
	}

	.entity-menu {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		margin-top: 6px;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
		padding: 6px;
		z-index: 61;
		display: flex;
		flex-direction: column;
		gap: 2px;
		max-height: 320px;
		overflow-y: auto;
	}

	.entity-option {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		width: 100%;
		padding: 8px 10px;
		border-radius: 5px;
		border: none;
		background: none;
		color: var(--text);
		font-size: 0.85rem;
		font-weight: 500;
		text-align: left;
		cursor: pointer;
		font-family: inherit;
		transition: background 0.12s;
	}

	.entity-option:not(:disabled):hover {
		background: rgba(99, 140, 255, 0.08);
	}

	/* De-emphasised with the muted TOKEN, never `opacity` — a fade composites
	   the whole subtree and would drag the sub-label under 4.5:1 with it
	   (app.css § .row-muted). `--text-muted` is 5.38:1 on `--surface`. */
	.entity-option:disabled {
		color: var(--text-muted);
		cursor: default;
	}

	.entity-option.selected {
		background: rgba(99, 140, 255, 0.12);
		color: var(--accent);
	}

	/* The retired row is the only option that carries a sentence, so it wraps
	   onto a second line rather than squeezing the name/state row above it. */
	.entity-option.retired {
		flex-wrap: wrap;
	}

	.entity-option-note {
		flex-basis: 100%;
		margin-top: 2px;
		font-size: 0.7rem;
		line-height: 1.35;
		text-align: left;
		white-space: normal;
		color: var(--text-muted);
	}

	.entity-option-sub {
		font-size: 0.68rem;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--text-muted);
		flex-shrink: 0;
	}
</style>
