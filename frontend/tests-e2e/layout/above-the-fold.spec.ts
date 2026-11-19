import type { Locator, Page } from '@playwright/test';

import { en } from '#lib/i18n/locales/en.ts';
import { enPortal } from '#lib/i18n/locales/portal/en.ts';
import { enSettings } from '#lib/i18n/locales/settings/en.ts';
import { NAV } from '#lib/nav.ts';

import { expect, NO_TENANT_BASE, test } from '../fixtures/helpers';

/**
 * Above the fold — every page's heading, primary action and first input or
 * filter are on screen when it opens, with no scrolling.
 *
 * Every other spec fills fields and clicks buttons by locator, and Playwright
 * scrolls to them first, so a page whose search box or primary action ended up
 * below the fold still passed. #642 found exactly that on /login (a global
 * class pushed the form off screen) and pinned it there; this table extends
 * the check to every sidebar page and the three public entry pages.
 *
 * `toBeInViewport()` never scrolls: it reads the element's box against the
 * window as the page first lays out. Each row names its three locators by role
 * and accessible name. A page with no page-level action, or no filter, says so
 * with the reason, so "nothing to check" is a decision on the page rather than
 * a gap in the table.
 *
 * The rows are keyed by `NAV` href (`lib/nav.ts`), and the drift test fails if
 * a sidebar page has no row or a row names a page the sidebar no longer has —
 * the same contract `lib/help/content.test.ts` holds `PAGE_HELP` to.
 *
 * At 390px the shell collapses the sidebar to its 60px rail (`+layout.svelte`'s
 * `(max-width: 700px)` query), so every locator is scoped to `#main-content`:
 * the assertion is about the page, never the nav.
 */

const MESSAGES: Record<string, string> = { ...en, ...enSettings, ...enPortal };

/** English text for a message key; fails loudly on a key that does not exist. */
function t(key: string): string {
	const text = MESSAGES[key];
	if (text === undefined) throw new Error(`above-the-fold: no English message for '${key}'`);
	return text;
}

type Find = (scope: Locator) => Locator;
/** A locator, or the reason the page has none. */
type Slot = Find | { none: string };

interface Row {
	/** The `<h1>`, by message key, or a literal for a page that is not localized. */
	heading: { key: string } | { text: string };
	action: Slot;
	filter: Slot;
}

const button = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('button', { name }).first();
};
const buttonText = (text: string): Find => (s) => s.getByRole('button', { name: text }).first();
const textbox = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('textbox', { name, exact: true });
};
const combobox = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('combobox', { name });
};
const tablist = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('tablist', { name, exact: true });
};
const group = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('group', { name, exact: true });
};
const nav = (key: string): Find => {
	const name = t(key);
	return (s) => s.getByRole('navigation', { name, exact: true });
};
/** A `FilterChips` row's leading "All" chip (its name carries a live count). */
const ALL_CHIP = new RegExp(`^${t('common.all')}\\b`);
const allChip: Find = (s) => s.getByRole('button', { name: ALL_CHIP }).first();

const ROWS: Record<string, Row> = {
	'/': {
		heading: { key: 'dashboard.title' },
		action: { none: 'a read-only overview: every action lives on the page it summarizes' },
		filter: { none: 'scoped by the sidebar entity switcher, which is nav, not page content' }
	},
	'/invoices': {
		heading: { key: 'invoices.title' },
		action: button('invoices.action.upload'),
		filter: textbox('invoices.search.aria')
	},
	'/payments': {
		heading: { key: 'payments.title' },
		action: { none: '"Review and pay" exists only once queue rows are selected' },
		filter: tablist('payments.title')
	},
	'/vendors': {
		heading: { key: 'vendors.title' },
		action: button('vendors.action.newVendor'),
		filter: textbox('vendors.search.aria')
	},
	'/vendors/screening': {
		heading: { key: 'vendors.screening.queue.title' },
		action: button('vendors.screening.queue.refresh'),
		filter: textbox('vendors.screening.queue.search.aria')
	},
	'/vendors/change-requests': {
		heading: { key: 'vendors.changeRequests.title' },
		action: button('vendors.changeRequests.refresh'),
		filter: allChip
	},
	'/exceptions': {
		heading: { key: 'exceptions.title' },
		action: { none: 'work is per exception (Resolve on each row, or the bulk bar once rows are selected)' },
		filter: textbox('exceptions.search.aria')
	},
	'/purchase-orders': {
		heading: { key: 'purchaseOrders.title' },
		action: button('purchaseOrders.action.syncErp'),
		filter: textbox('purchaseOrders.search.aria')
	},
	'/goods-receipts': {
		heading: { key: 'goodsReceipts.title' },
		action: button('goodsReceipts.record.action'),
		filter: tablist('goodsReceipts.title')
	},
	'/requisitions': {
		heading: { key: 'requisitions.title' },
		action: button('requisitions.action.new'),
		filter: textbox('requisitions.search.aria')
	},
	'/intake': {
		heading: { key: 'intake.title' },
		action: button('intake.action.new'),
		filter: textbox('intake.search.aria')
	},
	'/catalogs': {
		heading: { key: 'catalogs.title' },
		action: button('catalogs.action.new'),
		filter: textbox('catalogs.search.aria')
	},
	'/gl-accounts': {
		heading: { key: 'glAccounts.title' },
		action: button('glAccounts.action.new'),
		filter: textbox('glAccounts.search.aria')
	},
	'/budgets': {
		heading: { key: 'budgets.title' },
		action: button('budgets.action.new'),
		filter: textbox('budgets.search.aria')
	},
	'/contracts': {
		heading: { key: 'contracts.title' },
		action: button('contracts.action.new'),
		filter: textbox('contracts.search.aria')
	},
	'/expenses': {
		heading: { key: 'expenses.title' },
		action: button('expenses.action.newExpense'),
		filter: tablist('expenses.title')
	},
	'/credit-memos': {
		heading: { key: 'creditMemos.title' },
		action: button('creditMemos.new'),
		filter: textbox('creditMemos.search.aria')
	},
	'/discounts': {
		heading: { key: 'discounts.title' },
		action: button('discounts.bulk.open'),
		filter: textbox('discounts.opt.budgetAria')
	},
	'/recurring': {
		heading: { key: 'recurring.title' },
		action: button('recurring.action.new'),
		filter: textbox('recurring.search.aria')
	},
	'/vendor-statements': {
		heading: { key: 'vendorStatements.title' },
		action: button('vendorStatements.new'),
		filter: textbox('vendorStatements.searchAria')
	},
	'/positive-pay': {
		heading: { key: 'positivePay.title' },
		action: button('positivePay.action.generate'),
		filter: textbox('positivePay.search.aria')
	},
	'/bank-reconciliation': {
		heading: { key: 'bankRecon.title' },
		action: button('bankRecon.action.import'),
		filter: tablist('bankRecon.title')
	},
	'/assistant': {
		heading: { key: 'assistant.title' },
		action: button('assistant.newChat'),
		filter: textbox('assistant.composer.ariaLabel')
	},
	'/cfo': {
		heading: { key: 'cfo.title' },
		action: button('cfo.exportCsv'),
		filter: group('cfo.control.granularity')
	},
	'/cash-flow': {
		heading: { key: 'cashFlow.title' },
		action: button('cashFlow.newChat'),
		filter: textbox('cashFlow.composer.ariaLabel')
	},
	'/tax': {
		heading: { key: 'tax.title' },
		action: button('tax.fileButton'),
		filter: combobox('tax.taxYear')
	},
	'/reports': {
		heading: { key: 'reports.title' },
		action: button('reports.action.save'),
		filter: combobox('reports.dataSource')
	},
	'/workflows': {
		heading: { key: 'workflows.list.title' },
		action: button('workflows.list.newWorkflow'),
		filter: { none: 'a short list of definitions with no search or filter control' }
	},
	'/experiments': {
		heading: { key: 'experiments.title' },
		action: button('experiments.new'),
		filter: allChip
	},
	'/adaptive': {
		heading: { key: 'adaptive.title' },
		action: { none: 'suggestions are accepted or dismissed one by one; there is no page-level action' },
		filter: tablist('adaptive.title')
	},
	'/audit': {
		heading: { key: 'audit.title' },
		action: button('audit.downloadCsv'),
		filter: group('audit.queryMode')
	},
	'/admin/access-review': {
		heading: { key: 'admin.accessReview.title' },
		action: button('admin.accessReview.action.acknowledge'),
		filter: { none: 'a fixed report over every user; it has no filter by design' }
	},
	'/admin/retention': {
		// Not localized yet: the page's copy is literal English.
		heading: { text: 'Retention Policy' },
		action: buttonText('Save changes'),
		filter: (s) => s.getByRole('spinbutton', { name: 'Invoices', exact: true })
	},
	'/admin/privacy': {
		heading: { key: 'admin.privacy.title' },
		action: button('admin.privacy.action.export'),
		filter: combobox('admin.privacy.field.subjectType')
	},
	'/organization': {
		heading: { key: 'org.title' },
		action: { none: 'each settings section saves itself; the default section (Getting started) is a checklist' },
		filter: nav('org.rail.label')
	},
	'/billing': {
		heading: { key: 'billing.title' },
		action: { none: 'plan changes start from the current-plan card, which depends on the subscription state' },
		filter: { none: 'one subscription per workspace; nothing to filter' }
	},
	'/admin?tab=users': {
		heading: { key: 'admin.usersRoles.title' },
		action: button('admin.usersRoles.inviteUser'),
		filter: textbox('admin.users.search.aria')
	},
	'/admin?tab=roles': {
		heading: { key: 'admin.usersRoles.title' },
		action: button('admin.usersRoles.createRole'),
		filter: { none: 'the roles list is short (four system roles plus custom ones) and unfiltered' }
	},
	'/admin/entities': {
		heading: { key: 'admin.entities.title' },
		action: button('admin.entities.createEntity'),
		filter: { none: 'a short list of legal entities with no search or filter control' }
	},
	'/admin/partner': {
		heading: { key: 'admin.partner.title' },
		action: button('admin.partner.createChild'),
		filter: { none: 'the child-tenant list has no search or filter control' }
	},
	'/admin/api-keys': {
		heading: { key: 'admin.apiKeys.title' },
		action: button('admin.apiKeys.createKey'),
		filter: { none: 'a short list of keys with no search or filter control' }
	},
	'/admin/webhooks': {
		heading: { key: 'admin.webhooks.title' },
		action: button('admin.webhooks.createWebhook'),
		filter: { none: 'the only filter is the delivery log\'s, which belongs to the section below the subscriptions' }
	},
	'/admin/health': {
		heading: { key: 'sweepHealth.title' },
		action: button('sweepHealth.refresh'),
		filter: { none: 'a fixed status board of every sweep; nothing to filter' }
	}
};

const VIEWPORTS = [
	{ width: 1280, height: 720 },
	{ width: 1366, height: 768 },
	{ width: 390, height: 844 }
] as const;

const NAV_HREFS = NAV.flatMap((e) => (e.kind === 'link' ? [e.href] : e.children.map((c) => c.href)));

function headingText(row: Row): string {
	return 'key' in row.heading ? t(row.heading.key) : row.heading.text;
}

async function expectOnScreen(scope: Locator, row: Row, what: string): Promise<void> {
	await expect(
		scope.getByRole('heading', { level: 1, name: headingText(row), exact: true }),
		`${what}: heading`
	).toBeInViewport();
	for (const [slot, pick] of [
		['primary action', row.action],
		['first input or filter', row.filter]
	] as const) {
		if (typeof pick === 'function') await expect(pick(scope), `${what}: ${slot}`).toBeInViewport();
	}
}

/** Keep the consent banner from covering the bottom of a short window. */
async function acceptConsent(page: Page): Promise<void> {
	await page.addInitScript(() => {
		try {
			localStorage.setItem('feoh_consent_choice', 'accepted');
		} catch {
			// Storage blocked: the banner shows, and the assertions still hold.
		}
	});
}

test.describe('above the fold — the row table', () => {
	test('every sidebar page has a row, and every row is a sidebar page', () => {
		const missing = NAV_HREFS.filter((href) => !(href in ROWS));
		const stale = Object.keys(ROWS).filter((href) => !NAV_HREFS.includes(href));
		expect(missing, 'sidebar pages with no above-the-fold row').toEqual([]);
		expect(stale, 'rows for pages no longer in the sidebar').toEqual([]);
	});

	test('every message key a row names exists', () => {
		// `t` throws on an unknown key. The locator helpers resolve theirs when
		// the table is built, so a renamed key fails the file by name; the
		// headings resolve here.
		for (const row of Object.values(ROWS)) headingText(row);
	});
});

for (const viewport of VIEWPORTS) {
	const size = `${viewport.width}x${viewport.height}`;

	test.describe(`above the fold at ${size} — sidebar pages`, () => {
		test.use({ viewport });

		for (const [href, row] of Object.entries(ROWS)) {
			test(`${href} shows its heading, action and filter without scrolling`, async ({ page }) => {
				await acceptConsent(page);
				await page.goto(href);
				await expectOnScreen(page.locator('#main-content'), row, `${href} @ ${size}`);
			});
		}
	});

	test.describe(`above the fold at ${size} — entry pages`, () => {
		test.use({ viewport, storageState: { cookies: [], origins: [] } });

		test('/portal/login shows its heading, sign-in and email field', async ({ page }) => {
			await acceptConsent(page);
			await page.goto('/portal/login');
			// The heading is the tenant's product name (white-label), so it is
			// matched by level rather than by a fixed string.
			await expect(page.getByRole('heading', { level: 1 })).toBeInViewport();
			await expect(page.getByRole('button', { name: t('portal.login.signIn'), exact: true })).toBeInViewport();
			await expect(page.getByRole('textbox', { name: t('portal.login.email'), exact: true })).toBeInViewport();
		});

		test('/login/reset-password (set-password link) shows its heading, submit and first field', async ({
			page
		}) => {
			await acceptConsent(page);
			// A real token needs the invite email; a stubbed check renders the
			// live form, as the axe sweep does for the portal's copy.
			await page.route(
				(url) => url.pathname === '/api/auth/reset-password/check',
				(route) => route.fulfill({ json: { email: 'invitee@acme.example' } })
			);
			await page.goto('/login/reset-password?token=above-the-fold&mode=set');
			await expect(
				page.getByRole('heading', { level: 1, name: t('auth.resetPassword.setHeading') })
			).toBeInViewport();
			await expect(page.getByRole('button', { name: t('auth.resetPassword.setSubmit'), exact: true })).toBeInViewport();
			await expect(page.getByLabel(t('auth.resetPassword.newPassword'), { exact: true })).toBeInViewport();
		});
	});

	test.describe(`above the fold at ${size} — signup`, () => {
		test.use({ viewport, storageState: { cookies: [], origins: [] }, baseURL: NO_TENANT_BASE });

		test('/signup shows its heading and first field', async ({ page }) => {
			await acceptConsent(page);
			await page.goto('/signup');
			await expect(page.getByRole('heading', { level: 1, name: t('auth.signup.heading') })).toBeInViewport();
			await expect(page.getByRole('textbox', { name: t('auth.signup.companyName') })).toBeInViewport();
			// Primary action: none above the fold by design. Submit closes a
			// multi-field form, so it sits after the last field; the first
			// field is where the page's work starts.
		});
	});
}
