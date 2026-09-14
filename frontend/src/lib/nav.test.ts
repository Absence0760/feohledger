import { test, expect } from 'vitest';
import {
	NAV,
	canSee,
	isEntryActive,
	isEntryVisible,
	sectionTabActive,
	visibleChildren,
	type NavLink,
	type NavGroup,
} from './nav';
import { PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID, PERM_USER_MANAGE } from './types/admin';

const links = NAV.filter((e): e is NavLink => e.kind === 'link');
const link = (href: string): NavLink => links.find((l) => l.href === href)!;

const settings = NAV.find(
	(e): e is NavGroup => e.kind === 'group' && e.label === 'Settings'
)!;
const kids = settings.children;
const kid = (href: string) => kids.find((c) => c.href === href)!;
const url = (p: string) => new URL(`http://acme.localhost:7777${p}`);

test('isEntryActive: a plain link is active on its path and sub-paths', () => {
	const vendors = link('/vendors');
	expect(isEntryActive(vendors, '/vendors')).toBe(true);
	expect(isEntryActive(vendors, '/vendors/abc-123')).toBe(true);
	expect(isEntryActive(vendors, '/payments')).toBe(false);
});

test('isEntryActive: the most specific nested link wins (Vendors vs Screening)', () => {
	const vendors = link('/vendors');
	const screening = link('/vendors/screening');
	// On the screening sub-route, ONLY Screening lights up — not its parent.
	expect(isEntryActive(vendors, '/vendors/screening')).toBe(false);
	expect(isEntryActive(screening, '/vendors/screening')).toBe(true);
	// And the deeper link is not active on the plain list page.
	expect(isEntryActive(screening, '/vendors')).toBe(false);
	expect(isEntryActive(vendors, '/vendors')).toBe(true);
});

test('isEntryActive: the bank change-approval queue is its own most-specific link', () => {
	const vendors = link('/vendors');
	const changes = link('/vendors/change-requests');
	// The dual-control queue is a sibling sub-route of /vendors/screening; on
	// it, only it lights up — the parent Vendors row must not stay active.
	expect(isEntryActive(vendors, '/vendors/change-requests')).toBe(false);
	expect(isEntryActive(changes, '/vendors/change-requests')).toBe(true);
	expect(isEntryActive(changes, '/vendors')).toBe(false);
	// And it doesn't collide with the other /vendors sub-route.
	expect(isEntryActive(changes, '/vendors/screening')).toBe(false);
	expect(isEntryActive(link('/vendors/screening'), '/vendors/change-requests')).toBe(false);
});

test('the bank change-approval queue is gated to the roles the API allows', () => {
	// `GET /api/vendors/change-requests` is require_roles(ADMIN, AP_MANAGER) —
	// a CFO 403s, so the nav row must not offer it to them.
	expect(link('/vendors/change-requests').roles).toEqual(['admin', 'ap_manager']);
});

test('the discounts link matches the roles the API grants read to', () => {
	// `api/discounts.py::_READ_ROLES` includes ROLE_AP_CLERK — the dashboard,
	// the offer list and the per-invoice ROI are all readable by a clerk. The
	// nav used to hide the link from them and the page used to redirect them,
	// so all three layers disagreed with the one that actually enforces.
	// Pinned here because the drift was invisible: hiding a link that would
	// have worked reads exactly like a deliberate gate.
	const allLinks = NAV.flatMap((e) => (e.kind === 'group' ? e.children : [e]));
	const discounts = allLinks.find((l) => l.href === '/discounts')!;
	expect(discounts.roles).toEqual(['admin', 'ap_manager', 'ap_clerk', 'cfo']);
});

test('sectionTabActive: bare /admin defaults to the first same-path tab (Users)', () => {
	expect(sectionTabActive(kid('/admin?tab=users'), kids, url('/admin'))).toBe(true);
	expect(sectionTabActive(kid('/admin?tab=roles'), kids, url('/admin'))).toBe(false);
});

test('sectionTabActive: an explicit ?tab= selects exactly that tab', () => {
	expect(sectionTabActive(kid('/admin?tab=roles'), kids, url('/admin?tab=roles'))).toBe(true);
	expect(sectionTabActive(kid('/admin?tab=users'), kids, url('/admin?tab=roles'))).toBe(false);
});

test('sectionTabActive: a deeper sibling path does not light up the query tab', () => {
	// On /admin/api-keys, the API Keys tab is active and the /admin?tab=users
	// default tab is NOT (the prefix-overmatch bug this fix closes).
	expect(sectionTabActive(kid('/admin/api-keys'), kids, url('/admin/api-keys'))).toBe(true);
	expect(sectionTabActive(kid('/admin?tab=users'), kids, url('/admin/api-keys'))).toBe(false);
	expect(sectionTabActive(kid('/admin?tab=roles'), kids, url('/admin/api-keys'))).toBe(false);
});

test('every clerk-readable page offers the clerk a way in', () => {
	// Same drift the /discounts test above pins, on the three rows that still
	// had it. Each of these backends grants ap_clerk READ, and each page gates
	// its mutations separately — so hiding the nav row made a page the API
	// serves unreachable, which reads exactly like a deliberate gate:
	//
	//   /credit-memos      list   require_roles(ADMIN, AP_MANAGER, AP_CLERK, CFO)
	//   /recurring         list   recurring.py::_READ_ROLES (includes AP_CLERK)
	//   /vendors/screening queue  require_roles(ADMIN, AP_MANAGER, AP_CLERK, CFO)
	const allLinks = NAV.flatMap((e) => (e.kind === 'group' ? e.children : [e]));
	for (const href of ['/credit-memos', '/recurring', '/vendors/screening']) {
		const entry = allLinks.find((l) => l.href === href)!;
		expect(entry, href).toBeDefined();
		expect(entry.roles, href).toContain('ap_clerk');
	}
});

test('a page whose API 403s a clerk still hides its row', () => {
	// The inverse half, so "add ap_clerk everywhere" can't pass as the fix:
	// these three are genuinely clerk-forbidden at the API and must stay hidden.
	//   /payments                 require_roles(ADMIN, AP_MANAGER, CFO)
	//   /vendors/change-requests  require_roles(ADMIN, AP_MANAGER)
	//   /billing (subscription)   require_roles(ADMIN, CFO)
	const allLinks = NAV.flatMap((e) => (e.kind === 'group' ? e.children : [e]));
	for (const href of ['/payments', '/vendors/change-requests', '/billing']) {
		const entry = allLinks.find((l) => l.href === href)!;
		expect(entry, href).toBeDefined();
		expect(entry.roles, href).not.toContain('ap_clerk');
	}
});

test('the Payments row is reachable by role OR by holding payment.execute/payment.void', () => {
	// Closes the finding: a custom role granted ONLY `payment.execute` (or
	// only `payment.void`), with none of admin/ap_manager/cfo, must still see
	// the sidebar row — the supporting reads it needs (GET /api/payments,
	// GET /api/payments/runs/, …) are permission-gated for exactly this.
	const payments = link('/payments');
	expect(payments.permissions).toEqual([PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID]);

	const hasNoRole = () => false;
	const canNothing = () => false;
	expect(canSee(payments.roles, hasNoRole, payments.permissions, canNothing)).toBe(false);

	const canExecuteOnly = (perm: string) => perm === PERM_PAYMENT_EXECUTE;
	expect(canSee(payments.roles, hasNoRole, payments.permissions, canExecuteOnly)).toBe(true);

	const canVoidOnly = (perm: string) => perm === PERM_PAYMENT_VOID;
	expect(canSee(payments.roles, hasNoRole, payments.permissions, canVoidOnly)).toBe(true);

	// The pre-existing role-only path is untouched: a cfo with no granted
	// permissions still sees it.
	const hasCfo = (...roles: string[]) => roles.includes('cfo');
	expect(canSee(payments.roles, hasCfo, payments.permissions, canNothing)).toBe(true);

	// And `isEntryVisible` (what Sidebar actually calls) agrees at the whole-
	// entry level, not just via the lower-level `canSee` helper.
	expect(isEntryVisible(payments, hasNoRole, canExecuteOnly)).toBe(true);
	expect(isEntryVisible(payments, hasNoRole, canNothing)).toBe(false);
});

test('the Users tab lists both admin AND user.manage as its gate; Roles is admin-only', () => {
	// `GET /api/admin/users` migrated to require_permission(user.manage) —
	// see backend/app/api/admin.py — so a custom role holding only that
	// permission must see the Users tab. `POST/PATCH/DELETE /api/admin/roles`
	// (role CRUD) stayed require_roles(ROLE_ADMIN), so the Roles tab must NOT
	// gain a `permissions` alternative.
	const users = kid('/admin?tab=users');
	const roles = kid('/admin?tab=roles');
	expect(users.roles).toEqual(['admin']);
	expect(users.permissions).toEqual([PERM_USER_MANAGE]);
	expect(roles.roles).toEqual(['admin']);
	expect(roles.permissions).toBeUndefined();
});

test('canSee: permissions is an OR alternative to roles, not an AND', () => {
	const noHas = () => false;
	const noCan = () => false;
	const hasAdmin = (...roles: string[]) => roles.includes('admin');
	const canUserManage = (perm: string) => perm === PERM_USER_MANAGE;

	// Neither gate declared → always visible.
	expect(canSee(undefined, noHas)).toBe(true);
	// roles-only entry: unaffected by can being absent or always-false.
	expect(canSee(['admin'], hasAdmin)).toBe(true);
	// roles fail and no permissions declared → hidden.
	expect(canSee(['admin'], noHas)).toBe(false);
	// permissions-only match: roles fails (or is absent) but the permission matches.
	expect(canSee(['admin'], noHas, [PERM_USER_MANAGE], canUserManage)).toBe(true);
	// Neither the role nor the permission match → hidden.
	expect(canSee(['admin'], noHas, [PERM_USER_MANAGE], noCan)).toBe(false);
	// permissions declared but no can function supplied (e.g. a caller that
	// never passes one) must not throw and must not grant access on its own.
	expect(canSee(undefined, noHas, [PERM_USER_MANAGE])).toBe(false);
});

test('a user.manage-only custom role sees the Users tab but not Roles', () => {
	const noRoles = () => false; // holds no system role at all
	const onlyUserManage = (perm: string) => perm === PERM_USER_MANAGE;
	const settingsGroup = NAV.find(
		(e): e is NavGroup => e.kind === 'group' && e.label === 'Settings'
	)!;
	const visible = visibleChildren(settingsGroup, noRoles, onlyUserManage);
	expect(visible.map((c) => c.href)).toContain('/admin?tab=users');
	expect(visible.map((c) => c.href)).not.toContain('/admin?tab=roles');
	// The group itself must therefore still be visible (isEntryVisible), even
	// though the holder has no system role at all.
	expect(isEntryVisible(settingsGroup, noRoles, onlyUserManage)).toBe(true);
});

test('retention, access-review, and privacy nav entries match their backend RBAC', () => {
	// GET/PUT /api/retention-policy and the /api/privacy surface are admin-only;
	// /api/access-reviews is require_roles(ADMIN, CFO) — see nav.ts's own
	// comments beside each entry.
	const retention = kid('/admin/retention');
	const accessReview = kid('/admin/access-review');
	const privacy = kid('/admin/privacy');
	expect(retention.roles).toEqual(['admin']);
	expect(accessReview.roles).toEqual(['admin', 'cfo']);
	expect(privacy.roles).toEqual(['admin']);

	const hasAdmin = (...roles: string[]) => roles.includes('admin');
	const hasCfo = (...roles: string[]) => roles.includes('cfo');
	const hasClerk = (...roles: string[]) => roles.includes('ap_clerk');

	expect(canSee(retention.roles, hasAdmin)).toBe(true);
	expect(canSee(retention.roles, hasCfo)).toBe(false);
	expect(canSee(accessReview.roles, hasAdmin)).toBe(true);
	expect(canSee(accessReview.roles, hasCfo)).toBe(true);
	expect(canSee(accessReview.roles, hasClerk)).toBe(false);
	expect(canSee(privacy.roles, hasAdmin)).toBe(true);
	expect(canSee(privacy.roles, hasCfo)).toBe(false);
});

// --- Procurement: the nav is gated PER ITEM against each route's own backend
// gate, never one blanket group role list. -----------------------------------

const procurement = NAV.find(
	(e): e is NavGroup => e.kind === 'group' && e.label === 'Procurement'
)!;

/** The Procurement links a holder of exactly this one system role can see. */
function procurementFor(role: string): string[] {
	const has = (...roles: string[]) => roles.includes(role);
	return visibleChildren(procurement, has, () => false).map((c) => c.href);
}

test('an ap_clerk sees exactly the Procurement links their backend reads allow', () => {
	// The whole point of gating this group per item. Six of the seven routes have
	// list/detail reads open to any authenticated user or to all four roles:
	//   - /purchase-orders  api/purchase_orders.py — list, /counts, detail are
	//                       all `get_current_user`; only POST /sync-erp is
	//                       admin | ap_manager, and the page gates that button
	//                       on `auth.isManager`.
	//   - /goods-receipts   api/goods_receipts.py — list + detail
	//                       `get_current_user`; the Inspections tab's
	//                       `GET /api/inspections` likewise.
	//   - /requisitions, /intake, /catalogs — `require_roles(ADMIN, AP_MANAGER,
	//                       AP_CLERK, CFO)` on every read.
	//   - /gl-accounts      api/gl_accounts.py — `GET ""` is `get_current_user`
	//                       (a clerk coding an invoice has to be able to look a
	//                       code up, and the GL pickers already serve them the
	//                       same list); both writes — `POST ""` and
	//                       `POST /sync-erp` — are admin | ap_manager and are
	//                       gated in-page on `auth.isManager`. Gating the ROW on
	//                       the write instead is what left `sync-erp` reachable
	//                       from nowhere but admin-only `/organization`
	//                       (decisions §161).
	// /budgets is the one genuine exclusion: `require_roles(ADMIN, AP_MANAGER,
	// CFO)` on every read, so a clerk's FIRST page load would 403 — the nav must
	// keep hiding it. A group-level gate cannot express both halves, which is
	// why this asserts the exact set rather than a subset.
	expect(procurementFor('ap_clerk')).toEqual([
		'/purchase-orders',
		'/goods-receipts',
		'/requisitions',
		'/intake',
		'/catalogs',
		'/gl-accounts'
	]);
});

test('the other three system roles see every Procurement link', () => {
	// admin / ap_manager / cfo are on every one of the seven gates, so a
	// per-item split must not have narrowed anyone by accident.
	const all = procurement.children.map((c) => c.href);
	expect(procurementFor('admin')).toEqual(all);
	expect(procurementFor('ap_manager')).toEqual(all);
	expect(procurementFor('cfo')).toEqual(all);
});

test('Procurement nav entries carry their own gate, not the group\'s', () => {
	// Reading the roles directly, so a future edit that re-flattens the group
	// onto one shared list fails here rather than only in the derived sets above.
	const child = (href: string) => procurement.children.find((c) => c.href === href)!;
	const openToAll = ['admin', 'ap_manager', 'ap_clerk', 'cfo'];
	expect(child('/purchase-orders').roles).toEqual(openToAll);
	expect(child('/goods-receipts').roles).toEqual(openToAll);
	expect(child('/requisitions').roles).toEqual(openToAll);
	expect(child('/intake').roles).toEqual(openToAll);
	expect(child('/catalogs').roles).toEqual(openToAll);
	expect(child('/gl-accounts').roles).toEqual(openToAll);
	expect(child('/budgets').roles).toEqual(['admin', 'ap_manager', 'cfo']);
});

test('the Payments nav row admits a permission-only role its backend now serves', () => {
	// `nav.ts` OR's `payment.execute` / `payment.void` into the Payments row so a
	// custom role holding one of them (and no system role) can reach the page.
	// The page's mount effect fires `/api/payments/summary`, `/api/payments/queue`
	// and — on the default tab — `/queue/ids` unconditionally, and all three sat
	// on `require_roles(ADMIN, AP_MANAGER, CFO)`, so that row led to three 403s
	// on first paint. They now gate on the same any-of permission pair as the
	// `GET /api/payments` reads beside them (backend
	// `tests/test_sod_endpoint_wiring.py` pins it), which reproduces the prior
	// matrix exactly for the four system roles.
	const payments = link('/payments');
	expect(payments.permissions).toEqual([PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID]);
	const noRoles = () => false;
	const onlyVoid = (perm: string) => perm === PERM_PAYMENT_VOID;
	expect(canSee(payments.roles, noRoles, payments.permissions, onlyVoid)).toBe(true);
});
