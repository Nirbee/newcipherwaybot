"""Read-only admin API keys for AI agents (Claude etc.) — one row per issued key.

Only the SHA-256 hash of the key is stored (the key itself is shown once, on creation).
A key authenticates GET/HEAD requests to ``/api/admin/*`` only — see
``src/web/routes/admin/api_key_auth.py``. Created and revoked by the owner only.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.infrastructure.database.base import AwareDateTime, Base, IntPk, JsonB, utcnow


class AdminApiKey(IntPk, Base):
    __tablename__ = "admin_api_keys"

    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))  # first chars of the key, for display only
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(AwareDateTime, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(AwareDateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(AwareDateTime)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(AwareDateTime)
    # Admin API path prefixes the key may read; None = every screen open to keys.
    allowed_screens: Mapped[list[str] | None] = mapped_column(JsonB)
