# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Completion-ordered streaming over a thread pool — the P4 mechanism.

The upstream RFC asks for chunks to be delivered *as they complete* so the
engine can start the GPU copy of chunk i while chunk i+1 is still being read.
The original plan assumed that needed DAOS event queues. Measurement said
otherwise:

  * DAOS event queues give asynchronous completion *notification*, not
    concurrent *execution*. Throughput is pinned near 7–12 GB/s no matter how
    the queues and pollers are arranged, because every submit and completion on
    an EQ serialises on that EQ's ``eqx_lock``, while more EQs each cost a new
    network context (documented upstream, DAOS 2.8
    ``src/client/api/README.md``).
  * the blocking path has no EQ at all and scales with threads: 14.9 GB/s at one
    thread to 35.3 GB/s at sixteen, and 34.3 GB/s on a 100 GB NVMe-resident
    working set.

So the pool of blocking readers we already have *is* the fast path, and
completion-ordered delivery falls out of it for free: a worker finishes its read
and its future resolves. This module is that, and nothing more — no event ABI,
no pending table, no lifetime contract.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Iterable, Tuple


async def stream_completions(
    loop: asyncio.AbstractEventLoop,
    executor,
    fn: Callable[[Any], Any],
    items: Iterable[Any],
    max_inflight: int = 16,
) -> AsyncIterator[Tuple[int, Any]]:
    """Run ``fn`` over ``items`` on ``executor``; yield ``(index, result)``
    in **completion order**.

    ``index`` is the position of the item in ``items``, so a consumer can copy
    into the right slot without the stream having to preserve order — which is
    the whole point: yielding strictly in index order would let one slow chunk
    head-of-line block everything behind it.

    ``max_inflight`` bounds concurrent work. Without it every chunk would be
    submitted (and its destination allocated) up front, which defeats the memory
    benefit of streaming. A slot is refilled *before* the completed result is
    yielded, so reads keep flowing while the consumer is busy with the chunk it
    just received.

    A callable that raises does not abort the stream: the exception is yielded in
    place of the result for that index. Per-chunk error reporting is what the
    RFC asks for — one chunk failing should not invalidate the batch.
    """
    if max_inflight < 1:
        raise ValueError("max_inflight must be >= 1")

    pending: dict = {}
    it = enumerate(items)
    exhausted = False

    def submit_one() -> bool:
        nonlocal exhausted
        try:
            idx, item = next(it)
        except StopIteration:
            exhausted = True
            return False
        pending[loop.run_in_executor(executor, fn, item)] = idx
        return True

    while len(pending) < max_inflight and not exhausted:
        submit_one()

    while pending:
        done, _ = await asyncio.wait(
            list(pending.keys()), return_when=asyncio.FIRST_COMPLETED)
        for fut in done:
            idx = pending.pop(fut)
            try:
                res = fut.result()
            except Exception as exc:  # surfaced per chunk, not per batch
                res = exc
            # Refill first: keep the readers busy across the consumer's await.
            if not exhausted and len(pending) < max_inflight:
                submit_one()
            yield idx, res
