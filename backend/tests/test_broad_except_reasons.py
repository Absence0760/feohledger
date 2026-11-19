"""A broad `except` that neither re-raises nor logs must say why, on its line.

`except Exception`, `except BaseException` and a bare `except:` catch every
failure, so one that also neither re-raises nor logs makes a failure vanish. Many
are deliberate — a webhook that answers 204 on every rejection so it cannot be
enumerated, a provider probe that fails closed to "unavailable" — but some turned
a failed query into a confident figure: the CFO dashboard reported 0 fraud
exceptions when the count had not been taken, and the billing page said "no saved
cards" during a provider outage. Those were removed; this guard keeps the rest
honest.

The rule: such a handler carries its reason on the `except` line itself, as
`# noqa: BLE001 — <reason>` (an en dash, hyphen or colon also separates). A
handler that logs (any `.debug/.info/.warning/.error/.exception/.critical/.log`
call in its body) or contains a `raise` is out of scope. When logging, log the
exception CLASS, never its message: the message can carry PII or banking detail
(root CLAUDE.md § Project invariants).

The tree predates the rule, so the existing reasonless sites are a per-file
ratchet (`_RATCHET`). A file may only go DOWN: a new reasonless site anywhere
fails, and so does a file that now has fewer than recorded until its entry is
lowered (or removed), so the record never hides headroom.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)
_REASON = re.compile(r"#\s*noqa:\s*[A-Z0-9, ]*BLE001\s*(?:—|–|-|:)\s*\S")

#: Reasonless silent broad handlers per file (relative to app/) when the guard
#: landed. Only ever lower these: add the reason (or log the class / re-raise)
#: and drop the count. `services/extraction.py` belongs to the extraction work
#: and is ratcheted here rather than edited.
_RATCHET: dict[str, int] = {
    "api/admin.py": 1,
    "api/cards.py": 2,
    "api/erp_webhook.py": 1,
    "api/organization.py": 1,
    "api/payments.py": 2,
    "api/portal.py": 1,
    "main.py": 1,
    "services/assistant/claude_adapter.py": 1,
    "services/assistant/ollama_adapter.py": 1,
    "services/assistant/orchestrator.py": 3,
    "services/card_adapters/lithic.py": 2,
    "services/card_adapters/nium.py": 1,
    "services/e_invoice/detect.py": 1,
    "services/e_invoice/facturx.py": 3,
    "services/email_intake_adapters/generic.py": 1,
    "services/email_intake_adapters/mailgun.py": 1,
    "services/email_intake_adapters/ses.py": 1,
    "services/embedding_adapters/openai_adapter.py": 1,
    "services/enrichment_adapters/clearbit.py": 1,
    "services/enrichment_adapters/dun_bradstreet.py": 1,
    "services/erp_adapters/blackbaud_fe_nxt.py": 1,
    "services/erp_adapters/dynamics_365_bc.py": 1,
    "services/erp_adapters/merge_dev.py": 1,
    "services/erp_adapters/netsuite.py": 1,
    "services/erp_adapters/posted_total.py": 1,
    "services/erp_adapters/sage_accounting.py": 1,
    "services/erp_adapters/sage_accounting_za.py": 1,
    "services/erp_adapters/sage_intacct.py": 1,
    "services/erp_adapters/syspro.py": 1,
    "services/erp_adapters/xero.py": 1,
    "services/erp_call_budget.py": 1,
    "services/erp_dispatch.py": 1,
    "services/erp.py": 1,
    "services/expense_currency.py": 1,
    "services/extraction_adapters/aws_textract.py": 2,
    "services/extraction_adapters/claude_vision.py": 3,
    "services/extraction_adapters/ollama.py": 3,
    "services/extraction_adapters/openai_vision.py": 3,
    "services/extraction.py": 2,
    "services/fx_adapters/openexchangerates.py": 1,
    "services/mfa.py": 1,
    "services/payment_adapters/checkeeper.py": 1,
    "services/sanctions_adapters/complyadvantage.py": 1,
    "services/sanctions_adapters/dowjones.py": 1,
    "services/sanctions_adapters/refinitiv.py": 1,
    "services/scheduled_reports.py": 1,
    "services/storage.py": 1,
    "services/tax_filing_adapters/tax1099_adapter.py": 1,
    "services/tin_validation_adapters/tax1099_adapter.py": 1,
    "tenant.py": 1,
}


def _is_broad(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in ("Exception", "BaseException")
    if isinstance(node, ast.Tuple):
        return any(_is_broad(e) for e in node.elts)
    return False


def _own_nodes(nodes: list[ast.AST]):
    """Every node under `nodes`, not descending into nested scopes (a `raise`
    inside a nested function does not re-raise from the handler)."""
    for node in nodes:
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        yield from _own_nodes(list(ast.iter_child_nodes(node)))


def reasonless_silent_handlers(source: str) -> list[int]:
    """Line numbers of broad handlers that neither raise nor log and give no reason."""
    lines = source.splitlines()
    out: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node.type):
            continue
        body = list(_own_nodes(list(node.body)))
        if any(isinstance(n, ast.Raise) for n in body):
            continue
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _LOG_METHODS
            for n in body
        ):
            continue
        if not _REASON.search(lines[node.lineno - 1]):
            out.append(node.lineno)
    return sorted(out)


def _tree_counts() -> tuple[Counter[str], dict[str, list[int]]]:
    counts: Counter[str] = Counter()
    where: dict[str, list[int]] = {}
    for path in sorted(APP.rglob("*.py")):
        hits = reasonless_silent_handlers(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(APP).as_posix()
            counts[rel] = len(hits)
            where[rel] = hits
    return counts, where


def test_no_new_reasonless_silent_broad_except():
    counts, where = _tree_counts()
    grown = {f: (where[f], _RATCHET.get(f, 0)) for f, n in counts.items() if n > _RATCHET.get(f, 0)}
    assert not grown, (
        "A broad except that neither re-raises nor logs needs its reason on the "
        "except line: '# noqa: BLE001 — <why swallowing is right here>'. Or log the "
        "exception class (never the message), or let it raise. "
        f"Offending file -> (lines, allowed): {grown}"
    )


def test_ratchet_only_goes_down():
    counts, _ = _tree_counts()
    shrunk = {f: (counts.get(f, 0), n) for f, n in _RATCHET.items() if counts.get(f, 0) < n}
    assert not shrunk, (
        "These files now have fewer reasonless silent handlers than _RATCHET "
        f"records; lower (or remove) their entries: {shrunk}"
    )


# --- self-tests: the detector catches what it claims to -----------------------


def test_catches_a_reasonless_swallow():
    src = "try:\n    f()\nexcept Exception:  # noqa: BLE001\n    x = 0\n"
    assert reasonless_silent_handlers(src) == [3]


def test_catches_bare_and_tuple_and_base_exception():
    src = (
        "try:\n    f()\nexcept:\n    pass\n"
        "try:\n    f()\nexcept (ValueError, Exception):\n    pass\n"
        "try:\n    f()\nexcept BaseException:\n    pass\n"
    )
    assert reasonless_silent_handlers(src) == [3, 7, 11]


def test_a_written_reason_passes_and_an_empty_one_does_not():
    ok = "try:\n    f()\nexcept Exception:  # noqa: BLE001 — webhook answers 204\n    pass\n"
    empty = "try:\n    f()\nexcept Exception:  # noqa: BLE001 —\n    pass\n"
    assert reasonless_silent_handlers(ok) == []
    assert reasonless_silent_handlers(empty) == [3]


def test_logging_or_reraising_handlers_are_out_of_scope():
    logs = "try:\n    f()\nexcept Exception as exc:\n    log.warning('x %s', type(exc).__name__)\n"
    raises = "try:\n    f()\nexcept Exception as exc:\n    raise Boom() from exc\n"
    assert reasonless_silent_handlers(logs) == []
    assert reasonless_silent_handlers(raises) == []


def test_a_raise_in_a_nested_function_does_not_count():
    src = "try:\n    f()\nexcept Exception:\n    def g():\n        raise X\n"
    assert reasonless_silent_handlers(src) == [3]


def test_a_narrow_except_is_out_of_scope():
    assert reasonless_silent_handlers("try:\n    f()\nexcept ValueError:\n    pass\n") == []
