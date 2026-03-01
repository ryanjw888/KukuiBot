"""parallel.py — Asyncio worker pool for host-level parallelism."""

import asyncio
from typing import Any, Callable, Coroutine


async def run_parallel(
    tasks: list[Coroutine],
    max_workers: int = 8,
    on_complete: Callable[[int, Any], None] | None = None,
) -> list[Any]:
    """Run coroutines with bounded concurrency. Returns results in input order."""
    semaphore = asyncio.Semaphore(max_workers)
    results: list[Any] = [None] * len(tasks)
    errors: list[Exception | None] = [None] * len(tasks)

    async def wrapper(idx: int, coro: Coroutine):
        async with semaphore:
            try:
                result = await coro
                results[idx] = result
                if on_complete:
                    on_complete(idx, result)
            except Exception as e:
                errors[idx] = e
                results[idx] = None

    await asyncio.gather(*(wrapper(i, t) for i, t in enumerate(tasks)))
    return results


async def run_parallel_dict(
    task_map: dict[str, Coroutine],
    max_workers: int = 8,
) -> dict[str, Any]:
    """Run named coroutines with bounded concurrency. Returns {name: result}."""
    semaphore = asyncio.Semaphore(max_workers)
    results: dict[str, Any] = {}

    async def wrapper(name: str, coro: Coroutine):
        async with semaphore:
            try:
                results[name] = await coro
            except Exception as e:
                results[name] = {"error": str(e)}

    await asyncio.gather(*(wrapper(n, c) for n, c in task_map.items()))
    return results
