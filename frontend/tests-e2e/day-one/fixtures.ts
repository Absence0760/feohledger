import { createHmac } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, type Page } from '@playwright/test';

/**
 * Shared pieces of the day-one suite (`journey.spec.ts`). Nothing here is seed
 * data: the stack this suite runs against (`scripts/day-one/stack.sh`) holds one
 * tenant and its admin, exactly what `deploy/add-tenant.sh` leaves behind, and
 * every other row is created through the app by the journey itself.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, '../../..');

/** What `stack.sh provision` writes: the first tenant and its admin's
 *  generated temporary password — the one add-tenant.sh prints. */
export interface DayOneTenant {
	slug: string;
	name: string;
	admin_email: string;
	admin_temp_password: string;
	mailpit_url: string;
}

export const STATE_FILE = resolve(
	process.env.DAY_ONE_STATE_DIR ?? resolve(REPO_ROOT, '.day-one-state'),
	'tenant.json'
);

/** The provisioned tenant, or `null` when no day-one stack is up. */
export function readTenant(): DayOneTenant | null {
	if (!existsSync(STATE_FILE)) return null;
	return JSON.parse(readFileSync(STATE_FILE, 'utf8')) as DayOneTenant;
}

/**
 * Production-shaped values (#575 § 2). Seed and e2e fixtures are short ASCII;
 * real first invoices are not, and #574's table overflow only showed with these.
 */
export const REAL = {
	// Non-ASCII, punctuation, and long enough to wrap any column it lands in.
	vendorName: 'Ångström Präzisionsinstrumente & Kalibrierdienste (Europa) GmbH',
	// Long, with the separators real numbering schemes use.
	invoiceNumber: 'INV-2026-10-EU/000123456789-CAL',
	poNumber: 'PO-4410',
	managerName: 'Priya Raghunathan-Okonkwo',
	managerEmail: 'accounts.payable.operations.manager@dayone-trading.example.com',
	// Tax-EXCLUSIVE: line amounts are net, the tax sits on the header only.
	subtotal: '1500.00',
	taxAmount: '285.00',
	total: '1785.00'
} as const;

/** Escape text for an XML element or attribute value. `&` first, or the
 *  entities the later replacements introduce would be escaped again. */
export function xmlEscape(text: string): string {
	return text
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&apos;');
}

function isoDate(d: Date): string {
	return d.toISOString().slice(0, 10);
}

/**
 * A UBL 2.1 invoice from a supplier the tenant has never heard of. Tax-exclusive,
 * as every UBL invoice is: each line's LineExtensionAmount is net, and the tax
 * is stated once, on the header (`TaxTotal`), with `TaxExclusiveAmount` +
 * `PayableAmount` beside it. It references a purchase order the tenant does not
 * have, which is the PO flag the AP Manager clears. Parsed by the deterministic
 * `einvoice` adapter, which a production install runs with no extraction key.
 */
export function ublInvoice(today = new Date()): Buffer {
	const due = new Date(today.getTime() + 30 * 86_400_000);
	const xml = `<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>${xmlEscape(REAL.invoiceNumber)}</cbc:ID>
  <cbc:IssueDate>${isoDate(today)}</cbc:IssueDate>
  <cbc:DueDate>${isoDate(due)}</cbc:DueDate>
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:DocumentCurrencyCode>USD</cbc:DocumentCurrencyCode>
  <cac:OrderReference><cbc:ID>${xmlEscape(REAL.poNumber)}</cbc:ID></cac:OrderReference>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>${xmlEscape(REAL.vendorName)}</cbc:Name></cac:PartyName>
    <cac:PostalAddress>
      <cbc:StreetName>Königsallee 92a</cbc:StreetName>
      <cbc:CityName>Düsseldorf</cbc:CityName>
      <cbc:PostalZone>40212</cbc:PostalZone>
      <cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country>
    </cac:PostalAddress>
    <cac:PartyTaxScheme>
      <cbc:CompanyID>DE811907980</cbc:CompanyID>
      <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
    </cac:PartyTaxScheme>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyName><cbc:Name>Day One Trading Co</cbc:Name></cac:PartyName>
    <cac:PostalAddress>
      <cbc:StreetName>1 Congress Ave</cbc:StreetName>
      <cbc:CityName>Austin</cbc:CityName>
      <cbc:PostalZone>78701</cbc:PostalZone>
      <cac:Country><cbc:IdentificationCode>US</cbc:IdentificationCode></cac:Country>
    </cac:PostalAddress>
  </cac:Party></cac:AccountingCustomerParty>
  <cac:PaymentMeans><cbc:PaymentMeansCode>30</cbc:PaymentMeansCode></cac:PaymentMeans>
  <cac:PaymentTerms><cbc:Note>Net 30 days</cbc:Note></cac:PaymentTerms>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="USD">${REAL.taxAmount}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="USD">${REAL.subtotal}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="USD">${REAL.taxAmount}</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID><cbc:Percent>19.00</cbc:Percent>
        <cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="USD">${REAL.subtotal}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="USD">${REAL.subtotal}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="USD">${REAL.total}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="USD">${REAL.total}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:ID>1</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">3</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="USD">1237.50</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>Kalibrierung Drehmomentschlüssel (ISO 6789)</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19.00</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="USD">412.50</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
  <cac:InvoiceLine>
    <cbc:ID>2</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="USD">262.50</cbc:LineExtensionAmount>
    <cac:Item>
      <cbc:Name>Prüfprotokoll &amp; Versand</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID><cbc:Percent>19.00</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="USD">262.50</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>
</Invoice>
`;
	return Buffer.from(xml, 'utf8');
}

// ── TOTP — RFC 6238, in-process (the same algorithm auth/mfa.spec.ts uses) ──

function base32Decode(input: string): Buffer {
	const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
	let bits = 0;
	let value = 0;
	const out: number[] = [];
	for (const ch of input.replace(/=+$/, '').toUpperCase()) {
		const idx = alphabet.indexOf(ch);
		if (idx === -1) continue;
		value = (value << 5) | idx;
		bits += 5;
		if (bits >= 8) {
			out.push((value >>> (bits - 8)) & 0xff);
			bits -= 8;
		}
	}
	return Buffer.from(out);
}

/** The 30-second step a time falls in. */
export function totpStep(at = Date.now()): number {
	return Math.floor(at / 30_000);
}

/** The TOTP code for one step. */
export function totpCode(secret: string, step: number): string {
	const counter = Buffer.alloc(8);
	counter.writeBigUInt64BE(BigInt(step));
	const hmac = createHmac('sha1', base32Decode(secret)).update(counter).digest();
	const offset = hmac[hmac.length - 1] & 0x0f;
	const bin =
		((hmac[offset] & 0x7f) << 24) |
		((hmac[offset + 1] & 0xff) << 16) |
		((hmac[offset + 2] & 0xff) << 8) |
		(hmac[offset + 3] & 0xff);
	return (bin % 1_000_000).toString().padStart(6, '0');
}

// ── Mailpit: the inbox the API's smtp adapter delivers to ──────────────────

interface MailpitSummary {
	ID: string;
	Subject: string;
	To: { Address: string }[];
}

/**
 * The text of the newest email to `to` whose subject matches, once it has
 * arrived. Delivery happens after the request that triggers it commits, so this
 * polls the inbox — the arrival of the message is the signal.
 */
export async function awaitEmail(
	page: Page,
	mailpit: string,
	to: string,
	subject: RegExp,
	after: Set<string> = new Set()
): Promise<{ id: string; text: string }> {
	let found: MailpitSummary | undefined;
	await expect
		.poll(
			async () => {
				const resp = await page.request.get(`${mailpit}/api/v1/messages?limit=50`);
				const body = (await resp.json()) as { messages: MailpitSummary[] };
				found = body.messages.find(
					(m) =>
						!after.has(m.ID) &&
						subject.test(m.Subject) &&
						m.To.some((t) => t.Address.toLowerCase() === to.toLowerCase())
				);
				return found?.ID ?? null;
			},
			{ message: `an email to ${to} matching ${subject}` }
		)
		.not.toBeNull();
	const message = await page.request.get(`${mailpit}/api/v1/message/${found!.ID}`);
	return { id: found!.ID, text: ((await message.json()) as { Text: string }).Text };
}

/** Every message id currently in the inbox — pass to `awaitEmail` as `after`
 *  to wait for a NEW message rather than match an older one. */
export async function inboxIds(page: Page, mailpit: string): Promise<Set<string>> {
	const resp = await page.request.get(`${mailpit}/api/v1/messages?limit=200`);
	const body = (await resp.json()) as { messages: MailpitSummary[] };
	return new Set(body.messages.map((m) => m.ID));
}

/** The page is no wider than the viewport: nothing — a long vendor name, an
 *  invoice number, an email in a cell — pushes the layout sideways (#574). A
 *  table may scroll inside its own wrapper; the page itself may not. */
export async function expectNoPageOverflow(page: Page): Promise<void> {
	const overflow = await page.evaluate(
		() => document.documentElement.scrollWidth - document.documentElement.clientWidth
	);
	expect(overflow, 'the page scrolls horizontally').toBeLessThanOrEqual(0);
}
