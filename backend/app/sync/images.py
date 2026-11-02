"""Product image lists (products_clients_images.read_new, <=100 skus per call, 2/s) and processing queue."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Collection
from concurrent.futures.process import BrokenProcessPool
from datetime import timedelta
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.images.gallery import copies, thumbnail
from app.images.pool import ImagePool
from app.images.processing import VERSION, ProcessedImage, delete_image_files, image_path, process_image
from app.models import Product, ProductImage
from app.models.base import utcnow
from app.models.catalog import VISIBLE_STATUSES, ImageStatus
from app.site_settings import ImageSettings
from app.supplier.client import SupplierClient, SupplierError, SupplierNotFound, SupplierUnavailable

log = logging.getLogger(__name__)
LIST_BATCH = 100
REFRESH_AFTER = timedelta(days=14)
MAX_ATTEMPTS = 3
# Kinds of ProductImage.error the worker writes itself ("network: image 1/2.jpg: ReadTimeout"), named
# on the admin page: the supplier did not send the file while it sent others; the processing of the
# photo killed its process (the kernel stops a process that takes too much memory) or hung.
NETWORK, CRASHED, HUNG = "network", "crashed", "hung"


async def _skus_needing_lists(limit: int) -> list[int]:
    stale = utcnow() - REFRESH_AFTER
    async with SessionLocal() as session:
        q = (
            select(Product.sku)
            .where(
                Product.is_active,
                Product.has_image,
                or_(Product.images_synced_at.is_(None), Product.in_stock & (Product.images_synced_at < stale)),
            )
            .order_by(Product.in_stock.desc(), Product.images_synced_at.is_not(None), Product.sku)
            .limit(limit)
        )
        return list((await session.scalars(q)).all())


async def apply_image_lists(skus: list[int], items: list[dict]) -> int:
    """Store the supplier image list of `skus`; returns the number of new/changed images."""
    media_root = get_settings().media_root
    now = utcnow()
    by_sku: dict[int, list[dict]] = defaultdict(list)
    for it in items:
        if not it.get("deleted") and it.get("url") and it.get("sku") in skus:
            by_sku[it["sku"]].append(it)
    changed = 0
    ids = [int(it["id"]) for imgs in by_sku.values() for it in imgs]
    async with SessionLocal() as session:
        existing = {
            img.id: img
            for img in (
                await session.scalars(select(ProductImage).where(or_(ProductImage.sku.in_(skus), ProductImage.id.in_(ids))))
            ).all()
        }
        keep: set[int] = set()
        main: dict[int, int | None] = {sku: None for sku in skus}
        for sku, imgs in by_sku.items():
            # The supplier order, the first photo is the main one. `is_main` is not used: it marks a photo
            # anywhere in the list (first in 42% of products, last in 33%, often the last of the old p_ set).
            for pos, it in enumerate(imgs):
                iid = int(it["id"])
                row = existing.get(iid)
                if iid in keep or (row is not None and row.sku != sku and row.sku not in main):
                    continue  # the same image record is already attached to another product
                keep.add(iid)
                if main[sku] is None and not (row is not None and row.status == ImageStatus.failed and row.source_url == it["url"]):
                    main[sku] = iid  # a photo that is broken at the supplier is not used as the main one
                if row is None:
                    session.add(ProductImage(id=iid, sku=sku, source_url=it["url"], position=pos, priority=it.get("priority") or 0))
                    changed += 1
                    continue
                row.sku, row.position, row.priority = sku, pos, it.get("priority") or 0
                if row.source_url != it["url"]:
                    row.source_url, row.version = it["url"], row.version + 1
                    row.status, row.attempts, row.error = ImageStatus.pending, 0, None
                    changed += 1
        gone = [iid for iid, row in existing.items() if row.sku in main and iid not in keep]
        await session.flush()
        for sku, iid in main.items():
            await session.execute(update(Product).where(Product.sku == sku).values(main_image_id=iid, images_synced_at=now))
        if gone:
            await session.execute(delete(ProductImage).where(ProductImage.id.in_(gone)))
        await session.commit()
    for iid in gone:
        delete_image_files(media_root, iid)
    return changed


async def sync_image_lists(client: SupplierClient, max_products: int = 20000) -> dict:
    skus = await _skus_needing_lists(max_products)
    changed = 0
    for i in range(0, len(skus), LIST_BATCH):
        batch = skus[i : i + LIST_BATCH]
        changed += await apply_image_lists(batch, await client.get_product_images(batch))
    log.info("image lists: %d products checked, %d images new/changed", len(skus), changed)
    return {"products": len(skus), "images_changed": changed}


# --- processing ---------------------------------------------------------------------------------


async def replace_failed_main(session, sku: int) -> None:
    """If the product's main photo failed, show its next photo instead."""
    product = await session.get(Product, sku)
    if product is None or product.main_image_id is None:
        return
    main = await session.get(ProductImage, product.main_image_id)
    if main is None or main.status != ImageStatus.failed:
        return
    product.main_image_id = await session.scalar(
        select(ProductImage.id)
        .where(ProductImage.sku == sku, ProductImage.status != ImageStatus.failed)
        .order_by(ProductImage.position)
        .limit(1)
    )


async def next_pending(
    limit: int, cfg: ImageSettings, *, include_out_of_stock: bool = False, exclude: Collection[int] = ()
) -> list[tuple[int, str]]:
    """What the worker processes next (but the photos in `exclude`), of in-stock products (then of
    out-of-stock ones if requested):
    1. photos never processed: all main photos first, then all second photos, the third ones... up to
       `cfg.per_product` (only the main ones unless `cfg.gallery`: the others then on the first view);
    2. photos queued for processing again (stale), in the same order;
    3. photos made by an older processing version, in the same order (if `cfg.reprocess`).
    Each step is its own query bounded by status and position, so it walks the queue index: instant
    while there is work, ~1 s over the whole catalog when there is none (the worker then looks less
    often). A main photo further than the limit (the first ones broken at the supplier) is not taken:
    it is processed when its product is first opened."""
    steps = [ProductImage.status == ImageStatus.pending, ProductImage.status == ImageStatus.stale]
    if cfg.reprocess:
        old = or_(ProductImage.algo_version.is_(None), ProductImage.algo_version < VERSION)
        steps.append(and_(ProductImage.status == ImageStatus.ready, old))
    shown = ProductImage.position < (cfg.per_product if cfg.gallery else 1)
    if exclude:
        shown = and_(shown, ProductImage.id.not_in(list(exclude)))
    rows: list = []
    async with SessionLocal() as session:
        for in_stock in (True, False) if include_out_of_stock else (True,):
            for step in steps:
                q = (
                    select(ProductImage.id, ProductImage.source_url)
                    .join(Product, Product.sku == ProductImage.sku)
                    .where(step, shown, Product.is_active, Product.in_stock.is_(in_stock), ProductImage.attempts < MAX_ATTEMPTS)
                    .order_by(ProductImage.position, ProductImage.id)
                    .limit(limit - len(rows))
                )
                rows += (await session.execute(q)).all()
                if len(rows) >= limit:
                    return [tuple(r) for r in rows]
    return [tuple(r) for r in rows]


async def pick_trial(n: int) -> list[tuple[int, str]]:
    """`n` random main photos of in-stock products for a trial batch: ones not processed yet while
    there are enough of them, then processed ones too (they are processed again)."""
    base = (
        select(ProductImage.id, ProductImage.source_url)
        .join(Product, Product.main_image_id == ProductImage.id)
        .where(Product.is_active, Product.in_stock)
        .order_by(func.random())
    )
    async with SessionLocal() as session:
        rows = (await session.execute(base.where(ProductImage.status == ImageStatus.pending).limit(n))).all()
        if len(rows) < n:
            more = base.where(ProductImage.status.in_(VISIBLE_STATUSES)).limit(n - len(rows))
            rows += (await session.execute(more)).all()
    return [tuple(r) for r in rows]


async def process_one(client: SupplierClient, pool: ImagePool | None, image_id: int, url: str) -> ProcessedImage | None:
    """Downloads, cleans and stores one photo; None if that failed: an attempt of the photo is used up
    (record_failure), whatever the cause, so a photo that cannot be processed does not stay first in the
    queue. Raises SupplierUnavailable on network trouble and BrokenProcessPool when the worker's processes
    broke: whether the photo is to blame is not known yet, the caller decides (app.worker.process_batch)."""
    s = get_settings()
    try:
        data = await client.fetch_image(url, size="original")
        if pool is None:
            res = await asyncio.to_thread(process_image, data, str(s.media_root), image_id)
        else:
            res = await pool.run(process_image, data, str(s.media_root), image_id)
    except (SupplierUnavailable, BrokenProcessPool):
        raise
    except Exception as e:  # noqa: BLE001 - recorded on the photo; a bug in the processing is logged in full
        if isinstance(e, (SupplierError, OSError, ValueError)):
            log.warning("image %s (%s) failed: %s", image_id, url, e)
        else:
            log.exception("image %s (%s): processing crashed", image_id, url)
        await record_failure(image_id, e)
        return None
    async with SessionLocal() as session:
        await session.execute(
            update(ProductImage)
            .where(ProductImage.id == image_id)
            .values(
                # re-processed files keep their paths: a new version changes the URL (?v=) past browser caches
                version=case(
                    (ProductImage.status.in_(VISIBLE_STATUSES), ProductImage.version + 1), else_=ProductImage.version
                ),
                status=ImageStatus.ready,
                width=res.width,
                height=res.height,
                wm_template=res.wm_template,
                wm_score=res.wm_score,
                wm_left=res.wm_left,
                algo_version=VERSION,
                attempts=0,
                error=None,
                processed_at=utcnow(),
            )
        )
        if (sku := await session.scalar(select(ProductImage.sku).where(ProductImage.id == image_id))) is not None:
            await refresh_copies(session, sku)
        await session.commit()
    return res


async def record_failure(image_id: int, error: BaseException | str) -> None:
    """Uses up an attempt of the photo (all of them when the supplier has no such file). After MAX_ATTEMPTS
    the photo leaves the queue: failed, or ready if it was processed before (its files stay on the site)."""
    if isinstance(error, (SupplierError, OSError, ValueError)):
        text = str(error)
    elif isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = error
    async with SessionLocal() as session:
        img = await session.get(ProductImage, image_id)
        if img is None:
            return
        img.attempts = MAX_ATTEMPTS if isinstance(error, SupplierNotFound) else img.attempts + 1
        img.error = text[:500]
        if img.attempts >= MAX_ATTEMPTS:
            if img.status in VISIBLE_STATUSES:
                img.status = ImageStatus.ready
            else:
                img.status = ImageStatus.failed
                await session.flush()
                await replace_failed_main(session, img.sku)
        await session.commit()


async def refresh_copies(session: AsyncSession, sku: int) -> int:
    """Links the copies among a product's processed photos to the one shown instead (see
    app/images/gallery.py). Returns how many photos are hidden as copies."""
    photos = (
        await session.scalars(
            select(ProductImage)
            .where(ProductImage.sku == sku, ProductImage.status.in_(VISIBLE_STATUSES), ProductImage.width.is_not(None))
            .order_by(ProductImage.position)
        )
    ).all()
    thumbs = await asyncio.to_thread(_thumbnails, get_settings().media_root, [p.id for p in photos])
    links = copies([(p, thumbs[p.id]) for p in photos if p.id in thumbs])
    for p in photos:
        p.duplicate_of = links.get(p.id)
    return sum(1 for kept in links.values() if kept is not None)


def _thumbnails(media_root: Path, ids: list[int]) -> dict[int, np.ndarray]:
    out = {}
    for image_id in ids:
        try:
            with Image.open(image_path(media_root, image_id, "md")) as im:
                out[image_id] = thumbnail(im)
        except OSError:  # the files are not written yet or were removed
            continue
    return out
