"""Fire-and-forget tasks that outlive the request which started them.

The two forgot-password routes (`api/auth.py` and `api/portal_auth.py`) run
everything only a MATCHING address does (token mint, send, audit row) on a
task spawned here, so a known address answers in the same time as an unknown
one (`docs/decisions.md` §451). One registry for both, because each copy kept
its own set and its tests drained it by reaching into the module.

The event loop only holds tasks weakly; without a strong reference a task can
be garbage-collected mid-flight. The set below is that reference, released as
each task finishes. The coroutine owns its own error handling: a task that
raises is logged here by name only, never with its arguments, since those can
carry an address or a link.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def _finish(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Detached task %s failed: %s",
            task.get_name(),
            type(task.exception()).__name__,
        )


def spawn_detached(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task:
    """Start ``coro`` without awaiting it, holding it until it completes."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_finish)
    return task


#: How long a graceful shutdown waits for detached tasks (a reset link's mint,
#: send and audit). Bounded for the reason `audit_dispatch`'s auth-audit drain
#: is: a hung send must cost the shutdown a known amount, not block it.
DETACHED_DRAIN_TIMEOUT_SECONDS = 5.0


async def drain_detached(*, timeout: float | None = None) -> int:
    """Wait for every detached task, including any one of them spawns.

    The completion signal a test awaits before reading what a detached task
    wrote (an outbox, an audit row) — never a sleep — and the lifespan
    shutdown's flush, so a clean stop does not drop a link mid-send. Past
    ``timeout`` the remainder is cancelled, counted at WARNING and returned;
    ``None`` (the test default) waits indefinitely.
    """
    if timeout is None:
        while _tasks:
            await asyncio.gather(*list(_tasks), return_exceptions=True)
        return 0

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _tasks:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.wait(list(_tasks), timeout=remaining)
    abandoned = list(_tasks)
    for task in abandoned:
        task.cancel()
    if abandoned:
        logger.warning("Shutdown abandoned %d detached task(s)", len(abandoned))
    return len(abandoned)
