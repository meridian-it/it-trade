"""The photo queue never stands: a photo process that dies or hangs, a photo the processing cannot handle
or the supplier does not send. On production one such photo kept the worker taking the same photos for days
while the admin page showed "3 hours left"."""

import asyncio
import io
import os
import time
from concurrent.futures.process import BrokenProcessPool
from datetime import timedelta

import pytest
from conftest import login, reset_db
from PIL import Image
from sqlalchemy import select

from app import worker
from app.api.admin import loading
from app.auth import ensure_admin
from app.db import SessionLocal
from app.images.pool import ImagePool
from app.models import Category, JobState, Product, ProductImage
from app.models.base import utcnow
from app.models.catalog import ImageStatus
from app.site_settings import ImageSettings
from app.supplier.client import SupplierUnavailable
from app.sync.images import MAX_ATTEMPTS, next_pending
from app.worker import WORKER, PhotoQueue, process_batch

SKUS = range(1, 7)  # in stock, two photos each: id = sku * 100 + position, the first one is the main photo


@pytest.fixture(autouse=True)
async def db():
    await reset_db()
    loading.clear_cache()
    async with SessionLocal() as s:
        await ensure_admin(s)
        s.add(Category(id=1, name="Cat", slug="cat", path="/1/"))
        await s.flush()
        for sku in SKUS:
            s.add(Product(sku=sku, name=f"P{sku}", slug=f"p{sku}", category_id=1, search_text=f"p{sku}", in_stock=True, has_image=True, price=100))
            await s.flush()
            s.add_all(ProductImage(id=sku * 100 + pos, sku=sku, source_url=f"{sku}/{pos}.jpg", position=pos) for pos in range(2))
            await s.flush()
            p = await s.get(Product, sku)
            p.main_image_id, p.images_synced_at = sku * 100, utcnow()
        await s.commit()


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 300), "white").save(buf, "JPEG")
    return buf.getvalue()


class Supplier:
    def __init__(self, unsent: set[int] = frozenset()):
        self.unsent = unsent  # photos whose download fails with network trouble

    async def fetch_image(self, url, size="original"):
        if int(url.replace("/", "0")[:-4]) in self.unsent:  # "3/0.jpg" -> 300
            raise SupplierUnavailable(f"image {url}: ReadTimeout")
        return jpeg()


class FakePool:
    """ImagePool in the event loop: a photo in `kills` kills its process, which breaks the pool for every
    photo in it at that moment; a photo in `hangs` never finishes until the pool is restarted; a photo in
    `bugs` raises an error the processing does not expect."""

    def __init__(self, kills=(), hangs=(), bugs=()):
        self.kills, self.hangs, self.bugs = set(kills), set(hangs), set(bugs)
        self.restarts = 0
        self._restarted = asyncio.Event()

    async def run(self, fn, *args):
        image_id, restarted = args[2], self._restarted
        await asyncio.sleep(0.01)  # in the pool together with the others of the batch
        if image_id in self.hangs:
            await restarted.wait()
        if image_id in self.kills and not restarted.is_set():
            self.restart()
        if restarted.is_set():
            raise BrokenProcessPool("a child process terminated abruptly")
        if image_id in self.bugs:
            raise IndexError("index 3 is out of bounds")
        return fn(*args)

    def restart(self):
        self.restarts += 1
        self._restarted.set()
        self._restarted = asyncio.Event()


async def photos() -> dict[int, ProductImage]:
    async with SessionLocal() as s:
        return {p.id: p for p in (await s.scalars(select(ProductImage))).all()}


async def main_photos() -> list[tuple[int, str]]:
    return await next_pending(60, ImageSettings(gallery=False))


async def test_a_photo_that_kills_its_process_is_found_and_the_others_are_not_blamed():
    pool = FakePool(kills={300})
    res = await process_batch(Supplier(), pool, await main_photos(), PhotoQueue())
    # the batch broke: the photos of the broken pool went again one at a time
    assert (res.processed, res.failed, res.problem) == (5, 1, ("pool_broken", ""))
    p = await photos()
    assert p[300].attempts == 1 and p[300].error.startswith("crashed: ") and p[300].status == ImageStatus.pending
    assert all(p[sku * 100].status == ImageStatus.ready and p[sku * 100].attempts == 0 for sku in SKUS if sku != 3)

    for _ in range(MAX_ATTEMPTS - 1):  # it is first in the queue until its attempts are used up
        assert [iid for iid, _ in await main_photos()] == [300]
        await process_batch(Supplier(), pool, await main_photos(), PhotoQueue())
    assert (await photos())[300].status == ImageStatus.failed
    async with SessionLocal() as s:
        assert (await s.get(Product, 3)).main_image_id == 301  # the next photo is shown instead
    assert await main_photos() == []


async def test_photos_that_keep_killing_their_process_pause_the_queue_instead_of_failing_it_all():
    pool = FakePool(kills={sku * 100 for sku in SKUS})  # e.g. the server has no memory for any photo
    res = await process_batch(Supplier(), pool, await main_photos(), PhotoQueue())
    assert res.problem == ("crashes", "3") and res.left == 3 and res.failed == 3
    assert sorted(iid for iid, p in (await photos()).items() if p.attempts) == [100, 200, 300]


async def test_a_hanging_process_is_restarted(monkeypatch):
    monkeypatch.setattr(worker, "STALL_AFTER", 0.2)
    pool = FakePool(hangs={200})
    res = await process_batch(Supplier(), pool, await main_photos(), PhotoQueue())
    assert (res.processed, res.failed, res.problem) == (5, 1, ("hung", ""))
    p = await photos()
    assert p[200].attempts == 1 and p[200].error.startswith("hung: ")
    assert pool.restarts == 2  # when it hung in the batch and when it hung alone


async def test_an_unexpected_error_of_the_processing_uses_up_an_attempt():
    pool = FakePool(bugs={100})
    res = await process_batch(Supplier(), pool, await main_photos(), PhotoQueue())
    assert (res.processed, res.failed) == (5, 1)
    assert (await photos())[100].error == "IndexError: index 3 is out of bounds"


async def test_a_file_the_supplier_does_not_send_is_put_aside():
    queue = PhotoQueue()
    res = await process_batch(Supplier(unsent={100}), FakePool(), await main_photos(), queue)
    # the supplier sent the others: the trouble is with that file, an attempt is used up
    assert (res.processed, res.failed, res.unsent) == (5, 0, 1)
    assert (await photos())[100].attempts == 1 and (await photos())[100].error.startswith("network: ")
    assert 100 in queue.excluded()
    assert await next_pending(60, ImageSettings(gallery=False), exclude=queue.excluded()) == []


async def test_when_the_supplier_sends_nothing_photos_wait_without_using_attempts():
    queue = PhotoQueue()
    res = await process_batch(Supplier(unsent={sku * 100 for sku in SKUS}), FakePool(), await main_photos(), queue)
    assert (res.processed, res.unsent, res.problem) == (0, 6, ("supplier", "image 1/0.jpg: ReadTimeout"))
    assert all(p.attempts == 0 for p in (await photos()).values())
    assert len(queue.excluded()) == 6


# --- the real pool ------------------------------------------------------------------------------


async def test_pool_replaces_a_process_that_died():
    with ImagePool(1) as pool:
        assert await pool.run(abs, -3) == 3
        with pytest.raises(BrokenProcessPool):
            await pool.run(os._exit, 1)  # like a process killed by the kernel for memory
        assert pool.restarts == 1
        assert await pool.run(abs, -4) == 4  # a broken ProcessPoolExecutor would fail every call from now on


async def test_pool_restart_fails_the_calls_in_it_and_stops_a_hanging_process():
    with ImagePool(1) as pool:
        hanging = asyncio.ensure_future(pool.run(time.sleep, 60))
        waiting = asyncio.ensure_future(pool.run(abs, -1))  # in the queue behind it
        await asyncio.sleep(0.5)
        pool.restart()
        for call in (hanging, waiting):
            with pytest.raises(BrokenProcessPool):  # not CancelledError: the caller was not cancelled
                await asyncio.wait_for(call, 10)
        assert await pool.run(abs, -2) == 2


# --- the admin page -----------------------------------------------------------------------------


@pytest.fixture
async def admin(client):
    token = await login(client, "admin@example.com", "admin-pass-123")
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def heartbeat(**info) -> None:
    now = utcnow()
    async with SessionLocal() as s:
        s.add(JobState(name=WORKER, last_started_at=now - timedelta(days=3), info={"heartbeat": now.isoformat(), **info}))
        for name in ("products", "stock", "image_lists"):
            s.add(JobState(name=name, last_started_at=now, last_finished_at=now, last_success_at=now, info={}))
        await s.commit()


async def test_page_says_the_loading_stands_instead_of_an_estimate(admin):
    await admin.put("/api/admin/settings/images", json={**ImageSettings().model_dump(), "paused": False})
    stalled = (utcnow() - timedelta(days=3)).isoformat()
    await heartbeat(activity="photos", photo_at=stalled, problem={"kind": "pool_broken", "detail": "", "at": utcnow().isoformat()})
    d = (await admin.get("/api/admin/loading")).json()
    assert d["overview"]["stalled_since"] and d["overview"]["eta_seconds"] is None
    main = next(st for st in d["stages"] if st["key"] == "main")
    assert (main["status"], main["eta_seconds"], main["error"]) == ("error", None, "фото не обрабатываются 3 дн")
    check = next(c for c in d["checks"] if c["key"] == "photos")
    assert check["status"] == "error" and check["detail"].startswith("Фото не обрабатываются 3 дн. Процесс обработки фото упал")


async def test_page_tells_of_a_trouble_the_worker_got_over(admin):
    await admin.put("/api/admin/settings/images", json={**ImageSettings().model_dump(), "paused": False})
    await heartbeat(
        activity="photos",
        photo_at=utcnow().isoformat(),
        problem={"kind": "supplier", "detail": "image 1/0.jpg: ConnectTimeout", "at": utcnow().isoformat()},
    )
    d = (await admin.get("/api/admin/loading")).json()
    assert d["overview"]["stalled_since"] is None and d["overview"]["eta_seconds"] > 0
    check = next(c for c in d["checks"] if c["key"] == "photos")
    assert (check["status"], check["detail"]) == (
        "warn",
        "Только что: Поставщик не отдаёт фото (ConnectTimeout); воркер откладывает их и пробует другие",
    )


async def test_failed_photos_are_grouped_by_what_happened(admin):
    async with SessionLocal() as s:
        for iid, error in ((100, "crashed: image 1/0.jpg"), (200, "network: image 2/0.jpg: ReadTimeout"), (300, "hung: image 3/0.jpg")):
            img = await s.get(ProductImage, iid)
            img.status, img.error, img.attempts = ImageStatus.failed, error, MAX_ATTEMPTS
        await s.commit()
    reasons = {r["reason"] for r in (await admin.get("/api/admin/loading")).json()["failed"]["reasons"]}
    assert reasons == {
        "Обработка роняет процесс: файл слишком большой или битый",
        "Поставщик не отдаёт файл (ReadTimeout)",
        "Обработка зависает (дольше 5 мин)",
    }
