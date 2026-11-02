"""Web dependencies: access the AppContainer stored on app.state."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from src.infrastructure.di import AppContainer


def get_container(request: Request) -> AppContainer:
    container: Any = request.app.state.container
    return container


def dl_secret(container: AppContainer) -> str:
    """Secret for signing /dl import links (see connection.sign_dl); no extra env var needed."""
    app = container.settings.app
    return app.jwt_secret or app.crypt_key


def public_base(container: AppContainer, request: Request) -> str:
    """Public origin (WEB__PUBLIC_URL), falling back to the URL this request came in on."""
    base = (container.settings.web.public_url or "").strip().rstrip("/")
    return base or str(request.base_url).rstrip("/")
