// Where a glossary term's words and entry live. Its own module, importing
// nothing at run time, so a <HelpTip> anywhere in the app can reach a term
// without pulling the help centre's prose (./content.ts) into that page.

import type { MessageKey } from '#lib/i18n/messages.ts';

/** The term's name in the reader's language. Every glossary id has one (content.test.ts). */
export const termKey = (id: string) => `help.term.${id}` as MessageKey;

/** Its one-line definition: what a HelpTip shows. */
export const termShortKey = (id: string) => `help.term.${id}.short` as MessageKey;

/** Its entry on the glossary page. */
export const termHref = (id: string) => `/help/glossary#${id}`;
