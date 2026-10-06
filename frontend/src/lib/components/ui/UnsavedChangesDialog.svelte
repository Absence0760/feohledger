<script lang="ts">
	/**
	 * The single "unsaved changes" dialog, mounted once in the root layout and
	 * driven by `#lib/stores/unsavedChanges.svelte.ts`. Pages never render it —
	 * they call `guardUnsavedChanges(() => dirty)`.
	 *
	 * "Stay" is the safe answer, so it takes focus and is what Escape and a
	 * backdrop click mean. "Leave" discards the edits and continues.
	 */
	import Modal from './Modal.svelte';
	import { unsavedChanges } from '#lib/stores/unsavedChanges.svelte.ts';
	import { m } from '#lib/i18n/store.svelte.ts';
</script>

<Modal
	open={unsavedChanges.pending}
	ariaLabel={m('common.unsaved.title')}
	title={m('common.unsaved.title')}
	width="sm"
	onclose={unsavedChanges.stay}
>
	<p class="modal-hint">{m('common.unsaved.body')}</p>
	<div class="modal-footer">
		<button type="button" class="btn-cancel" onclick={unsavedChanges.leave}>
			{m('common.unsaved.leave')}
		</button>
		<button type="button" class="btn-primary" data-autofocus onclick={unsavedChanges.stay}>
			{m('common.unsaved.stay')}
		</button>
	</div>
</Modal>
