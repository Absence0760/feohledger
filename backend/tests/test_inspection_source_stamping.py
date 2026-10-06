"""Every `QualityInspection(...)` built under `app/` states where its verdict
came from.

A refresh that no longer finds a `quality_hold` closes it
(`invoice_warnings._close_cleared_po_exceptions`), and it refuses to when the
inspection that cleared it was typed in by someone implicated in the invoice,
or by nobody it can name. That rule reads `QualityInspection.source` /
`recorded_by_user_id` (migration 0105), and an inspection created without
them reads as unknown provenance — held for a human, fail closed. A new
creation site that forgets them would therefore not open a hole, but it would
silently stop QMS passes from lifting holds; this guard makes the omission a
test failure instead. Same shape as `test_exception_raiser_stamping.py`.
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
                and node.func.id == "QualityInspection"
            ):
                sites.append((path, node))
    return sites


def test_every_inspection_construction_stamps_its_source():
    sites = _construction_sites()
    assert len(sites) >= 2, "expected the manual and QMS creation sites"
    missing = [
        f"{path.relative_to(APP.parent)}:{call.lineno}"
        for path, call in sites
        if "source" not in {kw.arg for kw in call.keywords}
        or "recorded_by_user_id" not in {kw.arg for kw in call.keywords}
    ]
    assert missing == [], f"QualityInspection built without source/recorded_by: {missing}"
