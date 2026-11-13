"""Render a large export into a spool file, then stream it back.

Shared by the two exports that must never be capped and never look complete
when they are not: the SOX auditor export (``api/audit``) and the DSAR
complete bundle (``api/privacy``). Both read their rows off a server-side
cursor and render them in bounded chunks; this module is where those chunks go.

Why a spool and not a straight stream: streaming the cursor directly into the
response bounds the *memory* but extends the *connection hold* from "while the
rows are read" to "until the last byte reaches the client", and the per-tenant
pool is ``pool_size=5, max_overflow=10`` — a handful of concurrent large exports
over slow links could exhaust it. Draining into a ``SpooledTemporaryFile``
first keeps the short hold without giving back the memory bound (the file is
:data:`SPOOL_MAX_MEMORY_BYTES` of RAM, then disk), and lets the caller release
its sessions — and, for the DSAR bundle, commit its audit row — before
transmission starts. A database error part-way through surfaces as a clean 500
before any byte is sent.

The temp file is an anonymous one (``tempfile.TemporaryFile`` underneath — on
POSIX unlinked at creation), and :func:`drain_spool` closes it on both the
finished and the abandoned path, so nothing of an export outlives its response.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator

# The spool keeps at most this much of a rendered export in memory; past it
# `SpooledTemporaryFile` rolls the rest onto disk, so peak RSS is bounded by
# this number no matter how large the export is. A small export never touches
# the filesystem at all.
SPOOL_MAX_MEMORY_BYTES = 1024 * 1024

# Block size the finished export is handed back to the transport in. Small
# enough that the response is genuinely chunked (an aborted body has no
# terminating chunk), large enough that a 40k-row export is a few hundred reads
# rather than a few hundred thousand.
SPOOL_READ_BYTES = 64 * 1024


async def spool_body(chunks: AsyncIterator[str]) -> tempfile.SpooledTemporaryFile:
    """Render an export body into a spool file, positioned at its start.

    Writes go through ``asyncio.to_thread`` because a spool that has rolled over
    is a real file and a synchronous ``write`` would sit on the event loop for
    its duration (project invariant: no blocking work on the loop).
    """
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY_BYTES, mode="w+b")
    try:
        async for chunk in chunks:
            await asyncio.to_thread(spool.write, chunk.encode("utf-8"))
        await asyncio.to_thread(spool.seek, 0)
    except BaseException:
        # Includes cancellation: nothing downstream will ever be handed this
        # file, so it is this frame's job to unlink it.
        spool.close()
        raise
    return spool


async def drain_spool(spool: tempfile.SpooledTemporaryFile) -> AsyncIterator[bytes]:
    """Emit a spooled body in bounded blocks, deleting the file when done.

    The ``finally`` covers both exits that matter: the last block being read,
    and the client disconnecting mid-download — Starlette closes the body
    generator, which raises ``GeneratorExit`` here, so the temp file is unlinked
    on the abandoned path too rather than waiting for garbage collection.
    """
    try:
        while True:
            block = await asyncio.to_thread(spool.read, SPOOL_READ_BYTES)
            if not block:
                return
            yield block
    finally:
        spool.close()
