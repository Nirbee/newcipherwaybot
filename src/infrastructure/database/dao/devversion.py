"""DevVersion report DAO."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, RowMapping, Select, delete, func, select, text
from sqlalchemy.orm import defer

from src.infrastructure.database.dao.base import BaseDAO
from src.infrastructure.database.models.devversion import DevversionReport


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """Admin filters; ``asn=0`` means «provider unknown» (no ASN)."""

    asn: int | None = None
    app_version: str | None = None
    subscription_id: int | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None

    def apply(self, stmt: Select[Any]) -> Select[Any]:
        r = DevversionReport
        if self.asn == 0:
            stmt = stmt.where(r.asn.is_(None))
        elif self.asn is not None:
            stmt = stmt.where(r.asn == self.asn)
        if self.app_version:
            stmt = stmt.where(r.app_version == self.app_version)
        if self.subscription_id is not None:
            stmt = stmt.where(r.subscription_id == self.subscription_id)
        if self.since is not None:
            stmt = stmt.where(r.received_at >= self.since)
        if self.until is not None:
            stmt = stmt.where(r.received_at < self.until)
        return stmt


class DevversionReportDAO(BaseDAO[DevversionReport]):
    model = DevversionReport

    async def page(
        self, flt: ReportFilter, *, limit: int, offset: int
    ) -> tuple[Sequence[DevversionReport], int]:
        """Newest first, without the payload (the list never needs it)."""
        total = await self.session.scalar(flt.apply(select(func.count(DevversionReport.id))))
        stmt = (
            flt.apply(select(DevversionReport))
            .options(defer(DevversionReport.payload))
            .order_by(DevversionReport.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.scalars(stmt)).all(), int(total or 0)

    async def stream_digests(
        self, flt: ReportFilter, *, limit: int
    ) -> AsyncGenerator[tuple[int, int, int | None, str | None, dict[str, Any]]]:
        """(id, subscription_id, asn, as_org, digest) of the newest ``limit`` matching reports,
        through a server-side cursor in small batches — the caller stops at its ceiling."""
        r = DevversionReport
        stmt = flt.apply(select(r.id, r.subscription_id, r.asn, r.as_org, r.digest))
        stmt = stmt.order_by(r.id.desc()).limit(limit).execution_options(yield_per=100)
        result = await self.session.stream(stmt)
        async for i, s, a, o, d in result:
            yield int(i), int(s), a, o, d or {}

    async def subscription_ids(self, flt: ReportFilter) -> list[int]:
        stmt = flt.apply(select(DevversionReport.subscription_id).distinct())
        return [int(s) for s in (await self.session.scalars(stmt.limit(10_000))).all()]

    async def stream(self, flt: ReportFilter, *, limit: int) -> AsyncGenerator[RowMapping]:
        """Matching reports WITH payloads, newest first, read through a server-side cursor in
        small batches (plain rows, not ORM objects — nothing piles up in the session)."""
        r = DevversionReport
        stmt = flt.apply(select(*r.__table__.c)).order_by(r.id.desc()).limit(limit)
        result = await self.session.stream(stmt.execution_options(yield_per=50))
        async for row in result.mappings():
            yield row

    async def trim_subscription(self, subscription_id: int, keep: int) -> int:
        """Keep only the newest ``keep`` reports of a subscription."""
        r = DevversionReport
        edge = await self.session.scalar(
            select(r.id)
            .where(r.subscription_id == subscription_id)
            .order_by(r.id.desc())
            .offset(keep)
            .limit(1)
        )
        if edge is None:
            return 0
        result = await self.session.execute(
            delete(r).where(r.subscription_id == subscription_id, r.id <= edge)
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def table_bytes(self) -> int | None:
        """pg_total_relation_size of the table (None off Postgres, e.g. the sqlite tests)."""
        bind = self.session.get_bind()
        if bind.dialect.name != "postgresql":
            return None
        value = await self.session.scalar(
            text("SELECT pg_total_relation_size('devversion_reports')")
        )
        return int(value or 0)

    async def facets(self) -> dict[str, list[dict[str, Any]]]:
        """Providers and app versions present (for the admin filters), most reports first."""
        r = DevversionReport
        n = func.count(r.id)
        providers = await self.session.execute(
            select(r.asn, func.max(r.as_org), n).group_by(r.asn).order_by(n.desc()).limit(300)
        )
        versions = await self.session.execute(
            select(r.app_version, n).group_by(r.app_version).order_by(n.desc()).limit(100)
        )
        return {
            "providers": [
                {"asn": asn, "as_org": org, "reports": int(c)} for asn, org, c in providers.all()
            ],
            "app_versions": [{"app_version": v, "reports": int(c)} for v, c in versions.all()],
        }

    async def delete_for_subscription(self, subscription_id: int) -> int:
        return await self.delete_by(subscription_id=subscription_id)

    async def delete_older_than(self, cutoff: dt.datetime) -> int:
        result = await self.session.execute(
            delete(DevversionReport).where(DevversionReport.received_at < cutoff)
        )
        return cast("CursorResult[Any]", result).rowcount or 0
