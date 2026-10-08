"""Every `GoodsReceipt(...)` built under `app/` states where it came from.

A refresh that no longer finds a "billed beyond receipt" `po_mismatch` closes
it (`invoice_warnings._close_cleared_po_exceptions`), and it refuses to while a
hand-entered receipt on the PO was recorded by someone implicated in the
invoice (`_receipts_clear_hold`, decisions §261). That rule keys on
`GoodsReceipt.source == "manual"` — and, unlike an inspection, a receipt with
NO source is trusted, because every such row predates receipt entry. So a new
in-app creation site that forgot the stamp would not fail closed: its receipts
would read as "from elsewhere" and could release the creator's own invoice.
This guard makes that omission a test failure. Same shape as
`test_inspection_source_stamping.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"


def _construction_sites() -> list[tuple[Path, ast.Call]]:
    sites = []
    for path in APP.rglob("*.py"):
        if path.parts[-2] == "models":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "GoodsReceipt"
            ):
                sites.append((path, node))
    return sites


def test_every_receipt_construction_stamps_its_source_and_recorder():
    sites = _construction_sites()
    assert sites, "expected at least the manual entry site (services/goods_receipts.py)"
    missing = [
        f"{path.relative_to(APP.parent)}:{call.lineno}"
        for path, call in sites
        if "source" not in {kw.arg for kw in call.keywords}
        or "recorded_by_user_id" not in {kw.arg for kw in call.keywords}
    ]
    assert missing == [], f"GoodsReceipt built without source/recorded_by_user_id: {missing}"
