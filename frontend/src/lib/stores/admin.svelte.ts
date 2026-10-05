import type { AdminUser, PermissionCatalogEntry, Role } from '#lib/types/admin.ts';
import { api } from '#lib/api.ts';
import { appendUnique } from '#lib/utils/pagination.ts';
import { createRequestSequencer } from '#lib/utils/requestSequence.ts';

interface AdminUserListResponse {
	items: AdminUser[];
	total: number;
}

interface CreateUserResponse extends AdminUser {
	temporary_password: string;
}

interface FetchUsersOptions {
	search?: string;
	page?: number;
	pageSize?: number;
	append?: boolean;
}

/**
 * The minimal projection of a user that the invoice approver picker needs.
 *
 * Deliberately NOT `AdminUser`: this list is readable by every role that may
 * submit an invoice or assign a reviewer, so it carries only the display name
 * that already appears on the invoice's `assigned_to` — no email, no roles, no
 * last-login. Served by `GET /api/invoices/assignable-reviewers`.
 */
export interface AssignableReviewer {
	id: string;
	full_name: string;
	is_active: boolean;
}

function createAdminStore() {
	let users = $state<AdminUser[]>([]);
	let roles = $state<Role[]>([]);
	let permissionCatalog = $state<PermissionCatalogEntry[]>([]);
	let loading = $state(false);
	let total = $state(0);
	let page = $state(1);
	let pageSize = $state(20);

	// Did the most recent (non-append) users load fail? The table's empty state
	// reads it: without it a 500 / offline backend leaves `users` empty and the
	// user directory renders "No users." — an outage indistinguishable from a
	// tenant with nobody in it, on the page an admin goes to when access is
	// already misbehaving. Both call sites already carried a comment claiming
	// this flag existed; it did not, and the `.catch(() => {})` beside them ate
	// the only signal there was. Set only while this is still the newest request
	// (`isCurrentRequest`, the same rule `loading` uses), and the error is
	// re-thrown so an awaiting caller keeps its own handling. Mirrors
	// `stores/invoices.svelte.ts`.
	let errored = $state(false);
	// The same, for the independent roles request.
	let rolesErrored = $state(false);

	// Users and roles are two independent lists loaded by two independent
	// requests, so they get a sequencer each — a roles refresh must not mark an
	// in-flight users fetch un-committable, or vice versa. Within each, the
	// create/update/delete helpers below mutate the list in place with no fetch
	// of their own, so they retire whatever is in flight before they write.
	// See `frontend/CLAUDE.md` § Sequencing list fetches.
	const usersSequence = createRequestSequencer();
	const rolesSequence = createRequestSequencer();

	async function fetchUsers(opts: FetchUsersOptions = {}) {
		const token = usersSequence.start();
		loading = true;
		try {
			const params = new URLSearchParams();
			if (opts.search) params.set('search', opts.search);
			const nextPage = opts.page ?? 1;
			params.set('page', String(nextPage));
			params.set('page_size', String(opts.pageSize ?? pageSize));
			const res = await api.get<AdminUserListResponse>(`/api/admin/users?${params}`);
			// Superseded by a newer search/page fetch, or by a local edit.
			if (!usersSequence.canCommit(token)) return;
			users = opts.append ? appendUnique(users, res.items) : res.items;
			total = res.total;
			page = nextPage;
			if (opts.pageSize) pageSize = opts.pageSize;
			errored = false;
		} catch (err) {
			if (usersSequence.isCurrentRequest(token)) errored = true;
			throw err;
		} finally {
			if (usersSequence.isCurrentRequest(token)) loading = false;
		}
	}

	async function loadMoreUsers(opts: { search?: string } = {}) {
		await fetchUsers({ ...opts, page: page + 1, append: true });
	}

	// --- Assignable reviewers (non-admin-readable) ---
	//
	// `GET /api/admin/users` is gated `user.manage` (admin by default), so
	// every ap_manager/cfo/ap_clerk that opened the invoice approver picker
	// got a 403, an empty `<select>`, and a Submit button disabled forever —
	// even though the backend explicitly allows them to submit and assign.
	// This is the narrow list they can read. It lives beside the user
	// directory (same subject, one owner) rather than in a second store, and
	// it never widens the admin endpoint.
	let assignableReviewers = $state<AssignableReviewer[]>([]);
	// Settled-with-nothing vs not-settled-yet: the picker must not flash a
	// "no reviewers" note while the request is still in flight, and the caller
	// falls back only on a real failure.
	let reviewersLoaded = $state(false);
	let reviewersErrored = $state(false);
	const reviewersSequence = createRequestSequencer();

	/** Load the assignable-reviewer list. Never throws — the picker degrades to
	 *  "submit unassigned" rather than dead-ending, so a failure is a state the
	 *  UI renders, not an exception the caller has to remember to catch.
	 *  Resolves `true` when the list is usable. */
	async function fetchAssignableReviewers(): Promise<boolean> {
		const token = reviewersSequence.start();
		try {
			// A bare array (the `/api/gl-accounts` shape) is the contract; the
			// `{items}` unwrap is one line of tolerance so a paginated backend
			// answer can't silently render an empty picker.
			const res = await api.get<AssignableReviewer[] | { items: AssignableReviewer[] }>(
				'/api/invoices/assignable-reviewers'
			);
			if (!reviewersSequence.canCommit(token)) return true;
			assignableReviewers = Array.isArray(res) ? res : (res?.items ?? []);
			reviewersErrored = false;
			return true;
		} catch {
			if (!reviewersSequence.isCurrentRequest(token)) return false;
			assignableReviewers = [];
			reviewersErrored = true;
			return false;
		} finally {
			if (reviewersSequence.isCurrentRequest(token)) reviewersLoaded = true;
		}
	}

	async function fetchRoles() {
		const token = rolesSequence.start();
		try {
			const fetched = await api.get<Role[]>('/api/admin/roles');
			if (!rolesSequence.canCommit(token)) return;
			roles = fetched;
			rolesErrored = false;
		} catch {
			// Swallowed rather than re-thrown (this is the fire-and-forget
			// companion of the users load), but no longer silent: the panel reads
			// `rolesErrored` so a failed load says so instead of asserting "No
			// system roles." on the page that decides who can do what.
			if (rolesSequence.isCurrentRequest(token)) rolesErrored = true;
		}
	}

	async function fetchPermissionCatalog() {
		// Cache for the session — the catalog is static. Refetch only if empty.
		if (permissionCatalog.length > 0) return;
		try {
			permissionCatalog = await api.get<PermissionCatalogEntry[]>('/api/admin/permissions');
		} catch {
			// non-critical — the role editor degrades to no checkboxes
		}
	}

	async function createUser(data: {
		email: string;
		full_name: string;
		role_names: string[];
	}): Promise<CreateUserResponse> {
		const created = await api.post<CreateUserResponse>('/api/admin/users', data);
		// A users fetch already in flight read the list BEFORE this user existed,
		// so its response would drop the new row. An invite needs no pre-existing
		// row, so it races even the page's very first load.
		usersSequence.supersedeInFlight();
		users = [created, ...users];
		total += 1;
		return created;
	}

	async function updateUser(
		id: string,
		changes: Partial<{
			full_name: string;
			email: string;
			is_active: boolean;
			role_names: string[];
			password: string;
		}>
	): Promise<AdminUser> {
		const updated = await api.patch<AdminUser>(`/api/admin/users/${id}`, changes);
		usersSequence.supersedeInFlight();
		users = users.map((u) => (u.id === id ? updated : u));
		return updated;
	}

	async function deleteUser(id: string): Promise<void> {
		await api.delete(`/api/admin/users/${id}`);
		usersSequence.supersedeInFlight();
		users = users.filter((u) => u.id !== id);
		total = Math.max(0, total - 1);
	}

	interface BulkDeleteFailure {
		user_id: string;
		reason: 'not_found' | 'self' | 'blocked';
		references: {
			open_invoice_assignments: number;
			pending_approval_steps: number;
			active_workflow_approver_in: number;
		} | null;
	}
	interface BulkDeleteResult {
		deleted: string[];
		failed: BulkDeleteFailure[];
	}

	async function bulkDeleteUsers(ids: string[]): Promise<BulkDeleteResult> {
		const result = await api.post<BulkDeleteResult>('/api/admin/users/bulk-delete', {
			user_ids: ids
		});
		const deletedSet = new Set(result.deleted);
		usersSequence.supersedeInFlight();
		users = users.filter((u) => !deletedSet.has(u.id));
		total = Math.max(0, total - result.deleted.length);
		return result;
	}

	async function createRole(data: {
		name: string;
		description?: string;
		permissions?: string[];
	}): Promise<Role> {
		const created = await api.post<Role>('/api/admin/roles', data);
		rolesSequence.supersedeInFlight();
		roles = [...roles, created];
		return created;
	}

	async function updateRole(
		id: string,
		changes: { description?: string; permissions?: string[] }
	): Promise<Role> {
		const updated = await api.patch<Role>(`/api/admin/roles/${id}`, changes);
		rolesSequence.supersedeInFlight();
		roles = roles.map((r) => (r.id === id ? updated : r));
		return updated;
	}

	async function deleteRole(id: string): Promise<void> {
		await api.delete(`/api/admin/roles/${id}`);
		rolesSequence.supersedeInFlight();
		roles = roles.filter((r) => r.id !== id);
	}

	return {
		get users() { return users; },
		get roles() { return roles; },
		get permissionCatalog() { return permissionCatalog; },
		get loading() { return loading; },
		get errored() { return errored; },
		get rolesErrored() { return rolesErrored; },
		get total() { return total; },
		get page() { return page; },
		get pageSize() { return pageSize; },
		get hasMore() { return users.length < total; },
		get assignableReviewers() { return assignableReviewers; },
		get reviewersLoaded() { return reviewersLoaded; },
		get reviewersErrored() { return reviewersErrored; },
		fetchUsers,
		loadMoreUsers,
		fetchAssignableReviewers,
		fetchRoles,
		fetchPermissionCatalog,
		createUser,
		updateUser,
		deleteUser,
		bulkDeleteUsers,
		createRole,
		updateRole,
		deleteRole,
	};
}

export const adminStore = createAdminStore();
