"""The public pricing page is generated from the plan catalogue (issue #426).

`scripts/gen_pricing_catalog.py` writes `frontend/src/lib/marketing/
plans.generated.ts` from `DEFAULT_PLAN_CATALOG`; `--check` is the CI drift
guard (`pnpm check:pricing`). These pin the guard itself: it passes on the
committed file, goes red on a catalogue change, and refuses shapes the page
could only describe by guessing. Pure — no database, no services.
"""

from __future__ import annotations

import copy
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


def _generator():
    path = Path(__file__).resolve().parent.parent / "scripts" / "gen_pricing_catalog.py"
    spec = importlib.util.spec_from_file_location("gen_pricing_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_module_is_in_sync():
    """The same assertion CI's `Backend lint` job makes."""
    assert _generator().main(["--check"]) == 0


def test_the_check_goes_red_when_a_price_changes(monkeypatch, capsys):
    gen = _generator()
    catalog = copy.deepcopy(gen.DEFAULT_PLAN_CATALOG)
    growth = next(p for p in catalog if p["code"] == "growth")
    growth["monthly_price"] = Decimal("59.00")
    monkeypatch.setattr(gen, "DEFAULT_PLAN_CATALOG", tuple(catalog))

    assert gen.main(["--check"]) == 1
    assert "pnpm gen:pricing" in capsys.readouterr().err


def test_the_check_goes_red_when_an_allowance_or_feature_changes(monkeypatch):
    gen = _generator()
    catalog = copy.deepcopy(gen.DEFAULT_PLAN_CATALOG)
    free = next(p for p in catalog if p["code"] == "free")
    free["usage_components"][gen.METER_AI_INVOICES]["included"] = 150
    monkeypatch.setattr(gen, "DEFAULT_PLAN_CATALOG", tuple(catalog))
    assert gen.main(["--check"]) == 1

    catalog = copy.deepcopy(gen.DEFAULT_PLAN_CATALOG)
    growth = next(p for p in catalog if p["code"] == "growth")
    growth["entitlements"].pop("sso")
    monkeypatch.setattr(gen, "DEFAULT_PLAN_CATALOG", tuple(catalog))
    assert gen.main(["--check"]) == 1


def test_money_is_emitted_as_exact_strings():
    rows = {row["code"]: row for row in _generator().plan_rows()}
    assert rows["growth"]["monthlyPrice"] == "49.00"
    assert rows["growth"]["overageUnitPrice"] == "0.10"
    assert rows["free"]["overageUnitPrice"] is None
    # Features follow ALL_FEATURES order, not each plan's dict order.
    gen = _generator()
    assert rows["scale"]["features"] == list(gen.ALL_FEATURES)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p, g: p["entitlements"].__setitem__("max_seats", 25), "not in ALL_FEATURES"),
        (lambda p, g: p.__setitem__("monthly_price", 49.0), "float"),
        (
            lambda p, g: p["usage_components"][g.METER_AI_INVOICES].__setitem__(
                "overage_unit_price", "ten cents"
            ),
            "not a decimal",
        ),
        (
            lambda p, g: p["usage_components"].__setitem__("storage_gb", {"included": 5}),
            "only knows",
        ),
    ],
)
def test_shapes_the_page_cannot_describe_fail_generation(monkeypatch, mutate, message):
    gen = _generator()
    catalog = copy.deepcopy(gen.DEFAULT_PLAN_CATALOG)
    growth = next(p for p in catalog if p["code"] == "growth")
    mutate(growth, gen)
    monkeypatch.setattr(gen, "DEFAULT_PLAN_CATALOG", tuple(catalog))
    with pytest.raises(gen.CatalogError, match=message):
        gen.plan_rows()
