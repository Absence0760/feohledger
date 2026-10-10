import { describe, expect, it } from 'vitest';
import { credentialUpdate } from './providerCredentials.ts';

describe('credentialUpdate', () => {
	it('sends nothing when no secret was typed or removed', () => {
		expect(
			credentialUpdate({
				api_key: { value: '', clear: false },
				client_secret: { value: '   ', clear: false }
			})
		).toBeNull();
	});

	it('sets typed values, trimmed, and leaves blanks out (blank keeps)', () => {
		expect(
			credentialUpdate({
				api_key: { value: ' new-key ', clear: false },
				client_secret: { value: '', clear: false }
			})
		).toEqual({ set: { api_key: 'new-key' } });
	});

	it('clears only fields with nothing typed', () => {
		expect(
			credentialUpdate({
				api_key: { value: '', clear: true },
				client_secret: { value: 'typed', clear: true }
			})
		).toEqual({ set: { client_secret: 'typed' }, clear: ['api_key'] });
	});
});
