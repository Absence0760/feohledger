import { WEB_ORIGIN } from './env';
import { E2E_TENANT_COUNT, tenantPsql } from './helpers';

/**
 * Workflow-definition shape guard — runs once, before any e2e test, in the
 * Playwright main process (see `globalSetup` in `playwright.config.ts`).
 *
 * Why this exists: `docs/known-issues.md` § "Workflow-mutating e2e specs can
 * strand a tenant on a disabled workflow definition". The workflow-mutating
 * specs (`tests-e2e/workflows/*.spec.ts`, `tests-e2e/workflow-builder.spec.ts`,
 * a few others that briefly flip a workflow definition's `is_active` / step
 * `enabled` flags) all restore the original state in a `finally` block — but a
 * hard interruption (killed process, machine crash, a Playwright *timeout*
 * whose continuation never gets scheduled) can still skip that restoration on
 * a long-lived local dev database. The next run then hits a confusing failure
 * three specs later — e.g. `POST /complete` walks `new -> done` with no
 * approval step, so a subsequent `/approve` 409s — with no obvious link back
 * to the real cause.
 *
 * This check reproduces the *symptom* the doc diagnosed instead: for every
 * tenant the suite will touch, assert there is exactly one `is_default=true`
 * workflow definition, it is `is_active=true`, and its `approval` +
 * `erp_export` steps are `enabled=true` (the shape `backend/scripts/seed.py`
 * creates). A tenant that fails this needs `python scripts/seed.py` re-run
 * (or the specific row restored) before the suite can give trustworthy
 * results — fail fast here with a clear message instead of letting a stray
 * flag surface as an inscrutable 409 deep into an unrelated spec.
 *
 * Deliberately synchronous + no Playwright fixtures: `globalSetup` runs
 * outside any test/worker context, so this reuses the same raw-`psql`
 * primitive (`tenantPsql`) the specs use for direct DB assertions, just
 * called with an explicit tenant slug instead of the worker-derived default.
 */

interface WorkflowStepShape {
	type: string;
	enabled: boolean;
}

interface WorkflowDefinitionRow {
	id: string;
	name: string;
	entity_id: string | null;
	is_active: boolean;
	steps_config: { steps?: WorkflowStepShape[] } | null;
}

const REQUIRED_ENABLED_STEP_TYPES = ['approval', 'erp_export'] as const;

function fetchDefaultWorkflowRows(slug: string): WorkflowDefinitionRow[] {
	const raw = tenantPsql(
		`SELECT COALESCE(json_agg(json_build_object(
			'id', id::text,
			'name', name,
			'entity_id', entity_id::text,
			'is_active', is_active,
			'steps_config', steps_config
		)), '[]'::json) FROM workflow_definitions WHERE is_default = true`,
		slug
	).trim();
	return raw ? (JSON.parse(raw) as WorkflowDefinitionRow[]) : [];
}

/** Returns human-readable problem strings for one tenant; empty = healthy. */
function verifyTenantWorkflowShape(slug: string): string[] {
	const dbName = `feoh_${slug}`;
	let rows: WorkflowDefinitionRow[];
	try {
		rows = fetchDefaultWorkflowRows(slug);
	} catch (err) {
		const message = err instanceof Error ? err.message.split('\n')[0] : String(err);
		return [`${slug}: could not query ${dbName} (${message}) — is Postgres up and seeded?`];
	}

	if (rows.length === 0) {
		return [`${slug}: no is_default=true workflow definition found in ${dbName}.`];
	}

	const problems: string[] = [];

	if (rows.length > 1) {
		const summary = rows
			.map((r) => `"${r.name}" (entity_id=${r.entity_id ?? 'shared'}, is_active=${r.is_active})`)
			.join(', ');
		problems.push(
			`${slug}: ${rows.length} workflow definitions are is_default=true — expected exactly 1. ` +
				`This is the accumulation pattern from docs/known-issues.md (an auto-created stub ` +
				`definition, typically named "Invoice Processing", left behind in a different entity ` +
				`scope while the seeded default was briefly deactivated). Found: ${summary}`
		);
	}

	for (const row of rows) {
		const label = `"${row.name}" (${row.id})`;
		if (!row.is_active) {
			problems.push(
				`${slug}: default workflow ${label} is is_active=false. A prior workflow-mutating e2e ` +
					`spec likely didn't finish its cleanup. Fix: re-run \`python scripts/seed.py\` for this ` +
					`tenant, or restore is_active=true on that row.`
			);
		}
		const steps = row.steps_config?.steps ?? [];
		for (const stepType of REQUIRED_ENABLED_STEP_TYPES) {
			const step = steps.find((s) => s.type === stepType);
			if (!step) {
				problems.push(`${slug}: default workflow ${label} has no "${stepType}" step.`);
			} else if (!step.enabled) {
				problems.push(
					`${slug}: default workflow ${label} has its "${stepType}" step disabled — same strand ` +
						`pattern as above (a mutating spec didn't restore step.enabled).`
				);
			}
		}
	}

	return problems;
}

/**
 * Confirms the origin under test is actually THIS app.
 *
 * `reuseExistingServer` is on locally, so Playwright attaches to whatever
 * already answers on `E2E_WEB_ORIGIN` instead of starting its own server. The
 * config comments anticipate a sibling *worktree* holding the port — but
 * nothing checked that the listener belongs to this project at all, and
 * another project on this machine (`~/github/threkir`) also serves vite on
 * 7777. A run that attached to it navigated a foreign app, found none of the
 * app shell, and reported failures that looked like this suite's own.
 *
 * The worse shape is the quiet one: a spec whose assertions are absence-based
 * can pass against a foreign document, so the suite goes GREEN without ever
 * loading the code under test.
 *
 * `og:site_name` is rendered from `src/app.html`, so it is in the served
 * document under both `vite dev` and `vite preview`, before any JavaScript
 * runs — which matters, because this app renders nothing until hydration and
 * so has no other server-visible marker.
 */
async function verifyOriginServesThisApp(): Promise<string[]> {
	let html: string;
	try {
		const response = await fetch(WEB_ORIGIN, { redirect: 'follow' });
		if (!response.ok) {
			return [`${WEB_ORIGIN} answered HTTP ${response.status}, so the app under test is not being served.`];
		}
		html = await response.text();
	} catch (error) {
		return [`${WEB_ORIGIN} could not be reached (${(error as Error).message}).`];
	}

	if (/og:site_name/i.test(html) && /FeohLedger/i.test(html)) return [];

	return [
		`${WEB_ORIGIN} is serving a DIFFERENT application — its document carries no ` +
			'FeohLedger `og:site_name`, which `src/app.html` puts in every response.\n' +
			'    Playwright reuses an existing server locally, so it attached to whatever ' +
			'already held that port rather than starting this app.\n' +
			'    Either stop the other process, or give this run its own stack:\n' +
			'      E2E_WEB_ORIGIN=http://localhost:7801 PUBLIC_API_URL=http://localhost:8001 ' +
			'E2E_TENANT_OFFSET=1 pnpm test:e2e'
	];
}

export default async function globalSetup(): Promise<void> {
	// Identity first: every check below reads the DATABASE, so all of them pass
	// happily while Playwright is pointed at someone else's web server. Naming
	// the wrong-origin case here is what stops that from reading as a pile of
	// unrelated spec failures.
	const wrongOrigin = await verifyOriginServesThisApp();
	if (wrongOrigin.length > 0) {
		throw new Error(`\nThe e2e suite is pointed at the wrong server:\n\n  - ${wrongOrigin[0]}\n`);
	}

	// Escape hatch for a run that deliberately doesn't have the e2e tenants
	// seeded yet (e.g. exercising a single non-tenant spec by hand).
	if (process.env.FEOH_E2E_SKIP_WORKFLOW_SHAPE_CHECK === 'true') return;

	const slugs = [
		'acme',
		'techflow',
		...Array.from({ length: Math.max(E2E_TENANT_COUNT, 1) }, (_, i) => `e2e${i + 1}`)
	];

	const problems = slugs.flatMap(verifyTenantWorkflowShape);
	if (problems.length === 0) return;

	throw new Error(
		'\nWorkflow-definition shape guard failed before any e2e test ran ' +
			'(see docs/known-issues.md § "Workflow-mutating e2e specs can strand a tenant on a ' +
			'disabled workflow definition"):\n\n' +
			problems.map((p) => `  - ${p}`).join('\n') +
			'\n'
	);
}
