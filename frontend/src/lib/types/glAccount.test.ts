import { describe, expect, it } from 'vitest';
import {
	GL_ACCOUNT_TYPE_LABEL_KEYS,
	GL_ACCOUNT_TYPES,
	glAccountOptionLabel,
	glAccountTypeLabelKey,
	type GlAccountOption,
	type GlAccountScope
} from './glAccount';

const UNKNOWN = 'Another entity';

function option(over: Partial<GlAccountOption> = {}): GlAccountOption {
	return {
		id: 'acct-1',
		code: '6000',
		name: 'Office Supplies',
		account_type: 'expense',
		entity_id: null,
		...over
	};
}

const NORTHWIND = { id: 'ent-uk', name: 'Northwind UK' };
const CONTOSO = { id: 'ent-us', name: 'Contoso US' };

const SINGLE: GlAccountScope = { multiEntity: false, entities: [NORTHWIND] };
const MULTI: GlAccountScope = { multiEntity: true, entities: [NORTHWIND, CONTOSO] };

describe('glAccountOptionLabel', () => {
	it('renders code only by default, and code — name on request', () => {
		expect(glAccountOptionLabel(option(), SINGLE, { unknownEntity: UNKNOWN })).toBe('6000');
		expect(
			glAccountOptionLabel(option(), SINGLE, { withName: true, unknownEntity: UNKNOWN })
		).toBe('6000 — Office Supplies');
	});

	it('leaves a shared account bare — NULL entity_id means every chart', () => {
		expect(
			glAccountOptionLabel(option({ entity_id: null }), MULTI, { unknownEntity: UNKNOWN })
		).toBe('6000');
	});

	it('leaves an entity-scoped account bare on a single-entity tenant', () => {
		// Same gate as the /gl-accounts Scope column: with one entity the
		// distinction has no consequence, so the suffix would be noise.
		expect(
			glAccountOptionLabel(option({ entity_id: NORTHWIND.id }), SINGLE, {
				unknownEntity: UNKNOWN
			})
		).toBe('6000');
	});

	it('names the owning entity on a multi-entity tenant', () => {
		expect(
			glAccountOptionLabel(option({ entity_id: NORTHWIND.id }), MULTI, {
				unknownEntity: UNKNOWN
			})
		).toBe('6000 (Northwind UK)');
	});

	it('distinguishes two subsidiaries holding the same code — the whole point', () => {
		// The consolidated view returns every entity's chart at once, so two
		// legitimate `6000` rows arrive in one list. Before the suffix they were
		// two indistinguishable options that code to different accounts.
		const uk = option({ id: 'a', entity_id: NORTHWIND.id, name: 'Stationery' });
		const us = option({ id: 'b', entity_id: CONTOSO.id, name: 'Office Supplies' });
		const labels = [uk, us].map((a) =>
			glAccountOptionLabel(a, MULTI, { withName: true, unknownEntity: UNKNOWN })
		);
		expect(labels).toEqual([
			'6000 — Stationery (Northwind UK)',
			'6000 — Office Supplies (Contoso US)'
		]);
		expect(new Set(labels).size).toBe(2);
	});

	it('falls back to the supplied label for an entity the store does not carry', () => {
		// `entityStore.ensureLoaded` is best-effort — it swallows its failure so
		// the switcher simply does not render. A picker must still not claim an
		// entity-scoped account is shared, so it says "another entity" instead.
		expect(
			glAccountOptionLabel(option({ entity_id: 'ent-gone' }), MULTI, {
				unknownEntity: UNKNOWN
			})
		).toBe('6000 (Another entity)');
	});
});

describe('glAccountTypeLabelKey', () => {
	it('maps every documented type, and only those', () => {
		for (const t of GL_ACCOUNT_TYPES) {
			expect(glAccountTypeLabelKey(t)).toBe(GL_ACCOUNT_TYPE_LABEL_KEYS[t]);
		}
	});

	it('returns null for an absent or unrecognised type so the caller renders it raw', () => {
		// `account_type` is a free-form String(50) an ERP sync writes into.
		expect(glAccountTypeLabelKey(null)).toBeNull();
		expect(glAccountTypeLabelKey('')).toBeNull();
		expect(glAccountTypeLabelKey('contra-asset')).toBeNull();
	});
});
