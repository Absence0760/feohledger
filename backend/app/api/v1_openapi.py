"""Published, versioned OpenAPI document for the public ``/api/v1`` surface.

This is the *external contract* an integrator codes against — deliberately
**scoped to the public ``/api/v1`` routes only**, NOT the whole internal SPA
API. It is generated from the live FastAPI route table (so it can't drift from
the routes) but filtered down to the ``/api/v1`` path prefix, then overlaid
with:

- a single ``X-API-Key`` security scheme (the only auth on this surface — never
  the SPA's JWT) applied globally;
- an ``info.version`` of ``v1`` and a ``servers`` entry built from
  ``FEOH_API_PUBLIC_URL`` so generated clients hit the right base URL;
- the published ``V1Invoice`` / ``V1InvoiceList`` component schemas only — no
  internal-only model leaks in;
- an OpenAPI 3.1 ``webhooks`` section: one entry per outbound event in
  ``models/webhook.WEBHOOK_EVENT_TYPES``, its body the signed envelope with the
  event's ``data`` model from ``schemas/webhook_events`` — the same model the
  emitter builds the payload through, so the published payload cannot drift
  from the delivered one.

Both this spec and the human-readable docs page respect the
``FEOH_PUBLIC_API_ENABLED`` kill switch: when the public API is off, the spec and
docs 404 (the surface is simply not there), consistent with every API-key
request failing closed.

The routes are mounted additively in ``app/main.py`` at:

- ``GET /api/v1/openapi.json`` — the machine-readable spec
- ``GET /api/v1/docs``         — a self-contained HTML reference rendered from
  that spec (server-side, no script, no external asset — see
  ``render_docs_html`` for why not Swagger UI)
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import settings
from app.models.webhook import WEBHOOK_EVENT_TYPES
from app.schemas.webhook_events import (
    WEBHOOK_EVENT_DATA_MODELS,
    WEBHOOK_EVENT_SUMMARIES,
    webhook_envelope_schema,
)

# Stable contract version of the public surface. Bumping the path prefix
# (``/api/v2``) is what introduces a new version; this string tracks it.
PUBLIC_API_VERSION = "v1"

# The path prefix (under the app's ``/api`` mount) that constitutes the public
# surface. Anything not under here is internal and must not appear in the spec.
_PUBLIC_PATH_PREFIX = "/api/v1"

# Paths that are part of the public mount but are not themselves part of the
# documented contract (the spec/docs meta-endpoints describe the contract; they
# are not a resource of it).
_META_PATHS = frozenset(
    {
        f"{_PUBLIC_PATH_PREFIX}/openapi.json",
        f"{_PUBLIC_PATH_PREFIX}/docs",
    }
)

router = APIRouter(prefix="/v1", tags=["public-v1-meta"])


def build_public_openapi(app: FastAPI) -> dict[str, Any]:
    """Build the OpenAPI document scoped to the public ``/api/v1`` routes.

    Generates the full app spec from the live route table, then keeps only the
    ``/api/v1`` paths (minus the meta endpoints), prunes orphaned component
    schemas, and overlays the ``X-API-Key`` security scheme + the outbound
    ``webhooks`` catalogue + ``servers`` + contract version. Pure with respect
    to the app (reads routes, returns a new dict) — safe to call per request.
    """
    full = get_openapi(
        title="FeohLedger — Public Developer API",
        version=PUBLIC_API_VERSION,
        description=(
            "Programmatic, versioned access to the FeohLedger platform. "
            "Authenticated with a per-organization API key sent in the "
            "`X-API-Key` header (NOT the SPA session JWT). The key resolves to "
            "its organization and tenant data — there is no tenant header to "
            "swap. See backend/docs/public-api.md for the deprecation policy."
        ),
        routes=app.routes,
    )

    # 1. Keep only the public-surface paths, dropping the meta endpoints.
    public_paths: dict[str, Any] = {
        path: item
        for path, item in (full.get("paths") or {}).items()
        if path.startswith(_PUBLIC_PATH_PREFIX) and path not in _META_PATHS
    }
    full["paths"] = public_paths

    # 2. Prune component schemas down to those actually referenced by the kept
    #    paths (transitively) so no internal-only model leaks in via an orphan.
    all_schemas = (full.get("components") or {}).get("schemas") or {}
    kept_schema_names = _referenced_schema_names(public_paths, all_schemas)
    components: dict[str, Any] = full.get("components") or {}
    components["schemas"] = {
        name: schema for name, schema in all_schemas.items() if name in kept_schema_names
    }

    # 3. The ONLY auth on this surface: an API-key header scheme, applied
    #    globally so every documented operation shows it.
    components["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": (
                "Per-organization API key, format `feoh_live_…`. Mint via the "
                "admin key-management API. The key is the tenant boundary."
            ),
        }
    }
    full["components"] = components
    full["security"] = [{"ApiKeyAuth": []}]

    # 4. The outbound webhook catalogue — the push half of the contract.
    full["webhooks"] = _webhooks_section(components["schemas"])

    # 5. A servers entry so generated clients hit the right base URL.
    full["servers"] = [
        {
            "url": settings.api_public_url.rstrip("/"),
            "description": "FeohLedger API",
        }
    ]

    return full


_SCHEMA_REF = "#/components/schemas/{model}"

#: Headers every delivery carries (`services/webhooks/delivery`).
_WEBHOOK_HEADERS: tuple[tuple[str, bool, str], ...] = (
    (
        "X-Webhook-Signature",
        True,
        "Hex HMAC-SHA256 of the raw body under the subscription's current signing secret.",
    ),
    (
        "X-Webhook-Signature-Previous",
        False,
        "The same, under the retiring secret — sent only during a rotation's overlap window.",
    ),
    ("X-Webhook-Event-Id", True, "The event id (the body's `id`). Dedupe on it."),
    ("X-Webhook-Event-Type", True, "The event type (the body's `type`)."),
)


def _webhooks_section(component_schemas: dict[str, Any]) -> dict[str, Any]:
    """Build the OpenAPI 3.1 ``webhooks`` map and add each event's ``data``
    model to ``component_schemas``.

    One entry per type in ``WEBHOOK_EVENT_TYPES``, in that order; a type with no
    ``data`` model raises here, so a new event cannot be added to the catalogue
    without being published.
    """
    webhooks: dict[str, Any] = {}
    for event_type in WEBHOOK_EVENT_TYPES:
        model = WEBHOOK_EVENT_DATA_MODELS[event_type]
        schema = model.model_json_schema(ref_template=_SCHEMA_REF)
        for name, definition in (schema.pop("$defs", None) or {}).items():
            component_schemas.setdefault(name, definition)
        component_schemas.setdefault(model.__name__, schema)
        webhooks[event_type] = {
            "post": {
                "operationId": f"webhook_{event_type.replace('.', '_')}",
                "summary": WEBHOOK_EVENT_SUMMARIES[event_type],
                "description": (
                    f"Delivered to a subscription that lists `{event_type}`, as a "
                    "signed JSON POST. Retried with backoff on any non-2xx; dedupe "
                    "on `X-Webhook-Event-Id`."
                ),
                "security": [],
                "parameters": [
                    {
                        "name": name,
                        "in": "header",
                        "required": required,
                        "description": description,
                        "schema": {"type": "string"},
                    }
                    for name, required, description in _WEBHOOK_HEADERS
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": webhook_envelope_schema(
                                event_type, _SCHEMA_REF.format(model=model.__name__)
                            )
                        }
                    },
                },
                "responses": {"2XX": {"description": "Received. Any non-2xx is retried."}},
            }
        }
    return webhooks


def _referenced_schema_names(paths: dict[str, Any], all_schemas: dict[str, Any]) -> set[str]:
    """Collect every component-schema name reachable from ``paths``.

    Walks the kept path items for ``$ref`` pointers into
    ``#/components/schemas/…`` and follows references between schemas
    transitively, so a kept schema that references another keeps that one too.
    """
    seen: set[str] = set()

    def visit_ref(ref: str) -> None:
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return
        name = ref[len(prefix) :]
        if name in seen or name not in all_schemas:
            return
        seen.add(name)
        _collect_refs(all_schemas[name], visit_ref)

    for item in paths.values():
        _collect_refs(item, visit_ref)

    return seen


def _collect_refs(node: Any, on_ref) -> None:
    """Recursively invoke ``on_ref`` for every ``$ref`` string in ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                on_ref(value)
            else:
                _collect_refs(value, on_ref)
    elif isinstance(node, list):
        for value in node:
            _collect_refs(value, on_ref)


def _ensure_enabled() -> None:
    """404 when the public API kill switch is off — the surface is simply gone.

    Keeps the spec/docs behaviour consistent with every API-key request, which
    fails closed when ``FEOH_PUBLIC_API_ENABLED`` is false. A 404 (rather than a
    distinct "disabled") doesn't confirm the endpoint exists.
    """
    if not settings.public_api_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.get("/openapi.json", include_in_schema=False)
async def public_openapi(request: Request) -> JSONResponse:
    """The published OpenAPI document for the ``/api/v1`` surface (public)."""
    _ensure_enabled()
    return JSONResponse(build_public_openapi(request.app))


@router.get("/docs", include_in_schema=False)
async def public_docs(request: Request) -> HTMLResponse:
    """Human-readable reference for the public ``/api/v1`` surface.

    Rendered from the same document ``/v1/openapi.json`` serves, server-side,
    with no script and no external asset — see :func:`render_docs_html`.
    """
    _ensure_enabled()
    return HTMLResponse(
        render_docs_html(build_public_openapi(request.app)),
        headers={"Content-Security-Policy": DOCS_CSP},
    )


# ---------------------------------------------------------------------------
# The human-readable page
# ---------------------------------------------------------------------------

#: Route-scoped CSP for the docs page. Still strict — **no script from any
#: origin**, nothing third-party, no framing, no base-uri. It differs from the
#: global `default-src 'none'` only by allowing the page's own inline
#: stylesheet, which cannot exfiltrate or execute anything.
#:
#: `main.SecurityHeadersMiddleware` sets its header with `setdefault`, so a
#: header the route sets wins. The GLOBAL policy is deliberately left alone: it
#: is what keeps the API origin unable to load third-party script at all, and
#: relaxing it for one page would relax it for every JSON response too.
DOCS_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"

_DOCS_STYLE = """
:root { color-scheme: light dark; }
body { font: 16px/1.6 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 0 auto; max-width: 52rem; padding: 2rem 1.25rem 6rem; }
h1 { font-size: 1.6rem; margin-bottom: .25rem; }
h2 { font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 1px solid; padding-bottom: .3rem; }
h3 { font-size: 1rem; margin-top: 1.75rem; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em; }
pre { overflow-x: auto; padding: .75rem; border: 1px solid; border-radius: 6px; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; }
th, td { text-align: left; padding: .35rem .6rem; border: 1px solid; vertical-align: top; }
th { font-weight: 600; }
.op { font-weight: 700; letter-spacing: .04em; }
.muted { opacity: .75; }
.req::after { content: " *"; }
"""


def _esc(value: object) -> str:
    """Escape anything for HTML text. Every value here is server-authored (our
    own route metadata), but escaping is the invariant, not the audit."""
    return html.escape("" if value is None else str(value), quote=True)


def _schema_label(node: Any) -> str:
    """A short, human type label for a schema node — no recursion into objects."""
    if not isinstance(node, dict):
        return ""
    ref = node.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    for key in ("allOf", "anyOf", "oneOf"):
        options = node.get(key)
        if isinstance(options, list) and options:
            labels = [_schema_label(o) for o in options]
            return " | ".join(x for x in labels if x)
    node_type = node.get("type")
    if node_type == "array":
        inner = _schema_label(node.get("items") or {})
        return f"array[{inner}]" if inner else "array"
    if isinstance(node_type, str):
        fmt = node.get("format")
        return f"{node_type} ({fmt})" if fmt else node_type
    return ""


def render_docs_html(spec: dict[str, Any]) -> str:
    """Render the published spec as a self-contained HTML page.

    **Why this exists instead of Swagger UI.** ``get_swagger_ui_html``'s only
    stylesheet, script and favicon are third-party CDN URLs
    (``cdn.jsdelivr.net``, ``fastapi.tiangolo.com``), while
    ``main.SecurityHeadersMiddleware`` stamps
    ``Content-Security-Policy: default-src 'none'`` on every response — so the
    page loaded, fetched nothing, and rendered blank in any browser honouring
    the header. Three ways out were on the table:

    1. **Vendor ``swagger-ui-dist`` and serve it from our own origin.** Keeps
       the CSP strict and works offline, but commits ~1 MB of third-party
       JavaScript to a public repo and adds a version nobody will remember to
       bump — a supply-chain artifact acquired for a reference page.
    2. **Allowlist the CDN in a route-scoped CSP.** Three lines, but it hands a
       page the platform serves a third-party runtime dependency, and it *still*
       renders blank offline, which breaks guard rail 7 (local-first).
    3. **Drop the route** and point integrators at the spec URL. Honest, but a
       404 on a URL the docs advertise is its own defect.

    This is (1) taken to its minimum: the viewer is served from our own origin
    and works offline, without the vendored bundle. Server-rendered HTML, no
    script at all, no external asset of any kind — so the page needs only the
    one-token CSP relaxation in :data:`DOCS_CSP` (an inline stylesheet), and the
    global policy is untouched.

    The trade-off is deliberate and worth stating: this is a *reference*, not an
    interactive console — there is no "Try it out". The machine-readable
    contract at ``/api/v1/openapi.json`` is what integrators actually consume,
    and it feeds any client generator or Swagger/Redoc instance they already
    run. Pure function of the spec, so it can't drift from the routes.
    """
    info = spec.get("info") or {}
    title = info.get("title") or "Public Developer API"
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(title)}</title>",
        f"<style>{_DOCS_STYLE}</style>",
        "</head><body>",
        f"<h1>{_esc(title)}</h1>",
        f'<p class="muted">Version <code>{_esc(info.get("version"))}</code></p>',
    ]
    if info.get("description"):
        parts.append(f"<p>{_esc(info['description'])}</p>")

    # The spec URL first — it is the actual contract; this page is a reading aid.
    spec_url = f"{_PUBLIC_PATH_PREFIX}/openapi.json"
    parts.append(
        f"<p><strong>Machine-readable contract:</strong> <code>GET {_esc(spec_url)}</code>"
        " — point any OpenAPI client generator at it.</p>"
    )

    servers = spec.get("servers") or []
    if servers:
        rows = "".join(
            f"<tr><td><code>{_esc(s.get('url'))}</code></td>"
            f"<td>{_esc(s.get('description'))}</td></tr>"
            for s in servers
        )
        parts.append(f"<h2>Servers</h2><table><tbody>{rows}</tbody></table>")

    schemes = ((spec.get("components") or {}).get("securitySchemes") or {}).items()
    if schemes:
        parts.append("<h2>Authentication</h2>")
        for _name, scheme in schemes:
            header = _esc(scheme.get("name"))
            parts.append(
                f"<p>Send the header <code>{header}</code>. {_esc(scheme.get('description'))}</p>"
            )

    parts.append("<h2>Endpoints</h2>")
    paths = spec.get("paths") or {}
    if not paths:
        parts.append('<p class="muted">No endpoints are published.</p>')
    for path in sorted(paths):
        item = paths[path] or {}
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            parts.append(
                f'<h3><span class="op">{method.upper()}</span> <code>{_esc(path)}</code></h3>'
            )
            if op.get("summary"):
                parts.append(f"<p>{_esc(op['summary'])}</p>")
            if op.get("description"):
                parts.append(f'<p class="muted">{_esc(op["description"])}</p>')
            parts.append(_render_parameters(op.get("parameters") or []))
            parts.append(_render_responses(op.get("responses") or {}))

    parts.append(_render_webhooks(spec.get("webhooks") or {}))
    parts.append(_render_schemas((spec.get("components") or {}).get("schemas") or {}))
    parts.append("</body></html>")
    return "".join(p for p in parts if p)


def _render_parameters(parameters: list[Any]) -> str:
    if not parameters:
        return ""
    rows = []
    for param in parameters:
        if not isinstance(param, dict):
            continue
        required = ' class="req"' if param.get("required") else ""
        rows.append(
            f"<tr><td><code{required}>{_esc(param.get('name'))}</code></td>"
            f"<td>{_esc(param.get('in'))}</td>"
            f"<td>{_esc(_schema_label(param.get('schema') or {}))}</td>"
            f"<td>{_esc(param.get('description'))}</td></tr>"
        )
    if not rows:
        return ""
    head = "<tr><th>Parameter</th><th>In</th><th>Type</th><th>Description</th></tr>"
    return f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def _render_responses(responses: dict[str, Any]) -> str:
    rows = []
    for code in sorted(responses):
        body = responses[code] if isinstance(responses[code], dict) else {}
        content = body.get("content") or {}
        json_schema = (content.get("application/json") or {}).get("schema") or {}
        rows.append(
            f"<tr><td><code>{_esc(code)}</code></td>"
            f"<td>{_esc(body.get('description'))}</td>"
            f"<td>{_esc(_schema_label(json_schema))}</td></tr>"
        )
    if not rows:
        return ""
    head = "<tr><th>Status</th><th>Description</th><th>Body</th></tr>"
    return f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>"


def _render_webhooks(webhooks: dict[str, Any]) -> str:
    """The outbound events, each with its summary and its ``data`` schema."""
    if not webhooks:
        return ""
    parts = [
        "<h2>Webhooks</h2>",
        "<p>Events FeohLedger POSTs to a subscription's URL. Each body is "
        "<code>{id, type, created_at, organization_id, data}</code>; "
        "verify <code>X-Webhook-Signature</code> and dedupe on "
        "<code>X-Webhook-Event-Id</code>.</p>",
    ]
    for event_type in webhooks:
        op = (webhooks[event_type] or {}).get("post") or {}
        body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json")) or {}
        data = ((body.get("schema") or {}).get("properties") or {}).get("data") or {}
        parts.append(f"<h3><code>{_esc(event_type)}</code></h3>")
        if op.get("summary"):
            parts.append(f"<p>{_esc(op['summary'])}</p>")
        label = _schema_label(data)
        if label:
            parts.append(f'<p class="muted"><code>data</code>: <code>{_esc(label)}</code></p>')
    return "".join(parts)


def _render_schemas(schemas: dict[str, Any]) -> str:
    if not schemas:
        return ""
    parts = ["<h2>Schemas</h2>"]
    for name in sorted(schemas):
        schema = schemas[name] if isinstance(schemas[name], dict) else {}
        parts.append(f"<h3><code>{_esc(name)}</code></h3>")
        if schema.get("description"):
            parts.append(f'<p class="muted">{_esc(schema["description"])}</p>')
        required = set(schema.get("required") or [])
        rows = []
        for field, node in (schema.get("properties") or {}).items():
            marker = ' class="req"' if field in required else ""
            node = node if isinstance(node, dict) else {}
            rows.append(
                f"<tr><td><code{marker}>{_esc(field)}</code></td>"
                f"<td>{_esc(_schema_label(node))}</td>"
                f"<td>{_esc(node.get('description'))}</td></tr>"
            )
        if rows:
            head = "<tr><th>Field</th><th>Type</th><th>Description</th></tr>"
            parts.append(f"<table><thead>{head}</thead><tbody>{''.join(rows)}</tbody></table>")
    return "".join(parts)
