"""DevVersion wiring: effective settings from the hot-reload config, the 90-day retention and
the table-size alert."""

from __future__ import annotations

import datetime as dt
from contextlib import suppress
from typing import TYPE_CHECKING

from src.application.services.devversion import (
    RETENTION_DAYS,
    TABLE_ALERT_BYTES,
    DevVersionConfig,
    config_from,
)
from src.core.logging import get_logger

if TYPE_CHECKING:
    from src.infrastructure.di import AppContainer

log = get_logger(__name__)

_SIZE_ALERT_KEY = "devversion:size-alert"
_SIZE_ALERT_EVERY = 86400
# Hard stop: set (for a day) while the table is past TABLE_ALERT_BYTES; the daily retention run
# renews or clears it. Uploads answer 503 ``storage_full`` while it's there.
STORAGE_FULL_KEY = "devversion:storage-full"


async def load_config(container: AppContainer) -> DevVersionConfig:
    async with container.uow() as uow:
        snap = await container.bot_config.snapshot(uow)
    return config_from(snap)


async def purge_old(container: AppContainer, *, now: dt.datetime | None = None) -> int:
    """Delete reports older than RETENTION_DAYS (the consent text promises 90 days)."""
    cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(days=RETENTION_DAYS)
    async with container.uow() as uow:
        deleted = await uow.devversion_reports.delete_older_than(cutoff)
        await uow.commit()
    if deleted:
        log.info("devversion reports purged", count=deleted)
    return deleted


async def check_table_size(container: AppContainer) -> int | None:
    """Past TABLE_ALERT_BYTES (3 GB on disk, indexes and TOAST included) uploads stop (the
    «storage full» flag, renewed by this daily run) and the admins get an alert — at most once
    a day. Back under it — the flag is cleared. Returns the size (None where it can't be
    measured)."""
    async with container.uow() as uow:
        size = await uow.devversion_reports.table_bytes()
    if size is None:
        return None
    if size <= TABLE_ALERT_BYTES:
        with suppress(Exception):
            await container.redis.delete(STORAGE_FULL_KEY)
        return size
    with suppress(Exception):
        await container.redis.set(STORAGE_FULL_KEY, "1", ex=_SIZE_ALERT_EVERY)
    first = True
    with suppress(Exception):  # Redis down: the task itself runs once a day anyway
        first = bool(await container.redis.set(_SIZE_ALERT_KEY, "1", nx=True, ex=_SIZE_ALERT_EVERY))
    if first:
        log.warning("devversion table is large", bytes=size)
        with suppress(Exception):
            await container.notifier.notify_admins(
                f"⚠️ DevVersion: таблица отчётов занимает {size / 1024**3:.1f} ГБ "
                f"(порог {TABLE_ALERT_BYTES // 1024**3} ГБ) — приём отчётов остановлен до "
                "следующей ночной проверки. Проверьте, кто шлёт много данных "
                "(админка → DevVersion), или сузьте список разрешённых подписок.",
                topic="alerts",
            )
    return size
