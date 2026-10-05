<script lang="ts">
	// Roles moved into the tabbed Users & Roles page. Keep this route as a
	// redirect so existing links, bookmarks, and the sidebar (pre-merge) still
	// land on the Roles tab.
	import { goto } from '$app/navigation';

	$effect(() => {
		goto('/admin?tab=roles', { replace: true });
	});
</script>
