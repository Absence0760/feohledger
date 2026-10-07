"""The landing page's "N payment rails" figure is the backend's rail count.

`frontend/src/lib/components/marketing/Landing.svelte` publishes the number of
payment rails as a marketing statistic, and `docs/marketing-substantiation.md`
derives it from `payment_methods.KNOWN_PAYMENT_METHODS`. A public numeric claim
is an objectively checkable one, so the two must not drift: adding or retiring
a rail fails this test until the landing figure follows.
"""

import re
from pathlib import Path

from app.services.payment_methods import KNOWN_PAYMENT_METHODS

LANDING = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "lib"
    / "components"
    / "marketing"
    / "Landing.svelte"
)


def test_landing_payment_rail_count_matches_the_backend() -> None:
    match = re.search(r"value:\s*'(\d+)',\s*label:\s*'payment rails", LANDING.read_text())
    assert match, "Landing.svelte no longer carries the payment-rails statistic"
    assert int(match.group(1)) == len(KNOWN_PAYMENT_METHODS)
