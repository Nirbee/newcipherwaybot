"""DevVersion: diagnostics reports the Windows app uploads after the user opted in.

One row per upload (``POST /api/app/v1/telemetry``). The sections are stored as sent
(``payload``); ``digest`` is a small extract of the numbers the admin summaries need (built
at upload, see ``application/services/devversion.py``), so the summaries never load whole
payloads. Provider and region come from the request IP via an offline database — the IP itself
is never stored. Rows older than 90 days are deleted by the daily retention task; deleting a
subscription cascades its reports.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.infrastructure.database.base import AwareDateTime, Base, IntPk, JsonB


class DevversionReport(IntPk, Base):
    __tablename__ = "devversion_reports"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    install_id: Mapped[str] = mapped_column(String(36))
    received_at: Mapped[dt.datetime] = mapped_column(
        AwareDateTime, server_default=func.now(), nullable=False, index=True
    )
    app_version: Mapped[str] = mapped_column(String(32))
    consent_version: Mapped[str] = mapped_column(String(8))
    schema: Mapped[int] = mapped_column(Integer)
    # AS numbers are 32-bit unsigned — past a signed INTEGER.
    asn: Mapped[int | None] = mapped_column(BigInteger, index=True)
    as_org: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer, default=0)  # JSON bytes after gunzip
    payload: Mapped[dict[str, Any]] = mapped_column(JsonB)
    digest: Mapped[dict[str, Any]] = mapped_column(JsonB, default=dict)
