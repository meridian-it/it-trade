"""Background worker: periodic supplier syncs and housekeeping + continuous image processing.

    python -m app.worker
"""

import asyncio
import logging
import math
import os
import signal
import statistics
import time
import traceback
from collections.abc import Awaitable, Callable, Collection
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import SessionLocal
from app.images.pool import ImagePool
from app.images.processing import ProcessedImage
from app.models import JobState
from app.models.base import utcnow
from app.site_settings import ImageSettings, SyncSettings, get_settings_obj
from app.supplier.client import SupplierClient, SupplierUnavailable
from app.sync.categories import sync_categories
from app.sync.images import CRASHED, HUNG, NETWORK, next_pending, pick_trial, process_one, record_failure, sync_image_lists
from app.sync.products import sync_products
from app.sync.stock import sync_stock
from app.version import code_fingerprint

log = logging.getLogger("worker")
RETRY_AFTER = timedelta(minutes=10)
RETRY = "retry"  # info flag of a failed job: run it again at once (the "Повторить сейчас" button)
# job_state rows that are not jobs: the worker's heartbeat and the trial batch asked for in the admin panel
WORKER, TRIAL = "worker", "trial"
HEARTBEAT_EVERY = 30  # seconds; the admin panel calls the worker stopped after a few missed ones
IDLE_WAIT = 10  # seconds between looks at the settings and the trial while there is no work
# An empty queue is looked at again after this long (~1 s of database work on the whole catalog), or
# at once after a supplier sync or a change of the settings: those are what bring new work.
EMPTY_QUEUE_WAIT = 60
BATCH = 60  # photos taken from the queue at a time
STALL_AFTER = 300  # seconds without any photo of a batch finished: a photo process hangs
POSTPONE = 600  # seconds a photo the supplier did not send waits before it is taken again
SUPPLIER_SEEN = 600  # the supplier sent a photo this recently: one it does not send is that file's problem
CRASHES_IN_A_ROW = 3  # photos that kill their process one after another: blame the server (memory), not them
CRASH_PAUSE = 300  # seconds the photos wait then


LISTS_EVERY = timedelta(minutes=2)


@dataclass
class Job:
    name: str
    run: Callable[[SupplierClient], Awaitable[dict]]
    every: Callable[[SyncSettings], timedelta] | None = None  # an interval since the last start...
    catalog: bool = False  # ...or the catalog slots (catalog_slot)
    after: str | None = None  # do not start before this job has succeeded at least once


def jobs() -> list[Job]:
    return [
        Job("categories", sync_categories, catalog=True),
        Job("products", sync_products, catalog=True, after="categories"),
        Job("stock", sync_stock, every=lambda c: timedelta(minutes=c.stock_every_min), after="products"),
        Job("image_lists", sync_image_lists, every=lambda c: LISTS_EVERY, after="products"),
    ]


def catalog_slot(now: datetime, cfg: SyncSettings) -> datetime:
    """The latest catalog slot at or before `now`: catalog_hour + k * catalog_every_hours, local time.
    The interval divides 24, so the slots fall on the same hours every day (05:00, 11:00, 17:00, 23:00)."""
    local = now.astimezone(ZoneInfo(get_settings().timezone))
    n = cfg.catalog_every_hours
    first = cfg.catalog_hour % n
    if local.hour >= first:
        day, hour = local, first + (local.hour - first) // n * n
    else:  # before the first slot of the day: the last one of the day before
        day, hour = local - timedelta(days=1), first + (23 - first) // n * n
    return day.replace(hour=hour, minute=0, second=0, microsecond=0)


def scheduled_at(job: Job, st: JobState, now: datetime, cfg: SyncSettings) -> datetime:
    """When a job that has run before is due by its schedule (at or before `now`: due already)."""
    if job.catalog:
        slot = catalog_slot(now, cfg)
        done = st.last_success_at is not None and st.last_success_at >= slot
        return slot + timedelta(hours=cfg.catalog_every_hours) if done else slot
    return st.last_started_at + job.every(cfg)


def is_due(
    job: Job,
    st: JobState | None,
    now: datetime,
    states: dict[str, JobState],
    started_here: set[str] = frozenset(),
    cfg: SyncSettings | None = None,
) -> bool:
    if job.after and not (states.get(job.after) and states[job.after].last_success_at):
        return False
    if st is None or st.last_started_at is None:
        return True
    # started by a previous worker process that was stopped before the job finished
    interrupted = (st.last_finished_at is None or st.last_finished_at < st.last_started_at) and job.name not in started_here
    if interrupted:
        return True
    failed = st.last_success_at is None or st.last_success_at < st.last_started_at
    if failed:
        # at once when asked from the admin panel, or by a new worker process: it is usually restarted after
        # the cause was fixed (e.g. the supplier's password in .env, which is read at start); else after a pause
        if st.info.get(RETRY) or job.name not in started_here:
            return True
        return now - st.last_started_at >= RETRY_AFTER
    return now >= scheduled_at(job, st, now, cfg or SyncSettings())


async def run_job(job: Job, client: SupplierClient) -> None:
    async with SessionLocal() as session:
        st = await session.get(JobState, job.name) or JobState(name=job.name, info={})
        st.last_started_at = utcnow()
        st.info = {k: v for k, v in (st.info or {}).items() if k != RETRY}
        session.add(st)
        await session.commit()
    log.info("job %s: started", job.name)
    try:
        info = await job.run(client)
        error = None
    except Exception as e:  # noqa: BLE001 - keep the worker alive, record the error
        log.exception("job %s failed", job.name)
        info, error = None, "".join(traceback.format_exception_only(e)).strip()[:2000]
    async with SessionLocal() as session:
        st = await session.get(JobState, job.name)
        st.last_finished_at = utcnow()
        st.last_error = error
        if error is None:
            st.last_success_at = st.last_finished_at
            st.info = info or {}
        await session.commit()
    log.info("job %s: %s %s", job.name, "failed" if error else "done", info or error)


async def scheduler(client: SupplierClient, stop: asyncio.Event, state: dict) -> None:
    all_jobs = jobs()
    started_here: set[str] = set()
    while not stop.is_set():
        async with SessionLocal() as session:
            states = {j.name: await session.get(JobState, j.name) for j in all_jobs}
            cfg = await get_settings_obj(session, SyncSettings)  # edited in the admin panel: read every round
        now = utcnow()
        for job in all_jobs:
            if stop.is_set():
                break
            if is_due(job, states[job.name], now, states, started_here, cfg):
                started_here.add(job.name)
                await run_job(job, client)
                state["queue_changed"] = True  # new products, photo lists or stock: maybe new photos
                async with SessionLocal() as session:
                    states[job.name] = await session.get(JobState, job.name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


@dataclass
class PhotoQueue:
    """What the photo loop remembers between batches."""

    postponed: dict[int, float] = field(default_factory=dict)  # photo id -> monotonic time it may be taken again
    supplier_seen: float = -math.inf  # monotonic time the supplier last sent a photo

    def postpone(self, image_id: int) -> None:
        self.postponed[image_id] = time.monotonic() + POSTPONE

    def excluded(self) -> list[int]:
        now = time.monotonic()
        self.postponed = {iid: t for iid, t in self.postponed.items() if t > now}
        return list(self.postponed)


@dataclass
class BatchResult:
    processed: int = 0
    failed: int = 0  # an attempt of the photo used up (it leaves the queue after MAX_ATTEMPTS)
    unsent: int = 0  # not sent by the supplier (network trouble): put aside for POSTPONE
    left: int = 0  # not tried to the end: the photo processes keep crashing
    problem: tuple[str, str] | None = None  # (kind, detail) for the admin panel (app.api.admin.loading.PROBLEMS)

    @property
    def answered(self) -> bool:
        """The supplier sent at least one of the photos."""
        return bool(self.processed or self.failed)


async def image_loop(client: SupplierClient, pool: ImagePool, stop: asyncio.Event, state: dict) -> None:
    s = get_settings()
    queue = PhotoQueue()
    last_cfg, next_look = None, 0.0  # when to look at the queue again
    while not stop.is_set():
        async with SessionLocal() as session:
            cfg = await get_settings_obj(session, ImageSettings)
        client.set_image_rps(cfg.download_rps)
        state.update(paused=cfg.paused, download_rps=cfg.download_rps)
        if cfg != last_cfg or state.pop("queue_changed", False):
            last_cfg, next_look = cfg, 0.0
        if await run_trial(client, pool, state):
            continue
        batch = []
        if not cfg.paused and time.monotonic() >= next_look:
            batch = await next_pending(BATCH, cfg, include_out_of_stock=s.image_prefetch_out_of_stock, exclude=queue.excluded())
            if not batch:
                next_look = time.monotonic() + EMPTY_QUEUE_WAIT
        state["activity"] = "photos" if batch else "paused" if cfg.paused else "idle"
        if not batch:
            await _sleep(stop, IDLE_WAIT)
            continue
        res = await process_batch(client, pool, batch, queue, stop)
        log.info("images: %d processed, %d failed, %d not sent by the supplier", res.processed, res.failed, res.unsent)
        if res.processed:
            state["photo_at"] = utcnow().isoformat()
        if res.problem:
            kind, detail = res.problem
            state["problem"] = {"kind": kind, "detail": detail[:300], "at": utcnow().isoformat()}
        state["pool_restarts"] = pool.restarts
        if res.left:
            await _sleep(stop, CRASH_PAUSE)
        elif res.unsent and not res.answered:
            await _sleep(stop, 60)  # give the supplier a break


async def process_batch(
    client: SupplierClient, pool: ImagePool, batch: list[tuple[int, str]], queue: PhotoQueue, stop: asyncio.Event | None = None
) -> BatchResult:
    """Processes the photos together (downloads paced by the client's rate limiter, CPU work in the pool)
    and sees that no photo can stop the queue, which takes the same first photos again until they leave it:
    - a process that hangs (no photo finished for STALL_AFTER) is killed with the others of the pool;
    - the photos that were in a pool that broke (a process died: the kernel kills one that takes too much
      memory) or was killed are processed again one at a time, so the one that breaks the pool on its own
      uses up an attempt and the others are not blamed;
    - a photo the supplier does not send is put aside for POSTPONE, and uses up an attempt if the supplier
      sends other photos meanwhile (the trouble is with that file, not the network)."""
    res = BatchResult()
    urls = dict(batch)
    tasks = {asyncio.ensure_future(process_one(client, pool, iid, url)): iid for iid, url in batch}
    if await _wait_watched(tasks, pool):
        res.problem = ("hung", "")
    broken: list[int] = []
    unavailable: dict[int, str] = {}
    for task, iid in tasks.items():
        if task.cancelled():
            unavailable[iid] = f"image {urls[iid]}: NoAnswer"  # cancelled by _wait_watched
        elif (e := task.exception()) is None:
            if task.result() is None:
                res.failed += 1
            else:
                res.processed += 1
        elif isinstance(e, SupplierUnavailable):
            unavailable[iid] = str(e)
        elif isinstance(e, BrokenProcessPool):
            broken.append(iid)
        else:  # failed after the processing, saving it: tried again, at most MAX_ATTEMPTS times
            log.error("image %s: crashed", iid, exc_info=e)
            await record_failure(iid, e)
            res.failed += 1
            res.problem = res.problem or ("error", f"{type(e).__name__}: {e}")
    if broken:
        res.problem = res.problem or ("pool_broken", "")
        log.error("images: the photo processes broke, %d photos are processed again one at a time", len(broken))
    crashes = 0
    for k, iid in enumerate(broken):
        if crashes >= CRASHES_IN_A_ROW or (stop is not None and stop.is_set()):
            res.left = len(broken) - k  # still queued, not blamed
            if crashes >= CRASHES_IN_A_ROW:
                res.problem = ("crashes", str(crashes))
            break
        try:
            r = await asyncio.wait_for(process_one(client, pool, iid, urls[iid]), STALL_AFTER)
        except SupplierUnavailable as e:
            unavailable[iid] = str(e)
            continue
        except BrokenProcessPool:  # broke a fresh pool on its own (replaced already)
            log.error("image %s (%s) kills the photo process", iid, urls[iid])
            await record_failure(iid, f"{CRASHED}: image {urls[iid]}")
            res.failed += 1
            crashes += 1
            continue
        except TimeoutError:
            log.error("image %s (%s) hangs the photo process", iid, urls[iid])
            pool.restart()
            await record_failure(iid, f"{HUNG}: image {urls[iid]}")
            res.failed += 1
            crashes += 1
            continue
        except Exception as e:  # noqa: BLE001 - as above: saving it failed
            log.error("image %s: crashed", iid, exc_info=e)
            await record_failure(iid, e)
            res.failed += 1
            continue
        crashes = 0
        if r is None:
            res.failed += 1
        else:
            res.processed += 1

    now = time.monotonic()
    if res.answered:
        queue.supplier_seen = now
    files_to_blame = now - queue.supplier_seen < SUPPLIER_SEEN
    for iid, error in unavailable.items():
        queue.postpone(iid)
        if files_to_blame:
            await record_failure(iid, f"{NETWORK}: {error}")
    res.unsent = len(unavailable)
    if unavailable and not res.answered:
        res.problem = res.problem or ("supplier", next(iter(unavailable.values())))
    return res


async def _wait_watched(tasks: Collection[asyncio.Future], pool: ImagePool) -> bool:
    """Waits for the tasks. When none of them finishes for STALL_AFTER, a photo process hangs: the pool is
    restarted (the photos in it fail with BrokenProcessPool); when still none finishes, the rest hang in
    their downloads and are cancelled. Returns whether the pool was restarted."""
    pending, restarted = set(tasks), False
    while pending:
        done, pending = await asyncio.wait(pending, timeout=STALL_AFTER, return_when=asyncio.FIRST_COMPLETED)
        if done:
            continue
        if not restarted:
            log.error("images: none of %d photos finished in %d s, restarting the photo processes", len(pending), STALL_AFTER)
            pool.restart()
            restarted = True
            continue
        log.error("images: %d photo downloads hang, cancelled", len(pending))
        for t in pending:
            t.cancel()
        await asyncio.wait(pending)
        break
    return restarted


async def run_trial(client: SupplierClient, pool: ImagePool | None, state: dict) -> bool:
    """Processes the trial batch asked for in the admin panel, if there is one: random main photos of
    in-stock products, with the speed and the CPU time measured. Returns whether there was one."""
    async with SessionLocal() as session:
        st = await session.get(JobState, TRIAL)
        if st is None or st.info.get("status") != "queued":
            return False
        photos = await pick_trial(int(st.info.get("requested") or 0))
        st.last_started_at = utcnow()
        st.last_error = None
        st.info = {**st.info, "status": "running", "ids": [iid for iid, _ in photos], "count": len(photos), "done": 0, "failed": 0}
        await session.commit()
    log.info("trial batch: %d photos", len(photos))
    state["activity"] = "trial"
    started, times, failed = time.monotonic(), [], 0
    # all at once, as the queue would: downloads paced by the rate limiter keep the pool busy
    tasks = [asyncio.ensure_future(process_one(client, pool, iid, url)) for iid, url in photos]
    for k, task in enumerate(asyncio.as_completed(tasks), 1):
        try:
            res = await task
        except Exception:  # noqa: BLE001 - counted as failed: network trouble or a crash
            log.exception("trial photo failed")
            res = None
        if isinstance(res, ProcessedImage):
            times.append(res.ms)
        else:
            failed += 1
        if k % 10 == 0 or k == len(tasks):
            async with SessionLocal() as session:
                st = await session.get(JobState, TRIAL)
                st.info = {**st.info, "done": len(times), "failed": failed}
                await session.commit()
    elapsed = time.monotonic() - started
    async with SessionLocal() as session:
        st = await session.get(JobState, TRIAL)
        st.last_finished_at = st.last_success_at = utcnow()
        st.info = {
            **st.info,
            "status": "done",
            "seconds": round(elapsed, 1),
            "ms_median": round(statistics.median(times)) if times else None,
            "download_rps": round(client.image_rps, 2),
            "image_workers": get_settings().image_workers,
        }
        await session.commit()
    log.info("trial batch: %d done, %d failed in %.0f s", len(times), failed, elapsed)
    return True


async def heartbeat(stop: asyncio.Event, state: dict) -> None:
    """Tells the admin panel that the worker runs and what it does (job_state row "worker")."""
    s = get_settings()
    started = utcnow()
    code = code_fingerprint()  # the code this process runs, whatever changes on disk later
    async with SessionLocal() as session:
        # a trial cut short by a restart is not resumed: a new one can be started
        trial = await session.get(JobState, TRIAL)
        if trial is not None and trial.info.get("status") == "running":
            trial.info = {**trial.info, "status": "interrupted"}
            trial.last_finished_at = started
        await session.commit()
    while not stop.is_set():
        async with SessionLocal() as session:
            st = await session.get(JobState, WORKER) or JobState(name=WORKER, info={})
            st.last_started_at = started
            st.info = {
                "heartbeat": utcnow().isoformat(),
                "pid": os.getpid(),
                "cpus": os.cpu_count(),
                "image_workers": s.image_workers,
                "code": code,
                **{k: v for k, v in state.items() if k != "queue_changed"},
            }
            session.add(st)
            await session.commit()
        await _sleep(stop, HEARTBEAT_EVERY)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings()
    s.media_root.mkdir(parents=True, exist_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    async with SupplierClient(s) as client:
        if not client.has_credentials:
            log.warning("SUPPLIER_LOGIN/SUPPLIER_PASSWORD are empty: price/stock/image syncs will fail")
        with ImagePool(s.image_workers) as pool:
            state: dict = {"activity": "starting"}
            await asyncio.gather(scheduler(client, stop, state), image_loop(client, pool, stop, state), heartbeat(stop, state))


if __name__ == "__main__":
    asyncio.run(main())
