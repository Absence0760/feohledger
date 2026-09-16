// Splitting a translated sentence around the links embedded in it.
//
// Some copy is a sentence with a link inside it — "By creating a workspace you
// agree to the Terms of Service and the Privacy Policy". There are two ways to
// translate that, and only one of them survives contact with a translator.
//
// The tempting one is to cut the sentence into `…Pre` / `…Post` catalogue
// entries and put the anchor between them at the call site. That works for a
// single insert (`auth.signup.footerPre` does it around a hostname) and breaks
// for more than one, because the fragments fix the ORDER the links appear in.
// German and Japanese both move the clause that carries them; a translator
// handed three fragments can only reproduce English word order or mistranslate.
//
// So the message stays one string with `{token}` markers in it, and this splits
// it back into segments the caller renders — text as text, a token as its link.
// The translation decides where each link lands. `interpolate()` deliberately
// leaves an unreferenced `{token}` intact, so a message routed through it keeps
// its markers, and `messages_parity.test.ts` already fails any locale whose
// placeholder set differs from English — which is what stops a translation
// silently dropping the Terms link from a consent line.

export interface MessageSegment {
	/** Literal text to render. Empty when this segment is a token. */
	text: string;
	/** The token name, when this segment stands in for a link. */
	token?: string;
}

/**
 * Split `template` on the `{token}` markers named in `tokens`.
 *
 * A marker that is not in `tokens` is left in the text verbatim rather than
 * dropped: an unknown `{foo}` reaching a reader is a visible bug, where a
 * silently swallowed one is an invisible missing link. Tokens are matched
 * literally, never as a pattern.
 */
export function splitTokens(template: string, tokens: readonly string[]): MessageSegment[] {
	const markers = tokens.map((token) => ({ token, marker: `{${token}}` }));
	const segments: MessageSegment[] = [];
	let rest = template;

	while (rest.length > 0) {
		// The earliest marker wins, so the order in `tokens` never affects the
		// result — only the order the translation put them in.
		let next: { token: string; marker: string; index: number } | undefined;
		for (const { token, marker } of markers) {
			const index = rest.indexOf(marker);
			if (index === -1) continue;
			if (!next || index < next.index) next = { token, marker, index };
		}

		if (!next) {
			segments.push({ text: rest });
			break;
		}

		if (next.index > 0) segments.push({ text: rest.slice(0, next.index) });
		segments.push({ text: '', token: next.token });
		rest = rest.slice(next.index + next.marker.length);
	}

	return segments;
}
