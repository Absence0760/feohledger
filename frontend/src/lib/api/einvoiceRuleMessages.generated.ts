// GENERATED FILE — do not edit by hand.
//
// Source of truth: backend/app/services/e_invoice/rule_catalog.py
// Regenerate:      pnpm gen:einvoice-messages
// Drift check:     pnpm check:einvoice-messages  (runs in CI)
//
// Maps every EN 16931 / PEPPOL rule id the backend's validators can
// emit to the message key that states it in the reader's language.
// decisions.md §95 removed the hand-written version because it covered
// four codes out of dozens and drifted; this one is derived, so it
// cannot.
import type { MessageKey } from '#lib/i18n/messages.ts';

/** Rule id → the localized sentence that explains it. */
export const E_INVOICE_RULE_MESSAGE_KEYS = {
	// A VAT breakdown group is required for every VAT category used on a line
	'BR-AE-01': 'invoices.modal.einvoice.rule.vatCategory01',
	// This VAT category requires a zero rate
	'BR-AE-05': 'invoices.modal.einvoice.rule.vatCategory05',
	// VAT category taxable amount must equal the sum of its invoice lines
	'BR-AE-08': 'invoices.modal.einvoice.rule.vatCategory08',
	// Invoice type code must be a member of UNTDID 1001
	'BR-CL-01': 'invoices.modal.einvoice.rule.brCl01',
	// Document currency must be a member of ISO 4217 (alphabetic)
	'BR-CL-03': 'invoices.modal.einvoice.rule.brCl03',
	// Country code must be a member of ISO 3166-1 alpha-2
	'BR-CL-14': 'invoices.modal.einvoice.rule.brCl14',
	// Payment means code is not shaped like a UNCL4461 code
	'BR-CL-16': 'invoices.modal.einvoice.rule.brCl16',
	// VAT category code must be a member of the UNCL5305 EN 16931 subset
	'BR-CL-17': 'invoices.modal.einvoice.rule.brCl17',
	// Invoiced item VAT category must be a member of the UNCL5305 EN 16931 subset
	'BR-CL-18': 'invoices.modal.einvoice.rule.brCl18',
	// Unit of measure is not shaped like a UN/ECE Rec 20 code
	'BR-CL-23': 'invoices.modal.einvoice.rule.brCl23',
	// VAT identifier must start with the ISO 3166-1 country prefix that issued it
	'BR-CO-09': 'invoices.modal.einvoice.rule.brCo09',
	// Sum of invoice line net amounts must equal the total of the lines
	'BR-CO-10': 'invoices.modal.einvoice.rule.brCo10',
	// Total without VAT must equal the line total less allowances plus charges
	'BR-CO-13': 'invoices.modal.einvoice.rule.brCo13',
	// Invoice total VAT must equal the sum of the VAT breakdown amounts
	'BR-CO-14': 'invoices.modal.einvoice.rule.brCo14',
	// Total with VAT must equal the total without VAT plus the total VAT
	'BR-CO-15': 'invoices.modal.einvoice.rule.brCo15',
	// Amount due for payment must equal the total with VAT
	'BR-CO-16': 'invoices.modal.einvoice.rule.brCo16',
	// VAT category tax amount must equal its taxable amount times its rate
	'BR-CO-17': 'invoices.modal.einvoice.rule.brCo17',
	// An invoice with an amount due requires a due date or payment terms
	'BR-CO-25': 'invoices.modal.einvoice.rule.brCo25',
	// The seller requires a legal registration or VAT identifier
	'BR-CO-26': 'invoices.modal.einvoice.rule.brCo26',
	'BR-E-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-E-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-E-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-G-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-G-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-G-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-IC-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-IC-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-IC-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-IG-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-IG-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-IG-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-IP-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-IP-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-IP-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-O-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-O-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-O-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-S-01': 'invoices.modal.einvoice.rule.vatCategory01',
	// A standard-rated line requires a rate above zero
	'BR-S-05': 'invoices.modal.einvoice.rule.brS05',
	'BR-S-08': 'invoices.modal.einvoice.rule.vatCategory08',
	'BR-Z-01': 'invoices.modal.einvoice.rule.vatCategory01',
	'BR-Z-05': 'invoices.modal.einvoice.rule.vatCategory05',
	'BR-Z-08': 'invoices.modal.einvoice.rule.vatCategory08',
	// Invoice line net amount must equal quantity times item net price
	'PEPPOL-EN16931-R120': 'invoices.modal.einvoice.rule.peppolEn16931R120',
} as const satisfies Record<string, MessageKey>;

/**
 * The codes the 422 body does NOT fold into `msg`, so a client that
 * flattens the payload cannot identify them and no map can name them.
 * Their own sentence already spells the problem out in words. Listed
 * rather than dropped so a new generic kind shows up as a diff here.
 */
export const E_INVOICE_OPAQUE_CODES = [
	'implausible',
	'inconsistent',
	'malformed',
	'missing',
] as const;
