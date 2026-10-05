import { describe, expect, it } from 'vitest';
import { en } from '#lib/i18n/locales/en.ts';
import {
	ACTION_LABEL_KEYS,
	AUTONOMY_LEVELS,
	AUTONOMY_LEVEL_LABEL_KEYS,
	agentActionLabelKey,
	autonomyLevelLabelKey
} from './exceptionAgents';

/**
 * Drift guard for the two vocabularies the AI-Agents panel renders as labels.
 *
 * Both were hardcoded English before — `ACTION_LABELS` as a map, the autonomy
 * level as the raw lowercase wire value printed straight into a table cell and
 * a definition list. Keying them makes the set TOTAL, so the thing to guard is
 * that the set stays the backend's: a coordinator that grows a fourth action,
 * or an autonomy tier nobody added wording for, must fail here rather than
 * render a blank badge beside a confidence figure.
 *
 * Reads the real Python through `import.meta.glob` (see
 * `notification.roster.test.ts` for why, rather than `node:fs`).
 */

const RAW = import.meta.glob(
	[
		'../../../../backend/app/services/exception_agents/base.py',
		'../../../../backend/app/services/exception_agents/autonomy.py'
	],
	{ query: '?raw', import: 'default', eager: true }
) as Record<string, string>;

function source(suffix: string): string {
	const hit = Object.entries(RAW).find(([path]) => path.endsWith(suffix));
	expect(hit, `${suffix} not found through import.meta.glob`).toBeDefined();
	return hit![1];
}

/** The `ACTION_* = "value"` constants — `base.py` calls itself their single source. */
function backendActions(): string[] {
	const py = source('exception_agents/base.py');
	return [...py.matchAll(/^ACTION_[A-Z_]+\s*=\s*"([a-z_]+)"/gm)].map((m) => m[1]);
}

/** The `_THRESHOLDS` keys — the autonomy levels an org may be set to. */
function backendAutonomyLevels(): string[] {
	const py = source('exception_agents/autonomy.py');
	const block = /_THRESHOLDS:\s*dict\[str, Decimal\]\s*=\s*\{([\s\S]*?)\n\}/.exec(py);
	expect(block, '_THRESHOLDS map not found — did it move or change shape?').not.toBeNull();
	return [...block![1].matchAll(/"([a-z_]+)":/g)].map((m) => m[1]);
}

describe('agent action vocabulary', () => {
	it('labels exactly the actions the coordinator can record', () => {
		const actions = backendActions();
		expect(actions, 'parsed no ACTION_* constants').toHaveLength(3);
		expect(Object.keys(ACTION_LABEL_KEYS).sort()).toEqual([...actions].sort());
	});

	it('resolves every action key in the English catalogue', () => {
		for (const key of Object.values(ACTION_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('returns null for an action this build has no wording for', () => {
		expect(agentActionLabelKey('escalated')).toBe('exceptions.agents.action.escalated');
		expect(agentActionLabelKey('quarantined')).toBeNull();
	});
});

describe('autonomy-level vocabulary', () => {
	it('carries exactly the levels the backend thresholds declare', () => {
		const levels = backendAutonomyLevels();
		expect(levels, 'parsed no autonomy levels').toHaveLength(3);
		expect([...AUTONOMY_LEVELS].sort()).toEqual([...levels].sort());
	});

	it('keeps the levels ordered lowest-authority first', () => {
		// `conservative` is "off" (unreachable threshold). The order is
		// documentation, not behaviour, but a reversed list would read as a claim
		// that `aggressive` is the safe default.
		expect([...AUTONOMY_LEVELS]).toEqual(['conservative', 'balanced', 'aggressive']);
	});

	it('resolves every autonomy key in the English catalogue', () => {
		for (const key of Object.values(AUTONOMY_LEVEL_LABEL_KEYS)) {
			expect(en, `${key} is missing from en.ts`).toHaveProperty(key);
		}
	});

	it('returns null for a level this build has no wording for', () => {
		// `resolve_autonomy_level` falls back to `conservative` when it STAMPS a
		// decision, but the column is a plain String(20) and historical rows are
		// read back as written — including the `supervised` the e2e fixtures use.
		expect(autonomyLevelLabelKey('balanced')).toBe('exceptions.agents.autonomy.balanced');
		expect(autonomyLevelLabelKey('supervised')).toBeNull();
	});
});
