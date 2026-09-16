import { describe, expect, it } from 'vitest';

import { splitTokens } from './segments';

/** Render segments the way `LinkedMessage.svelte` does, for readable assertions. */
function render(template: string, tokens: string[], labels: Record<string, string>): string {
	return splitTokens(template, tokens)
		.map((s) => (s.token ? `[${labels[s.token]}]` : s.text))
		.join('');
}

describe('splitTokens', () => {
	it('returns the whole string as one segment when it carries no token', () => {
		expect(splitTokens('No links here.', ['terms'])).toEqual([{ text: 'No links here.' }]);
	});

	it('splits around a single token', () => {
		expect(splitTokens('Read the {terms} first.', ['terms'])).toEqual([
			{ text: 'Read the ' },
			{ text: '', token: 'terms' },
			{ text: ' first.' },
		]);
	});

	it('preserves the order the TRANSLATION puts the links in, not the order of `tokens`', () => {
		// This is the whole reason the splitter exists. Fragment-per-link
		// catalogue entries fix the sequence at the call site; a language that
		// moves the clause then cannot be translated without either reordering
		// the sentence wrongly or losing a link.
		const labels = { terms: 'Terms', privacy: 'Privacy' };
		expect(render('Agree to the {terms} and the {privacy}.', ['terms', 'privacy'], labels)).toBe(
			'Agree to the [Terms] and the [Privacy].',
		);
		expect(render('{privacy} und {terms} gelten.', ['terms', 'privacy'], labels)).toBe(
			'[Privacy] und [Terms] gelten.',
		);
	});

	it('handles a token at either end without emitting an empty text segment', () => {
		expect(splitTokens('{terms} apply.', ['terms'])).toEqual([
			{ text: '', token: 'terms' },
			{ text: ' apply.' },
		]);
		expect(splitTokens('See the {terms}', ['terms'])).toEqual([
			{ text: 'See the ' },
			{ text: '', token: 'terms' },
		]);
	});

	it('leaves an unknown placeholder visible rather than swallowing it', () => {
		// A stray `{foo}` a reader can see is a bug someone reports. One this
		// dropped silently is a link that vanished from a consent line in one
		// locale, which is the failure mode that matters here.
		expect(render('Agree to the {terms} and {foo}.', ['terms'], { terms: 'Terms' })).toBe(
			'Agree to the [Terms] and {foo}.',
		);
	});

	it('renders a token repeated in one message every time it appears', () => {
		expect(render('{terms} — see {terms}.', ['terms'], { terms: 'Terms' })).toBe(
			'[Terms] — see [Terms].',
		);
	});

	it('matches a token literally, so regex characters in the text are safe', () => {
		expect(render('Cost (in $) — {terms}?', ['terms'], { terms: 'Terms' })).toBe(
			'Cost (in $) — [Terms]?',
		);
	});
});
