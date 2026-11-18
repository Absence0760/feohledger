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


async def drain_detached() -> None:
    """Wait for every detached task, including any one of them spawns.

    The completion signal a test awaits before reading what a detached task
    wrote (an outbox, an audit row) — never a sleep.
    """
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
