"""No-Code Workflow Builder — engine for the NEW builder step types.

The visual workflow builder lets an admin compose a workflow out of the existing
canonical steps (``extraction``, ``approval``, ``erp_export``, ``done``) plus five
NEW builder step types stored in the same ``steps_config`` JSONB:

    condition  — branch the path on invoice field rules (goto another step)
    parallel   — fan an approval out to multiple branches, join on all/any/N
    webhook    — call out to an external URL (recorded-not-sent by default)
    email      — send a notification via the existing email adapter
    delay       — wait for a duration / until a field date (records intent only)

This module is the single home for the logic that evaluates / resolves /
executes those builder steps. It is consumed by:

  - ``workflow_simulation`` (Worker B) — dry-run a workflow over a fake invoice
  - the import + create paths in ``api/workflow_definitions`` (Worker B) —
    ``validate_builder_steps`` rejects malformed config before persisting
  - ``workflow_engine`` — which only needs to *know* the new types exist so it
    doesn't reject a definition that contains them (the engine still drives the
    invoice state machine; the builder steps are advisory/orchestration only).

LOCAL-FIRST (rail 7): every executor here runs on a dev laptop with no cloud and
no network. ``webhook`` defaults to a no-network "recorded" result; ``email``
uses the existing email adapter (``console`` by default); ``delay`` never sleeps —
it records the intent. ``dry_run=True`` (simulation) has ZERO side effects.

Money rule: amounts are ``Decimal`` everywhere — never ``float``.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.workflow_step_types import (
    BUILDER_STEP_TYPES,
    is_known_step_type,
    resolve_step_type,
)

logger = logging.getLogger(__name__)

# condition rule vocabulary — kept in sync with the spec + the frontend types.
CONDITION_FIELDS = {"amount", "currency", "vendor_id", "gl_account", "cost_center", "department"}
CONDITION_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "ne", "in", "not_in", "starts_with"}
# Operators that compare numerically (amount-style) rather than as strings.
_NUMERIC_OPERATORS = {"gt", "gte", "lt", "lte"}
_HTTP_METHODS = {"POST", "GET", "PUT"}
_EMAIL_RECIPIENTS = {"approver", "vendor", "custom"}


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Coerce a money value to Decimal without ever going through float.

    Accepts Decimal, int, or a decimal string. ``None``/blank → 0.
    """
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # guard: bool is an int subclass
        return Decimal("0")
    if isinstance(value, int):
        return Decimal(value)
    # str / anything else: route through str() so a stray float literal is
    # parsed as its decimal text, not its binary expansion.
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def build_invoice_context(invoice: Any) -> dict:
    """Map an Invoice ORM object (or a plain dict / SimInvoice) to the
    condition-evaluation context.

    Returns::

        {"amount": Decimal, "currency": str, "vendor_id": str|None,
         "gl_account": str|None, "cost_center": str|None, "department": str|None}

    Money stays Decimal — never float. ``vendor_id`` is stringified so a UUID and
    a plain-string id compare the same way in ``eq``/``in`` rules.
    """

    def _get(key: str) -> Any:
        if isinstance(invoice, dict):
            return invoice.get(key)
        return getattr(invoice, key, None)

    def _str_or_none(value: Any) -> str | None:
        return None if value is None else str(value)

    return {
        "amount": _to_decimal(_get("amount")),
        # None when the invoice records no currency (decisions §473): a
        # condition on `currency` then matches nothing rather than USD.
        "currency": _get("currency") or None,
        "vendor_id": _str_or_none(_get("vendor_id")),
        "gl_account": _str_or_none(_get("gl_account")),
        "cost_center": _str_or_none(_get("cost_center")),
        "department": _str_or_none(_get("department")),
    }


# ---------------------------------------------------------------------------
# condition
# ---------------------------------------------------------------------------


def _coerce_list(value: Any) -> list:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _evaluate_rule(rule: dict, ctx: dict) -> bool:
    """Evaluate a single condition rule against the context."""
    field = rule.get("field")
    operator = rule.get("operator")
    target = rule.get("value")

    actual = ctx.get(field)

    # Numeric comparisons run on Decimal (money-safe). All comparison fields
    # other than `amount` are strings; if a numeric operator is pointed at a
    # non-`amount` field we still compare on Decimal coercion for robustness.
    if operator in _NUMERIC_OPERATORS:
        left = actual if isinstance(actual, Decimal) else _to_decimal(actual)
        right = _to_decimal(target)
        if operator == "gt":
            return left > right
        if operator == "gte":
            return left >= right
        if operator == "lt":
            return left < right
        return left <= right  # lte

    # `amount` under an equality/membership operator: compare as Decimal so
    # "100" == 100.00 holds; everything else compares as string.
    if field == "amount":
        left_val: Any = actual if isinstance(actual, Decimal) else _to_decimal(actual)
    else:
        left_val = "" if actual is None else str(actual)

    def _norm(v: Any) -> Any:
        if field == "amount":
            return _to_decimal(v)
        return "" if v is None else str(v)

    if operator == "eq":
        return left_val == _norm(target)
    if operator == "ne":
        return left_val != _norm(target)
    if operator == "in":
        return left_val in [_norm(v) for v in _coerce_list(target)]
    if operator == "not_in":
        return left_val not in [_norm(v) for v in _coerce_list(target)]
    if operator == "starts_with":
        return str(left_val).startswith(str(target if target is not None else ""))

    # Unknown operator — defensive; validate_builder_steps catches these first.
    return False


def evaluate_condition(condition_config: dict, ctx: dict) -> dict:
    """Evaluate a ``condition`` step's config against an invoice context.

    Returns ``{"matched": bool, "goto": int|None, "explanation": str}``.
    ``goto`` is ``on_true_goto`` when matched, else ``on_false_goto`` (either may
    be ``None`` = fall through to the next step). ``match`` controls whether all
    rules must pass (``"all"``, default) or any one (``"any"``).
    """
    rules = condition_config.get("rules") or []
    match = condition_config.get("match", "all")

    results = [_evaluate_rule(rule, ctx) for rule in rules]
    if not results:
        # No rules — vacuously true for "all", vacuously false for "any".
        matched = match == "all"
    elif match == "any":
        matched = any(results)
    else:
        matched = all(results)

    goto = (
        condition_config.get("on_true_goto") if matched else condition_config.get("on_false_goto")
    )

    n_pass = sum(1 for r in results if r)
    explanation = (
        f"{n_pass}/{len(results)} rule(s) passed (match={match}); "
        f"{'matched' if matched else 'not matched'}"
        + (f" → goto step {goto}" if goto is not None else " → fall through")
    )
    return {"matched": matched, "goto": goto, "explanation": explanation}


# ---------------------------------------------------------------------------
# parallel
# ---------------------------------------------------------------------------


def resolve_parallel(parallel_config: dict) -> dict:
    """Resolve a ``parallel`` step's join semantics into a required-count.

    Returns::

        {"branches": [{"name": str, "approver_ids": [str]}],
         "join": str, "min_approvals": int|None, "required": int}

    ``required`` is the number of branch approvals needed to clear the join:
      - ``join == "all"`` → every branch
      - ``join == "any"`` → 1
      - ``min_approvals`` (when set) overrides, clamped to ``[1, len(branches)]``.
    """
    raw_branches = parallel_config.get("branches") or []
    branches = [
        {
            "name": b.get("name") or f"Branch {i + 1}",
            "approver_ids": [str(a) for a in (b.get("approver_ids") or [])],
        }
        for i, b in enumerate(raw_branches)
    ]
    join = parallel_config.get("join", "all")
    min_approvals = parallel_config.get("min_approvals")
    n = len(branches)

    if min_approvals is not None:
        required = max(1, min(int(min_approvals), n)) if n else int(min_approvals)
    elif join == "any":
        required = 1 if n else 0
    else:  # "all"
        required = n

    return {
        "branches": branches,
        "join": join,
        "min_approvals": min_approvals,
        "required": required,
    }


# ---------------------------------------------------------------------------
# custom-step execution (webhook / email / delay)
# ---------------------------------------------------------------------------


def _render_template(template: str | None, ctx: dict) -> str:
    """Best-effort ``{field}`` substitution from the context. Unknown keys are
    left as-is rather than raising — a builder template is user-authored text."""
    if not template:
        return ""
    out = template
    for key, value in ctx.items():
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, "" if value is None else str(value))
    return out


def _execute_webhook(config: dict, ctx: dict, *, dry_run: bool) -> dict:
    """Webhook executor. Local-first: by default the call is *recorded*, not
    sent (no network). A real send is only attempted when ``config["enabled"]``
    is truthy AND not a dry run — and even then the actual HTTP call is left to
    the deployed wiring; here we record intent so dev + simulation never touch
    the network."""
    url = config.get("url")
    method = (config.get("method") or "POST").upper()
    if not url:
        return {"type": "webhook", "status": "error", "detail": "webhook step missing 'url'"}

    rendered_body = _render_template(config.get("body_template"), ctx)
    enabled = bool(config.get("enabled"))

    if dry_run or not enabled:
        why = "dry-run" if dry_run else "disabled (local-first default)"
        return {
            "type": "webhook",
            "status": "ok",
            "detail": (
                f"recorded {method} {url} ({why}, not sent); body={len(rendered_body)} chars"
            ),
        }

    # enabled and not dry_run: still recorded-not-sent at the engine layer — the
    # actual network send belongs to deployed orchestration, never the dev path.
    return {
        "type": "webhook",
        "status": "ok",
        "detail": f"recorded {method} {url} for delivery; body={len(rendered_body)} chars",
    }


async def _execute_email(config: dict, ctx: dict, *, dry_run: bool) -> dict:
    """Email executor via the existing email adapter (``console`` by default).

    ``dry_run`` records the intent without sending. A real send resolves the
    recipient list and calls the adapter; failures degrade to status ``error``
    (never raise) so one bad step can't abort a workflow run.
    """
    to_kind = config.get("to", "custom")
    subject = _render_template(config.get("subject"), ctx) or "(no subject)"
    body = _render_template(config.get("body_template"), ctx)

    if to_kind == "custom":
        recipients = [str(a) for a in (config.get("to_addresses") or [])]
    else:
        # approver/vendor recipients are resolved by the caller at runtime; the
        # engine records the *kind* only (no PII, no address lookup here).
        recipients = []

    if dry_run:
        target = to_kind if to_kind != "custom" else f"{len(recipients)} address(es)"
        return {
            "type": "email",
            "status": "ok",
            "detail": f"would send '{subject}' to {target} (dry-run, not sent)",
        }

    if to_kind == "custom" and not recipients:
        return {
            "type": "email",
            "status": "skipped",
            "detail": "no recipient addresses configured",
        }
    if to_kind != "custom":
        # Runtime address resolution for approver/vendor lives in the caller;
        # the engine records intent rather than guessing an address.
        return {
            "type": "email",
            "status": "ok",
            "detail": f"queued '{subject}' for {to_kind} (resolved by caller)",
        }

    from app.services.email_adapters import EmailMessage, get_email_adapter

    try:
        adapter = get_email_adapter()
        for addr in recipients:
            await adapter.send(EmailMessage(to=addr, subject=subject, body_text=body))
    except Exception as exc:  # noqa: BLE001 — a bad email step must not abort the run
        # PII guard: the raw exception can embed a recipient email address
        # (the adapter echoes the address on a bad-recipient error). The result
        # detail is stored in WorkflowInstance.step_results JSONB and the log is
        # shipped to CloudWatch — keep the address out of both. Type only.
        err = exc.__class__.__name__
        logger.warning("[workflow_builder] email step send failed: %s", err)
        return {"type": "email", "status": "error", "detail": f"email send failed: {err}"}

    return {
        "type": "email",
        "status": "ok",
        "detail": f"sent '{subject}' to {len(recipients)} address(es)",
    }


def _execute_delay(config: dict, ctx: dict) -> dict:
    """Delay executor. NEVER sleeps — records the intended wait. A real
    scheduler (deployed) consumes this intent; in dev + simulation the workflow
    proceeds immediately."""
    duration = config.get("duration_seconds")
    until_field = config.get("until_field")
    if until_field:
        return {
            "type": "delay",
            "status": "ok",
            "detail": f"recorded wait until invoice.{until_field} (not slept)",
        }
    secs = int(duration) if duration is not None else 0
    return {
        "type": "delay",
        "status": "ok",
        "detail": f"recorded delay of {secs}s (not slept)",
    }


async def execute_custom_step(step: dict, ctx: dict, *, dry_run: bool = False) -> dict:
    """Execute a single ``webhook`` / ``email`` / ``delay`` builder step.

    ``dry_run=True`` (simulation) has NO side effects. Returns
    ``{"type": str, "status": "ok"|"skipped"|"error", "detail": str}``.

    Condition / parallel steps are NOT executed here — they're resolved via
    ``evaluate_condition`` / ``resolve_parallel`` because they branch the path
    rather than perform an effect.
    """
    step_type = step.get("type")
    config = step.get("config") or {}

    if step_type == "webhook":
        return _execute_webhook(config, ctx, dry_run=dry_run)
    if step_type == "email":
        return await _execute_email(config, ctx, dry_run=dry_run)
    if step_type == "delay":
        return _execute_delay(config, ctx)

    return {
        "type": str(step_type),
        "status": "error",
        "detail": f"execute_custom_step does not handle step type '{step_type}'",
    }


# ---------------------------------------------------------------------------
# validation (used by Worker B's import + create)
# ---------------------------------------------------------------------------


def _validate_condition(config: dict, label: str) -> list[str]:
    errors: list[str] = []
    rules = config.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append(f"{label}: condition needs a non-empty 'rules' list")
        rules = rules if isinstance(rules, list) else []
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"{label}: rule {i} must be an object")
            continue
        field = rule.get("field")
        operator = rule.get("operator")
        if field not in CONDITION_FIELDS:
            errors.append(
                f"{label}: rule {i} has unknown field '{field}' "
                f"(expected one of {sorted(CONDITION_FIELDS)})"
            )
        if operator not in CONDITION_OPERATORS:
            errors.append(
                f"{label}: rule {i} has unknown operator '{operator}' "
                f"(expected one of {sorted(CONDITION_OPERATORS)})"
            )
        if "value" not in rule:
            errors.append(f"{label}: rule {i} is missing 'value'")
    match = config.get("match", "all")
    if match not in {"all", "any"}:
        errors.append(f"{label}: 'match' must be 'all' or 'any' (got '{match}')")
    for goto_key in ("on_true_goto", "on_false_goto"):
        goto = config.get(goto_key)
        if goto is not None and not isinstance(goto, int):
            errors.append(f"{label}: '{goto_key}' must be an int step number or null")
    return errors


def _validate_parallel(config: dict, label: str) -> list[str]:
    errors: list[str] = []
    branches = config.get("branches")
    if not isinstance(branches, list) or not branches:
        errors.append(f"{label}: parallel needs a non-empty 'branches' list")
        branches = branches if isinstance(branches, list) else []
    for i, branch in enumerate(branches):
        if not isinstance(branch, dict):
            errors.append(f"{label}: branch {i} must be an object")
            continue
        approver_ids = branch.get("approver_ids")
        if not isinstance(approver_ids, list):
            errors.append(f"{label}: branch {i} 'approver_ids' must be a list")
    join = config.get("join", "all")
    if join not in {"all", "any"}:
        errors.append(f"{label}: 'join' must be 'all' or 'any' (got '{join}')")
    min_approvals = config.get("min_approvals")
    if min_approvals is not None:
        if not isinstance(min_approvals, int) or min_approvals < 1:
            errors.append(f"{label}: 'min_approvals' must be a positive int or null")
        elif isinstance(branches, list) and branches and min_approvals > len(branches):
            errors.append(
                f"{label}: 'min_approvals' ({min_approvals}) exceeds branch count ({len(branches)})"
            )
    return errors


def _validate_webhook(config: dict, label: str) -> list[str]:
    errors: list[str] = []
    url = config.get("url")
    if not url or not isinstance(url, str):
        errors.append(f"{label}: webhook is missing a 'url'")
    elif not (url.startswith("http://") or url.startswith("https://")):
        errors.append(f"{label}: webhook 'url' must start with http:// or https://")
    method = config.get("method") or "POST"
    if method not in _HTTP_METHODS:
        errors.append(f"{label}: webhook 'method' must be one of {sorted(_HTTP_METHODS)}")
    timeout = config.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        errors.append(f"{label}: webhook 'timeout_seconds' must be a positive int")
    return errors


def _validate_email(config: dict, label: str) -> list[str]:
    errors: list[str] = []
    to_kind = config.get("to", "custom")
    if to_kind not in _EMAIL_RECIPIENTS:
        errors.append(f"{label}: email 'to' must be one of {sorted(_EMAIL_RECIPIENTS)}")
    if to_kind == "custom":
        addrs = config.get("to_addresses")
        if not isinstance(addrs, list) or not addrs:
            errors.append(f"{label}: email with to='custom' needs 'to_addresses'")
    if not config.get("subject"):
        errors.append(f"{label}: email is missing a 'subject'")
    return errors


def _validate_delay(config: dict, label: str) -> list[str]:
    errors: list[str] = []
    duration = config.get("duration_seconds")
    until_field = config.get("until_field")
    if duration is None and not until_field:
        errors.append(f"{label}: delay needs 'duration_seconds' or 'until_field'")
    if duration is not None and (not isinstance(duration, int) or duration < 0):
        errors.append(f"{label}: delay 'duration_seconds' must be a non-negative int")
    return errors


_VALIDATORS = {
    "condition": _validate_condition,
    "parallel": _validate_parallel,
    "webhook": _validate_webhook,
    "email": _validate_email,
    "delay": _validate_delay,
}

# Money thresholds on the canonical `approval` step, and the chain levels' own
# amount bounds. Each is a real gate at runtime, and each is typed
# `Decimal | None` by `schemas/workflow.ApprovalStepConfig` /
# `ApprovalLevelConfig` — so every save path EXCEPT `POST /api/workflows/import`
# already refuses a non-numeric one. Import takes `steps_config` as a free-form
# dict, which is why they are re-checked here.
_APPROVAL_MONEY_FIELDS = ("auto_approve_below", "require_cfo_above", "max_invoice_amount")
_LEVEL_MONEY_FIELDS = ("min_amount", "max_amount")


def _numeric_error(value: Any, label: str, field: str) -> str | None:
    """``None`` when ``value`` is an absent or usable finite number, else the
    error string. Booleans are rejected outright — `True` is a Decimal-coercible
    `1` and would silently become a $1 threshold."""
    if value is None:
        return None
    if isinstance(value, bool):
        return f"{label}: '{field}' must be a number, not a boolean"
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return f"{label}: '{field}' must be a number (got {value!r})"
    if not parsed.is_finite():
        return f"{label}: '{field}' must be a finite number (got {value!r})"
    return None


def _validate_approval(config: dict, label: str) -> list[str]:
    """Money-threshold shape on the canonical ``approval`` step.

    These are gates, not decoration: an unusable `max_invoice_amount` /
    `require_cfo_above` now makes the runtime refuse the approval fail-closed
    (`approval_chain.max_amount_gate_applies` / `cfo_gate_applies`), and an
    unusable `auto_approve_below` silently stops being a floor. Refusing the
    definition at the boundary is what keeps that fail-closed behaviour a
    backstop rather than the way an org discovers its workflow is broken."""
    errors: list[str] = []
    for field in _APPROVAL_MONEY_FIELDS:
        err = _numeric_error(config.get(field), label, field)
        if err:
            errors.append(err)

    chain = config.get("approval_chain")
    if chain is not None and not isinstance(chain, list):
        errors.append(f"{label}: 'approval_chain' must be a list")
        chain = []
    for i, level in enumerate(chain or []):
        if not isinstance(level, dict):
            errors.append(f"{label}: approval_chain level {i} must be an object")
            continue
        for field in _LEVEL_MONEY_FIELDS:
            # `resolve_applicable_levels` reads an unparseable bound as "no
            # bound", so a typo here doesn't crash — it quietly widens the level
            # to every amount, routing money past the tier that should have seen it.
            err = _numeric_error(level.get(field), f"{label} level {i}", field)
            if err:
                errors.append(err)
    return errors


def _validate_extraction(config: dict, label: str) -> list[str]:
    """Confidence bar on the canonical ``extraction`` step.

    `decide_auto_approve` compares `overall_confidence >= auto_approve_threshold`;
    a non-numeric bar is a `TypeError` out of a pure function, and one outside
    0..1 can never be met (or is always met). Both now disable the confidence
    trigger fail-closed at runtime — this refuses them at the boundary."""
    errors: list[str] = []
    raw = config.get("auto_approve_threshold")
    if raw is None:
        return errors
    if isinstance(raw, bool):
        return [f"{label}: 'auto_approve_threshold' must be a number, not a boolean"]
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return [f"{label}: 'auto_approve_threshold' must be a number (got {raw!r})"]
    if not (0.0 <= value <= 1.0):
        errors.append(f"{label}: 'auto_approve_threshold' must be between 0 and 1 (got {raw!r})")
    return errors


# Canonical (engine) steps whose config carries runtime-load-bearing numbers.
# Separate from `_VALIDATORS` so the builder-step contract stays exactly as it
# was: these run IN ADDITION, keyed by canonical type.
_CANONICAL_VALIDATORS = {
    "approval": _validate_approval,
    "extraction": _validate_extraction,
}


def validate_builder_steps(steps: list[dict]) -> list[str]:
    """Validate a steps list before it is persisted as a workflow definition.

    Returns a list of human-readable error strings (empty list = valid).

    Two checks, in order:

    1. **Every step's ``type`` must be one the platform recognises** — a
       canonical engine step, a legacy alias, or one of the five builder types
       (``is_known_step_type``, the shared vocabulary). This is the gate that
       was missing: ``POST /api/workflows/import`` takes ``steps_config`` as a
       free-form dict, so it is the one save path a Pydantic ``Literal`` does
       not already constrain, and an unrecognised type used to persist happily
       and then be *silently ignored* at runtime — a typo'd ``"aproval"`` step
       reads to the engine as "no approval step configured", which drops the
       approval gate off the workflow rather than failing loudly.
    2. **Builder-step config shape** — the five builder types are inspected by
       ``_VALIDATORS``. Also cross-checks every ``condition`` ``goto`` target
       against the set of step numbers actually present, so a dangling branch is
       caught before persist.
    3. **Canonical-step numbers that gate money** — the ``approval`` step's
       ``auto_approve_below`` / ``require_cfo_above`` / ``max_invoice_amount``
       (and each chain level's ``min_amount`` / ``max_amount``), plus the
       ``extraction`` step's ``auto_approve_threshold``. Every other save path
       types these through Pydantic (``Decimal | None`` / a bounded float);
       import is the one that does not, and a non-numeric threshold reaching the
       runtime used to raise ``InvalidOperation`` out of the approval gate — a
       500 on every approval under that workflow. The gates now fail closed, and
       this refuses the definition before it can get there.
    """
    errors: list[str] = []
    if not isinstance(steps, list):
        return ["steps must be a list"]

    step_numbers = {s.get("number") for s in steps if isinstance(s, dict)}

    for step in steps:
        if not isinstance(step, dict):
            errors.append("each step must be an object")
            continue
        step_type = step.get("type")
        if not is_known_step_type(step_type):
            errors.append(
                f"step {step.get('number')}: unknown step type {step_type!r} "
                "— a step type the platform does not recognise would be ignored "
                "at runtime, silently removing that step from the workflow"
            )
            continue
        canonical_validator = _CANONICAL_VALIDATORS.get(resolve_step_type(step_type))
        if step_type not in BUILDER_STEP_TYPES and canonical_validator is None:
            continue  # canonical step with no runtime-load-bearing numbers
        number = step.get("number")
        name = step.get("name") or step_type
        label = f"step {number} ('{name}')"
        config = step.get("config")
        if canonical_validator is not None and config is None:
            # A canonical step's `config` is OPTIONAL — `{"number": 1, "type":
            # "approval"}` with no config at all is a valid step (it is how a
            # minimal experiment variant is written). Absent means "no thresholds
            # to check", not "malformed". The five BUILDER types below genuinely
            # require a config object, and keep demanding one.
            config = {}
        if not isinstance(config, dict):
            errors.append(f"{label}: 'config' must be an object")
            continue

        if canonical_validator is not None:
            errors.extend(canonical_validator(config, label))
        if step_type not in BUILDER_STEP_TYPES:
            continue

        errors.extend(_VALIDATORS[step_type](config, label))

        # goto-target integrity for conditions.
        if step_type == "condition":
            for goto_key in ("on_true_goto", "on_false_goto"):
                goto = config.get(goto_key)
                if isinstance(goto, int) and goto not in step_numbers:
                    errors.append(
                        f"{label}: '{goto_key}' points at step {goto}, "
                        f"which does not exist in this workflow"
                    )

    return errors
