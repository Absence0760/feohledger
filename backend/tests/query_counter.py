"""`QueryCounter` — records every SQL statement a block of test code causes.

Shared by the statement-count guards (`test_vendor_list_query_counts.py`,
`test_list_endpoint_query_counts.py`, `test_dashboard_processing_time.py`).
Their common assertion is that a read's cost does not grow with the data it
reads: neither the NUMBER of statements (an N+1 grows that with the page) nor
the number of BIND PARAMETERS in any one of them (an `IN (:id_1, … :id_n)`
built from a previous result grows that with the table — and past asyncpg's
32 767-parameter ceiling it stops being slow and starts failing).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from sqlalchemy import event
from sqlalchemy.engine import Engine


class QueryCounter:
    """Records every SQL statement executed while the block is open.

    Listens on the ``Engine`` *class*, so it captures the request-path engines
    the `realdb` client builds internally as well as any seeding session — the
    counter needs no cooperation from the harness. Async engines dispatch these
    events on their underlying sync engine, so class-level listening is the one
    hook that sees them all.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.param_counts: list[int] = []

    def __enter__(self) -> QueryCounter:
        event.listen(Engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(Engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ARG002
        self.statements.append(" ".join(statement.split()))
        if isinstance(parameters, Mapping | Sequence) and not isinstance(parameters, str):
            self.param_counts.append(len(parameters))
        else:
            self.param_counts.append(0)

    def matching(self, pattern: str) -> list[str]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [s for s in self.statements if rx.search(s)]

    def count_matching(self, pattern: str) -> int:
        return len(self.matching(pattern))

    @property
    def max_params(self) -> int:
        """The most bind parameters any one recorded statement carried."""
        return max(self.param_counts, default=0)

    def __len__(self) -> int:
        return len(self.statements)
