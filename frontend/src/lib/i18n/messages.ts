import { en } from './locales/en';
import { enHelp } from './locales/help/en';

// The catalogue shape: every locale module is `... satisfies Messages`,
// so a missing or extra key is a compile error. Values are plain strings
// (en's value type widens to `string`, not literals) — translations are
// free to differ.
export type Messages = typeof en;

// The help-centre slice (`help.*` minus the chrome keys), shipped as a
// second, lazily loaded catalogue per locale — see `locales/help/en.ts`.
// Every help locale module is `... satisfies HelpMessages`.
export type HelpMessages = typeof enHelp;

/** A key of the main catalogue — what most of the app passes to `m()`. */
export type MessageKey = keyof Messages;

/** A key of the help-centre slice. */
export type HelpMessageKey = keyof HelpMessages;

/** Any key `m()` can resolve: the main catalogue or the help slice. */
export type AnyMessageKey = MessageKey | HelpMessageKey;
