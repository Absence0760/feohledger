"""`app/utils/detached_tasks` — the shared registry behind the forgot-password
routes' detached mint / send / audit, and its bounded shutdown drain."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.utils import detached_tasks


@pytest.mark.asyncio
async def test_drain_waits_for_work_a_detached_task_spawns():
    done: list[str] = []

    async def _child() -> None:
        await asyncio.sleep(0)
        done.append("child")

    async def _parent() -> None:
        detached_tasks.spawn_detached(_child(), name="test.child")
        done.append("parent")

    detached_tasks.spawn_detached(_parent(), name="test.parent")
    assert await detached_tasks.drain_detached() == 0
    assert done == ["parent", "child"]
    assert not detached_tasks._tasks


@pytest.mark.asyncio
async def test_a_bounded_drain_cancels_and_counts_what_it_abandons(caplog):
    gate = asyncio.Event()
    task = detached_tasks.spawn_detached(gate.wait(), name="test.hung")

    with caplog.at_level(logging.WARNING, logger="app.utils.detached_tasks"):
        abandoned = await detached_tasks.drain_detached(timeout=0.05)
    assert abandoned == 1
    assert "abandoned 1 detached task" in caplog.text
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not detached_tasks._tasks


@pytest.mark.asyncio
async def test_a_failing_task_is_logged_by_name_and_class_only(caplog):
    async def _boom() -> None:
        raise ConnectionError("smtp refused someone@example.com")

    with caplog.at_level(logging.ERROR, logger="app.utils.detached_tasks"):
        detached_tasks.spawn_detached(_boom(), name="test.boom")
        await detached_tasks.drain_detached()
    assert "test.boom" in caplog.text
    assert "ConnectionError" in caplog.text
    assert "someone@example.com" not in caplog.text
