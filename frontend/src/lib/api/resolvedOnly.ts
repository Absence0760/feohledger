/**
 * The not-loaded-means-null rule the preloaded `refusal` catalogue slice is
 * read under (`PRELOADED_SLICES` in `i18n/catalogues.ts`).
 *
 * That slice loads beside the main catalogue at startup but is not awaited
 * before the first paint, so a sentence asked for in that window has no text
 * yet, and `m()` signals that by handing the key back. {@link resolvedOnly}
 * wraps `t` to throw instead; the registry that reads the slice catches it and
 * answers `null`, and its caller shows the server's own sentence (or a stored
 * `last_error`) until the slice lands, never a raw `refusal.*` /
 * `erpRefusal.*` key. The readers are `codedRefusals.ts::codedRefusalText` and
 * `erpRefusals.ts::erpRefusalText`, the `PRELOADED_READERS` of
 * `i18n/slices.test.ts`.
 *
 * Imports nothing from the Svelte runtime or `$app/*`.
 */
import type { MessageKey } from '#lib/i18n/messages.ts';

type Translate = (key: MessageKey, params?: Record<string, string | number>) => string;

/** `t`, but throwing for a key it cannot resolve (`m()` returns the key itself). */
export function resolvedOnly(t: Translate): Translate {
	return (key, params) => {
		const text = t(key, params);
		if (text === key) throw new Error(`unresolved message key: ${key}`);
		return text;
	};
}
