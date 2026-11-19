"""Every `Invoice(...)` built under `app/` names its currency (decisions §473).

`Invoice.__init__` falls back to "USD" when `currency` is not mentioned at all,
which keeps old test fixtures working. That fallback is the substitution
§160 / §196 / §200 forbid if a real document ever reaches it, so an application
construction site must say which currency it means: the code the user or the
document stated, or `None` for "unknown" (the upload, the supplier-portal
submit). This scans the AST the way `test_invoice_uploader_stamping.py` does.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _invoice_constructions():
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "Invoice"
            ):
                yield path, node


def test_there_are_construction_sites_to_check():
    assert sum(1 for _ in _invoice_constructions()) >= 5


def test_every_app_construction_site_names_its_currency():
    missing = [
        f"{path.relative_to(APP.parent)}:{node.lineno}"
        for path, node in _invoice_constructions()
        if not any(kw.arg == "currency" for kw in node.keywords)
        and not any(kw.arg is None for kw in node.keywords)  # **kwargs: caller's job
    ]
    assert missing == [], (
        "Invoice(...) built without `currency=` at " + ", ".join(missing) + ". Pass the "
        "stated code, or `currency=None` when nothing has said one (never a guessed USD)."
    )


def test_the_upload_and_the_portal_submit_record_unknown():
    """The two doors that create a row before anything has read the document."""
    for rel in ("api/workflow.py", "api/portal.py"):
        path = APP / rel
        tree = ast.parse(path.read_text(), filename=str(path))
        none_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Invoice"
            and any(
                kw.arg == "currency"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is None
                for kw in node.keywords
            )
        ]
        assert none_sites, f"{rel} should create its uploaded invoice with currency=None"
