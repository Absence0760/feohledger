"""The landing page's "N payment rails" figure is the backend's rail count.

`frontend/src/lib/components/marketing/Landing.svelte` publishes the number of
payment rails as a marketing statistic, and `docs/marketing-substantiation.md`
derives it from `payment_methods.KNOWN_PAYMENT_METHODS`. A public numeric claim
is an objectively checkable one, so the two must not drift: adding or retiring
a rail fails this test until the landing figure follows.

The figure is written once, in the markup; its label is the
`marketing.stats.rails` key of the lazily loaded `marketing` locale slice, one
file per shipped language. The label must carry no number of its own, or a
translation could state a different count from the one this test pins.
"""

import re
from pathlib import Path

from app.services.payment_methods import KNOWN_PAYMENT_METHODS

FRONTEND_LIB = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib"
LANDING = FRONTEND_LIB / "components" / "marketing" / "Landing.svelte"
MARKETING_LOCALES = FRONTEND_LIB / "i18n" / "locales" / "marketing"
RAILS_LABEL = re.compile(r"""^\s*'marketing\.stats\.rails':\s*'((?:[^'\\]|\\.)*)'""", re.M)


def test_landing_payment_rail_count_matches_the_backend() -> None:
    match = re.search(
        r"value:\s*'(\d+)',\s*label:\s*m\('marketing\.stats\.rails'\)", LANDING.read_text()
    )
    assert match, "Landing.svelte no longer carries the payment-rails statistic"
    assert int(match.group(1)) == len(KNOWN_PAYMENT_METHODS)


def test_no_locale_restates_the_rail_count_in_its_label() -> None:
    locales = sorted(MARKETING_LOCALES.glob("*.ts"))
    assert len(locales) >= 6, f"marketing locale slice not found under {MARKETING_LOCALES}"
    for path in locales:
        labels = RAILS_LABEL.findall(path.read_text())
        assert len(labels) == 1, f"{path.name}: expected one 'marketing.stats.rails' label"
        assert not re.search(r"\d", labels[0]), (
            f"{path.name}: the rails label states a number; the count belongs in Landing.svelte"
        )
