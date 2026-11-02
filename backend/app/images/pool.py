"""The worker's processes for the CPU part of the photos (app.images.processing.process_image).

A ProcessPoolExecutor whose process dies (killed by the kernel for lack of memory, a crash in a C
library on a broken file) is broken for good: every photo given to it later fails at once with
BrokenProcessPool, and the queue stops while the worker goes on taking photos. So the pool is
replaced by a fresh one when that happens, and when a process hangs (`restart`).
"""

import asyncio
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any


class ImagePool:
    def __init__(self, workers: int):
        self.workers = workers
        self.restarts = 0  # since the worker started, for the admin panel
        self._executor = ProcessPoolExecutor(workers)

    async def run[T](self, fn: Callable[..., T], *args: Any) -> T:
        """`fn(*args)` in a process of the pool. Raises BrokenProcessPool when the pool broke or was
        restarted while the call waited or ran; the pool is a fresh one by then."""
        executor = self._executor
        try:
            return await asyncio.get_running_loop().run_in_executor(executor, fn, *args)
        except BrokenProcessPool:
            if executor is self._executor:  # the first call to see it: replace the pool
                self.restart()
            raise
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if executor is self._executor or (task is not None and task.cancelling()):
                raise  # the caller was cancelled
            # restart() cancels the calls still waiting in the old pool's queue: not the caller's doing
            raise BrokenProcessPool("the photo processes were restarted") from None

    def restart(self) -> None:
        """Replaces the processes by fresh ones; what runs or waits in the old ones fails with BrokenProcessPool."""
        old, self._executor = self._executor, ProcessPoolExecutor(self.workers)
        self.restarts += 1
        old.kill_workers()

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "ImagePool":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
