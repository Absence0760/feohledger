import { parse } from 'svelte/compiler';
import { describe, expect, it } from 'vitest';

import { en } from '#lib/i18n/locales/en.ts';
import { messages as de } from '#lib/i18n/locales/de.ts';
import { messages as es } from '#lib/i18n/locales/es.ts';
import { messages as fr } from '#lib/i18n/locales/fr.ts';
import { messages as ja } from '#lib/i18n/locales/ja.ts';
import { messages as ptBR } from '#lib/i18n/locales/pt-BR.ts';
import {
	CORE_FEATURE_KEYS,
	FEATURE_LABEL_KEYS,
	FEATURED_PLAN_CODE,
	formatPlanPrice,
	pricingTiers,
	TAGLINE_KEYS,
	topPlanName,
} from './pricing.ts';
import { PLAN_CURRENCY, PLANS, type CatalogPlan } from './plans.generated.ts';
import pricingSource from '#lib/components/marketing/Pricing.svelte?raw';

/**
 * The public pricing grid must say exactly what the plan catalogue says
 * (docs/decisions.md §253, issue #426). `plans.generated.ts` is the catalogue
 * as `pnpm gen:pricing` wrote it; these check that the grid is a faithful
 * rendering of it and that no figure can enter the page any other way.
 *
 * The expected strings are computed here with `Intl` directly, as an oracle
 * independent of `formatMoney`, and never typed as literals — a catalogue
 * change must not need this file edited, only regenerated.
 */

const LOCALE = 'en-US';

function money(amount: string, whole: boolean): string {
	return new Intl.NumberFormat(LOCALE, {
		style: 'currency',
		currency: PLAN_CURRENCY,
		...(whole ? { minimumFractionDigits: 0, maximumFractionDigits: 0 } : {}),
	}).format(Number(amount));
}

describe('the pricing grid matches the generated catalogue', () => {
	const tiers = pricingTiers(PLANS, LOCALE);

	it('has one tier per catalogue plan, in catalogue order', () => {
		expect(tiers.map((t) => [t.code, t.name])).toEqual(PLANS.map((p) => [p.code, p.name]));
	});

	it.each(PLANS.map((plan, i) => [plan.code, plan, i] as const))(
		'%s: price, allowance and overage come from the catalogue',
		(_code, plan, i) => {
			const tier = tiers[i];
			const whole = Number.isInteger(Number(plan.monthlyPrice));
			expect(tier.price).toBe(money(plan.monthlyPrice, whole));
			expect(tier.included).toBe(plan.includedAiInvoices);
			expect(tier.includedLabel).toBe(
				new Intl.NumberFormat(LOCALE).format(plan.includedAiInvoices)
			);
			expect(tier.overage).toBe(
				plan.overageUnitPrice === null ? null : money(plan.overageUnitPrice, false)
			);
		}
	);

	it('every tier builds on the one before it, so "Everything in X, plus" is true', () => {
		for (let i = 1; i < PLANS.length; i++) {
			const missing = PLANS[i - 1].features.filter(
				(f) => !(PLANS[i].features as readonly string[]).includes(f)
			);
			expect(missing, `${PLANS[i].code} drops a feature ${PLANS[i - 1].code} grants`).toEqual(
				[]
			);
		}
	});

	it('the added-feature lists reassemble each plan’s full grant', () => {
		const seen: string[] = [];
		tiers.forEach((tier, i) => {
			seen.push(...tier.addedFeatures);
			expect(new Set(seen)).toEqual(new Set(PLANS[i].features.map((f) => FEATURE_LABEL_KEYS[f])));
			expect(tier.buildsOn).toBe(i === 0 ? null : PLANS[i - 1].name);
		});
	});

	it('the featured card and the Enterprise base name catalogue plans', () => {
		expect(PLANS.map((p) => p.code)).toContain(FEATURED_PLAN_CODE);
		expect(topPlanName()).toBe(PLANS[PLANS.length - 1].name);
	});

	it('follows a changed catalogue without any edit here', () => {
		// A synthetic catalogue proves the grid is computed, not memorised: a
		// fractional price keeps its cents (never rounded into another figure),
		// and an allowance renders grouped.
		const synthetic: CatalogPlan[] = [
			{
				code: 'x',
				name: 'X',
				monthlyPrice: '12.50',
				includedAiInvoices: 12345,
				overageUnitPrice: '0.125',
				trialDays: 0,
				features: ['sso'],
			},
		];
		const [tier] = pricingTiers(synthetic, LOCALE);
		expect(tier.price).toBe(money('12.50', false));
		expect(tier.includedLabel).toBe(new Intl.NumberFormat(LOCALE).format(12345));
		expect(tier.addedFeatures).toEqual([FEATURE_LABEL_KEYS.sso]);
		expect(formatPlanPrice('49.00', LOCALE)).toBe(money('49', true));
	});
});

describe('no figure reaches the page except through the catalogue', () => {
	const pricingKey = (k: string) => k.startsWith('marketing.pricing.');
	// Product names that contain digits are names, not figures.
	const PROPER_NOUNS = /Dynamics 365/g;
	const catalogues = { en, de, es, fr, ja, 'pt-BR': ptBR } as Record<string, Record<string, string>>;

	it.each(Object.keys(catalogues))('%s: pricing strings carry no digits', (locale) => {
		const offenders = Object.entries(catalogues[locale])
			.filter(([k]) => pricingKey(k))
			.filter(([, v]) => /\d/.test(v.replace(PROPER_NOUNS, '')))
			.map(([k, v]) => `${k}: ${v}`);
		expect(offenders).toEqual([]);
	});

	it('every label the grid uses exists in the English catalogue', () => {
		const keys = [
			...Object.values(FEATURE_LABEL_KEYS),
			...CORE_FEATURE_KEYS,
			...Object.values(TAGLINE_KEYS),
		];
		for (const key of keys) expect(en, key).toHaveProperty([key]);
	});

	it('the English copy promises no trial, seat price or annual plan', () => {
		// Each was on the old grid and none exists in the billing code
		// (followups #426): signup binds every workspace to the first plan and
		// nothing grants `trial_days`; plans are flat monthly, users unlimited.
		const copy = Object.entries(en)
			.filter(([k]) => pricingKey(k))
			.map(([, v]) => v)
			.join('\n');
		expect(copy).not.toMatch(/trial|per seat|annual|minimum/i);
	});

	it('Pricing.svelte reads the generated module and types no figure', () => {
		expect(pricingSource).toMatch(/#lib\/marketing\/pricing\.ts/);
		expect(pricingSource).toMatch(/#lib\/marketing\/plans\.generated\.ts/);
		// Template text and attribute literals: no digit anywhere a reader
		// could see one. Script figures would have to reach the template
		// through an expression, which the module checks above cover.
		const ast = parse(pricingSource, { modern: true }) as unknown as { fragment: unknown };
		const found: string[] = [];
		(function walk(node: unknown): void {
			if (node === null || typeof node !== 'object') return;
			if (Array.isArray(node)) return node.forEach(walk);
			const rec = node as Record<string, unknown>;
			if (rec.type === 'Text' && typeof rec.data === 'string' && /\d/.test(rec.data)) {
				found.push(rec.data.trim());
			}
			for (const [key, value] of Object.entries(rec)) {
				// `id="plan-{code}-name"` etc. are not copy; class names neither.
				if (key === 'attributes') continue;
				walk(value);
			}
		})(ast.fragment);
		expect(found).toEqual([]);
		// And no hand-typed money or allowance in the script either.
		const script = pricingSource.slice(0, pricingSource.indexOf('</script>'));
		expect(script).not.toMatch(/\$\d|['"`]\d[\d,.]*['"`]/);
	});
});
