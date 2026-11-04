"""Web seam settings (FastAPI: webhooks + health; cabinet API later)."""

from __future__ import annotations

import ipaddress
from typing import Annotated

from pydantic import BaseModel, field_validator
from pydantic_settings import NoDecode

# Where our own reverse proxy reaches the web container from: loopback, the docker networks
# (RFC 1918) and their IPv6 counterparts. Explicit on purpose — ``ip_address.is_private`` also
# covers the documentation / reserved ranges, which are not "our proxy".
DEFAULT_TRUSTED_PROXIES = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
]


class WebSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    # Public https base URL the whole thing is reachable at (e.g. https://vpn.example.com).
    # On first boot this auto-wires the bot <-> mini-app link: SUBSCRIPTION_MINI_APP_URL=<url>/app
    # and CABINET_URL=<url>, so the bot shows the mini-app button without any manual config.
    public_url: str = ""
    # Explicit CORS origins for the future cabinet. NEVER "*" with cookie auth (gotcha #20).
    cors_origins: list[str] = []
    # Peers (CIDR) whose X-Forwarded-For / X-Real-IP name the real client — our Caddy (or the
    # host's nginx) in front of the container. Anyone else's proxy headers are ignored, so a
    # client can't dodge rate limits by sending its own. Comma-separated in the env.
    # NoDecode: take the env value as a plain comma list (not JSON) — split by the validator.
    trusted_proxies: Annotated[list[str], NoDecode] = DEFAULT_TRUSTED_PROXIES

    @field_validator("public_url", mode="before")
    @classmethod
    def _strip_url(cls, v: object) -> object:
        return v.strip().rstrip("/") if isinstance(v, str) else v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, v: object) -> object:
        if isinstance(v, str):
            v = [n.strip() for n in v.split(",") if n.strip()]
            if not v:  # an empty WEB__TRUSTED_PROXIES= line means "the default", not "nobody"
                return list(DEFAULT_TRUSTED_PROXIES)
        return v

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_cidrs(cls, v: list[str]) -> list[str]:
        for net in v:
            ipaddress.ip_network(net, strict=False)  # fail fast on a typo at startup
        return v
