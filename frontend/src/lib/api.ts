import { getApiBase, getTenantSlug } from '#lib/tenant.ts';
import { getSelectedEntityId } from '#lib/entity.ts';
import { apiErrorCode, formatApiDetail } from '#lib/utils/apiError.ts';
import { localizeApiDetail } from '#lib/api/codedRefusals.ts';
import { m } from '#lib/i18n/store.svelte.ts';

// Re-exported so callers that already import from `#lib/api` (e.g. the
// hand-rolled fetch in `api/expenses.ts`) don't need a second import path.
export { formatApiDetail };

// The API origin is resolved PER REQUEST, not frozen in a module-level const,
// because it depends on the host the SPA is being served from: the build-time
// `PUBLIC_API_URL` on a platform host, same-origin (`''`) on a tenant's vanity
// custom domain, where only a same-origin request carries the vanity hostname
// in `Host` for the backend to resolve the tenant from. One owner —
// `#lib/tenant.ts::getApiBase` — so no call site re-derives it.

/** Raised by `request()` on any non-OK HTTP response. Carries the status code
 *  so a caller can branch on a specific status (e.g. a 409 "stale state"
 *  conflict) without parsing the message text — `AssistantBudgetError`
 *  already established the "extend Error with structured fields" pattern for
 *  its own special-cased 429; this is the general-purpose counterpart for
 *  everything routed through the shared `request()` helper. */
export class ApiError extends Error {
	status: number;
	/** The refusal's machine-readable code when the backend sent a coded
	 *  `detail` (`{code, message, params}`), else `null`. `message` is the
	 *  rendered sentence — localized already for a refusal the
	 *  `api/codedRefusals.ts` registry knows, otherwise the server's English.
	 *  BRANCH on this, never on `message`: the message is localized, so a
	 *  substring match on the English fails in every other locale
	 *  (`InvoiceModal`'s stale-edit prompt keys on `INVOICE_STALE_EDIT`). */
	code: string | null;
	/** The coded refusal's params; empty when there are none. */
	params: Record<string, unknown>;
	/** The response's raw `detail`, for a caller that needs its STRUCTURE.
	 *  `undefined` when the body carried none. */
	detail: unknown;
	constructor(
		message: string,
		status: number,
		code: string | null = null,
		params: Record<string, unknown> = {},
		detail?: unknown
	) {
		super(message);
		this.name = 'ApiError';
		this.status = status;
		this.code = code;
		this.params = params;
		this.detail = detail;
	}
}

/**
 * Build the `ApiError` for a non-OK response body. The message is the
 * reader's-language sentence when the `detail` is a structured refusal this
 * build can localize (`api/codedRefusals.ts::localizeApiDetail`), otherwise
 * `formatApiDetail`'s rendering of whatever the server sent — done HERE, once,
 * so every write path's toast is localized without a per-call-site change
 * (`frontend/CLAUDE.md` § Internationalization). Code / params / raw detail
 * ride along. Shared with `portalApi.ts`, so the two clients cannot disagree on
 * how a `detail` becomes an error.
 */
export function apiErrorFromBody(
	body: { detail?: unknown },
	fallback: string,
	status: number
): ApiError {
	const { code, params } = apiErrorCode(body.detail);
	const message = localizeApiDetail(body.detail, m) ?? formatApiDetail(body.detail, fallback);
	return new ApiError(message, status, code, params, body.detail);
}

function getToken(): string | null {
	if (typeof window === 'undefined') return null;
	return localStorage.getItem('auth_token');
}

export function setToken(token: string) {
	localStorage.setItem('auth_token', token);
}

export function clearToken() {
	localStorage.removeItem('auth_token');
}

export function hasToken(): boolean {
	return !!getToken();
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
	const token = getToken();
	const inHeaders = (init?.headers ?? {}) as Record<string, string>;
	const headers: Record<string, string> = {
		...( init?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
		...inHeaders,
	};
	if (token) {
		headers['Authorization'] = `Bearer ${token}`;
	}
	const tenant = getTenantSlug();
	if (tenant) {
		headers['X-Tenant-Slug'] = tenant;
	}
	// Multi-entity: scope to the selected subsidiary. Absent = consolidated.
	const entity = getSelectedEntityId();
	if (entity) {
		headers['X-Entity-ID'] = entity;
	}

	const res = await fetch(`${getApiBase()}${path}`, { ...init, headers });

	if (res.status === 401) {
		// Auto-redirect only fires when an existing session went stale.
		// For anonymous requests (e.g. /api/auth/login itself), 401
		// means "wrong credentials" — let the caller's catch handle it
		// so forms can render error banners instead of being torn down
		// mid-render by a navigation.
		if (token) {
			clearToken();
			window.location.href = '/login';
		}
		const body = await res.json().catch(() => ({}));
		throw apiErrorFromBody(body, 'Unauthorized', res.status);
	}

	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw apiErrorFromBody(body, `API error ${res.status}`, res.status);
	}

	if (res.status === 204) return undefined as T;
	return res.json();
}

async function fetchBlob(path: string): Promise<string> {
	// For binary endpoints (image / PDF) that <img src> and <iframe src>
	// can't reach because they don't carry the Bearer token. Caller is
	// responsible for `URL.revokeObjectURL` on the returned URL.
	const blob = await downloadBlob(path);
	return URL.createObjectURL(blob);
}

/** Shared tail of every blob download: the 401 clear-and-bounce `request()`
 *  performs, the backend's own `detail` when it sent one, then the body as a
 *  Blob. One owner so the GET and POST helpers can't drift on either. */
async function blobFromResponse(res: Response): Promise<Blob> {
	if (res.status === 401) {
		clearToken();
		window.location.href = '/login';
		throw new ApiError('Unauthorized', 401);
	}
	if (!res.ok) {
		const body = await res.json().catch(() => ({}));
		throw apiErrorFromBody(body, `Failed to load file: ${res.status}`, res.status);
	}
	return res.blob();
}

async function downloadBlob(path: string): Promise<Blob> {
	return blobFromResponse(await fetch(`${getApiBase()}${path}`, { headers: authHeaders() }));
}

/**
 * POST a JSON body and read the response back as a `Blob` — the file-returning
 * counterpart of `api.post`, for endpoints that take a selection in the body
 * and answer with a document (`POST /api/invoices/bulk/export`).
 *
 * Exists because `downloadBlob` is GET-only, which pushed such callers into
 * hand-rolling `fetch` — and a hand-rolled request silently loses whatever the
 * shared client does for everyone else. `/invoices`' bulk export lost two
 * things that way: `X-Entity-ID` (so the export wasn't scoped to the
 * subsidiary the selection was made under) and the 401 clear-and-bounce (an
 * expired session produced a cryptic failure toast instead of a re-login).
 * Composed from the same `authHeaders()` as `request` / `downloadBlob` / the
 * SSE stream helper, so it cannot drift from them again.
 *
 * Always resolves a `Blob` — a JSON-returning export is a `Blob` of JSON.
 * Callers that want the parsed value (e.g. to re-serialize it pretty-printed
 * for the downloaded file) read `await blob.text()`; formatting a download is a
 * presentation decision and stays with the caller, not in the transport layer.
 */
async function downloadBlobPost(path: string, body: unknown): Promise<Blob> {
	return blobFromResponse(
		await fetch(`${getApiBase()}${path}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...authHeaders() },
			body: JSON.stringify(body)
		})
	);
}

/** Compose the auth + tenant + entity headers shared by `request`, the blob
 *  helpers, and the SSE stream helper. Single source of truth so the streaming
 *  path can't drift from the JSON path (EventSource can't set these headers,
 *  which is why streaming uses `fetch` + a body reader instead). */
function authHeaders(): Record<string, string> {
	const headers: Record<string, string> = {};
	const token = getToken();
	if (token) headers['Authorization'] = `Bearer ${token}`;
	const tenant = getTenantSlug();
	if (tenant) headers['X-Tenant-Slug'] = tenant;
	const entity = getSelectedEntityId();
	if (entity) headers['X-Entity-ID'] = entity;
	return headers;
}

/** Raised by `streamAssistantChat` (and surfaced to callers) when the backend
 *  rejects the turn for exceeding the monthly AI token budget — HTTP 429 with
 *  `code: "assistant_budget_exceeded"`. Carries the budget figures so the page
 *  can render a friendly notice. */
export class AssistantBudgetError extends Error {
	used: number;
	budget: number;
	period: string;
	constructor(detail: string, used: number, budget: number, period: string) {
		super(detail);
		this.name = 'AssistantBudgetError';
		this.used = used;
		this.budget = budget;
		this.period = period;
	}
}

export interface StreamCallbacks {
	onTool?: (frame: { tool: string; args: Record<string, unknown>; result: Record<string, unknown> | null; error: string | null }) => void;
	onDelta?: (text: string) => void;
	onDone?: (payload: { conversation_id: string; answer: string; tool_invocations: unknown[]; usage: { input_tokens: number; output_tokens: number } }) => void;
	onError?: (frame: { code?: string; detail?: string; [k: string]: unknown }) => void;
}

/**
 * Stream a turn from `POST /api/assistant/chat/stream` (`text/event-stream`).
 *
 * Uses `fetch` + a `ReadableStream` reader (NOT `EventSource`, which can't set
 * the Authorization / tenant / entity headers). Parses SSE frames
 * (`event: <name>\ndata: <json>\n\n`) and dispatches to the callbacks.
 *
 * Throws `AssistantBudgetError` on a pre-stream HTTP 429, or a plain `Error`
 * on any other non-OK status / network failure / missing-body — the caller is
 * expected to fall back to the non-streaming `/chat` endpoint in that case.
 * Resolves once the stream ends (after a `done` or `error` frame).
 */
export async function streamAssistantChat(
	body: { message: string; conversation_id?: string },
	cb: StreamCallbacks,
	signal?: AbortSignal
): Promise<void> {
	return streamChatTurn('/api/assistant/chat/stream', body, cb, signal);
}

/**
 * Stream a turn from the AI Cash-Flow Copilot (`POST /api/cash-flow/copilot/stream`).
 *
 * The copilot rides the SAME orchestrator + SSE contract as the assistant
 * (`tool`/`delta`/`done`/`error` frames), so it reuses the exact SSE parser and
 * header logic below — only the endpoint path differs. Throws `AssistantBudgetError`
 * on a pre-stream HTTP 429, or a plain `Error` on any other non-OK / network
 * failure — the `/cash-flow` page falls back to the non-streaming `/copilot`.
 */
export async function streamCashFlowCopilot(
	body: { message: string; conversation_id?: string },
	cb: StreamCallbacks,
	signal?: AbortSignal,
	// `consolidated` runs the turn across every entity instead of the one
	// selected in the sidebar — a treasury question is usually a group
	// question. It rides the URL (not the body) because the backend reads it as
	// a query flag on both copilot routes.
	opts?: { consolidated?: boolean }
): Promise<void> {
	const path = opts?.consolidated
		? '/api/cash-flow/copilot/stream?consolidated=true'
		: '/api/cash-flow/copilot/stream';
	return streamChatTurn(path, body, cb, signal);
}

/** Shared SSE streaming client for the assistant + cash-flow copilot turns —
 *  one parser, one header path, so the two surfaces can't drift. */
async function streamChatTurn(
	path: string,
	body: { message: string; conversation_id?: string },
	cb: StreamCallbacks,
	signal?: AbortSignal
): Promise<void> {
	const headers: Record<string, string> = { 'Content-Type': 'application/json', ...authHeaders() };
	const res = await fetch(`${getApiBase()}${path}`, {
		method: 'POST',
		headers,
		body: JSON.stringify(body),
		signal
	});

	if (res.status === 401) {
		clearToken();
		window.location.href = '/login';
		throw new Error('Unauthorized');
	}
	if (res.status === 429) {
		const j = (await res.json().catch(() => ({}))) as Record<string, unknown>;
		throw new AssistantBudgetError(
			(j.detail as string) || 'Monthly AI budget reached',
			Number(j.used ?? 0),
			Number(j.budget ?? 0),
			String(j.period ?? '')
		);
	}
	if (!res.ok || !res.body) {
		throw new Error(`Stream error ${res.status}`);
	}

	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';

	const dispatch = (raw: string) => {
		// One SSE event block: lines of `event:` / `data:`. Multiple `data:`
		// lines concatenate with a newline (SSE spec); we expect single-line
		// JSON but handle the multi-line case defensively.
		let event = 'message';
		const dataLines: string[] = [];
		for (const line of raw.split('\n')) {
			if (line.startsWith('event:')) event = line.slice(6).trim();
			else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
		}
		if (dataLines.length === 0) return;
		let parsed: Record<string, unknown>;
		try {
			parsed = JSON.parse(dataLines.join('\n'));
		} catch {
			return; // ignore unparseable frame (e.g. a stray keep-alive comment)
		}
		switch (event) {
			case 'tool':
				cb.onTool?.(parsed as Parameters<NonNullable<StreamCallbacks['onTool']>>[0]);
				break;
			case 'delta':
				cb.onDelta?.(String(parsed.text ?? ''));
				break;
			case 'done':
				cb.onDone?.(parsed as Parameters<NonNullable<StreamCallbacks['onDone']>>[0]);
				break;
			case 'error':
				cb.onError?.(parsed);
				break;
		}
	};

	for (;;) {
		const { done, value } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });
		// SSE frames are separated by a blank line. Process every complete
		// frame in the buffer; keep the trailing partial for the next read.
		let sep: number;
		while ((sep = buffer.indexOf('\n\n')) !== -1) {
			const frame = buffer.slice(0, sep);
			buffer = buffer.slice(sep + 2);
			if (frame.trim()) dispatch(frame);
		}
	}
	// Flush any trailing frame not terminated by a blank line.
	if (buffer.trim()) dispatch(buffer);
}

export const api = {
	get: <T>(path: string) => request<T>(path),
	post: <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
	patch: <T>(path: string, body: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
	put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
	// `body` is optional — most DELETEs don't carry one, but a few need
	// credentials in the body rather than the URL (e.g. the passkey-delete
	// step-up password, which must never land in a query string / access log).
	delete: (path: string, body?: unknown) =>
		request<void>(path, {
			method: 'DELETE',
			...(body === undefined ? {} : { body: JSON.stringify(body) })
		}),
	upload: <T>(
		path: string,
		file: File,
		fields?: Record<string, string | string[] | undefined>,
		method: 'POST' | 'PUT' = 'POST'
	) => {
		const form = new FormData();
		form.append('file', file);
		// Optional extra multipart form fields (e.g. chat attachment body /
		// mention ids). Arrays repeat the key so FastAPI binds them to a
		// `list[str]` Form param; undefined / empty values are skipped.
		if (fields) {
			for (const [key, value] of Object.entries(fields)) {
				if (value === undefined) continue;
				if (Array.isArray(value)) {
					for (const v of value) form.append(key, v);
				} else {
					form.append(key, value);
				}
			}
		}
		return request<T>(path, {
			method,
			body: form,
			headers: {},  // let browser set Content-Type with boundary
		});
	},
	fetchBlob,
	downloadBlob,
	downloadBlobPost,
};
