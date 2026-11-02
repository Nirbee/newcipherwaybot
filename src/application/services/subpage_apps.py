"""Connect-screen apps from the Remnawave subscription page — one source of truth.

The panel's subscription page lists apps PER PLATFORM, each with ordered steps (install /
add the subscription / connect) and buttons: store links (``external``), one-tap import
deep links (``subscriptionLink``, with ``{{SUBSCRIPTION_LINK}}`` & co. to fill in) and
copy-the-link (``copyButton``). The mini-app and the bot show that same list, so what the
owner edits in the panel is what every surface offers.

Owner-enabled bot clients (``CONNECTION_APPS``) the panel doesn't list are appended after the
panel's apps on platforms where the bot's catalogue has a download link for them (INCY).

Never breaks the Connect screen: panel down, no config, an unexpected shape -> ``None`` and
callers keep the old ``CONNECTION_APPS`` behaviour. The parsed config (no user data) is cached
in Redis for an hour; a miss is remembered for a few minutes so a dead panel isn't hit (and
waited on) by every screen render.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from src.application.common.cache import KeyValueCache
from src.application.common.panel import RemnawaveClient
from src.application.services.connection import (
    APP_LINK_SCHEMES,
    CLIENT_LABELS,
    CLIENT_STORES,
    build_deep_links,
    dl_url,
)
from src.core.logging import get_logger

log = get_logger(__name__)

CACHE_KEY = "subpage:apps:v1"
CACHE_TTL_SECONDS = 3600
MISS_TTL_SECONDS = 300
PANEL_TIMEOUT_SECONDS = 4.0
_MISS = "-"

# Our platform order (the mini-app's device chips); other panel platforms (TVs) follow in
# the panel's own order.
PLATFORM_ORDER: tuple[str, ...] = ("ios", "android", "windows", "macos", "linux")
PLATFORM_NAMES: dict[str, str] = {
    "ios": "iOS",
    "android": "Android",
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "appleTV": "Apple TV",
    "androidTV": "Android TV",
}

_BUTTON_TYPES = ("external", "subscriptionLink", "copyButton")
_PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_MAX_NAME = 60
_MAX_TITLE = 120
_MAX_TEXT = 1500
_MAX_BUTTON_TEXT = 80
_MAX_LINK = 2048
_MAX_APPS = 20
_MAX_BLOCKS = 15
_MAX_BUTTONS = 10


def _norm(name: str) -> str:
    """Name for matching panel apps against bot clients: case/space/punctuation-insensitive."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _text(value: Any, limit: int) -> str:
    """A localized panel string ({"ru": …, "en": …} or plain) -> Russian, falling back to en."""
    if isinstance(value, str):
        out = value
    elif isinstance(value, dict):
        out = next(
            (
                v
                for v in (value.get("ru"), value.get("en"), *value.values())
                if isinstance(v, str) and v.strip()
            ),
            "",
        )
    else:
        out = ""
    return out.strip()[:limit]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_https(link: str) -> bool:
    """Panel buttons open https pages only (no plain http). Case-sensitive, like /dl."""
    return link.startswith("https://")


def _parse_button(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict) or raw.get("type") not in _BUTTON_TYPES:
        return None
    link = raw.get("link")
    if not isinstance(link, str) or not link.strip() or len(link) > _MAX_LINK:
        return None
    link = link.strip()
    if raw["type"] == "external" and not _is_https(link):
        return None  # store/download/instruction pages only — never a script or app scheme
    return {
        "type": str(raw["type"]),
        "text": _text(raw.get("text"), _MAX_BUTTON_TEXT),
        "link": link,
    }


def _parse_app(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    blocks: list[dict[str, Any]] = []
    for b in _list(raw.get("blocks")):
        if not isinstance(b, dict):
            continue
        buttons = [
            btn
            for btn in (_parse_button(x) for x in _list(b.get("buttons"))[:_MAX_BUTTONS])
            if btn is not None
        ]
        title = _text(b.get("title"), _MAX_TITLE)
        text = _text(b.get("description"), _MAX_TEXT)
        if title or text or buttons:
            blocks.append({"title": title, "text": text, "buttons": buttons})
        if len(blocks) >= _MAX_BLOCKS:
            break
    if not blocks:
        return None
    return {
        "name": name.strip()[:_MAX_NAME],
        "featured": bool(raw.get("featured")),
        "blocks": blocks,
    }


def parse_subpage_config(config: Any) -> list[dict[str, Any]] | None:
    """Panel subscription-page config -> [{key, name, apps: [app template]}] or None.

    Templates keep the raw ``{{…}}`` links (no user data), so the result is cacheable for
    everyone. Anything malformed is skipped; nothing usable at all -> None (fallback).
    """
    if not isinstance(config, dict) or not isinstance(config.get("platforms"), dict):
        return None
    platforms: dict[str, Any] = config["platforms"]
    keys = [k for k in PLATFORM_ORDER if k in platforms]
    keys += [k for k in platforms if k not in PLATFORM_ORDER]
    out: list[dict[str, Any]] = []
    for key in keys:
        raw = platforms.get(key)
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        apps = [
            a for a in (_parse_app(x) for x in _list(raw.get("apps"))[:_MAX_APPS]) if a is not None
        ]
        if not apps:
            continue
        name = _text(raw.get("displayName"), _MAX_NAME) or PLATFORM_NAMES.get(key, key)
        out.append({"key": key, "name": name, "apps": apps})
    return out or None


def _fill(template: str, values: dict[str, str]) -> str | None:
    """Substitute ``{{NAME}}`` placeholders; None if any stays unresolved (button dropped)."""
    out = template
    for name, value in values.items():
        out = out.replace("{{" + name + "}}", value)
    return None if _PLACEHOLDER.search(out) else out


def _resolve_button(
    btn: dict[str, str], values: dict[str, str], hide_link: bool
) -> dict[str, str] | None:
    text = btn.get("text") or ""
    if btn["type"] == "external":
        return {"kind": "link", "text": text or "Открыть", "url": btn["link"]}
    filled = _fill(btn["link"], values)
    if filled is None:
        return None
    if btn["type"] == "copyButton":
        if hide_link:
            return None  # owner hides the raw link (HIDE_SUBSCRIPTION_LINK)
        return {"kind": "copy", "text": text or "Скопировать ссылку", "url": filled}
    # subscriptionLink: launched through /dl, so only https or a known app scheme
    if not (_is_https(filled) or filled.startswith(APP_LINK_SCHEMES)):
        return None
    return {"kind": "import", "text": text or "Добавить подписку", "url": filled}


def _with_stages(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map the panel's step list onto the wizard: install -> subscribe -> connect.

    Everything before the first block with an import/copy button installs the app; from it
    on — adding the subscription (fallback tips, routing rules); a trailing block without
    buttons after it is «how to connect» (the wizard's final screen). ``_sub`` marks a block
    the PANEL made the add-subscription step even if its button was dropped (hidden link).
    """
    anchors = [
        bool(b.pop("_sub", False)) or any(x["kind"] in ("import", "copy") for x in b["buttons"])
        for b in blocks
    ]
    first = next((i for i, a in enumerate(anchors) if a), None)
    last = len(blocks) - 1
    connect = last if last > 0 and not blocks[last]["buttons"] and (first or 0) < last else None
    for i, b in enumerate(blocks):
        if i == connect:
            b["stage"] = "connect"
        elif first is None or i < first:
            b["stage"] = "install"
        else:
            b["stage"] = "subscribe"
    return blocks


def _store_button_text(url: str) -> str:
    if "apps.apple.com" in url:
        return "App Store"
    if "play.google.com" in url:
        return "Google Play"
    return "Скачать"


def _bot_app(key: str, store: str, deep_link: str) -> dict[str, Any]:
    """A CONNECTION_APPS client the panel doesn't list, in the panel-app shape."""
    label = CLIENT_LABELS[key]
    blocks: list[dict[str, Any]] = [
        {
            "title": "Установка приложения",
            "text": f"Установите приложение {label} и вернитесь к этому экрану.",
            "buttons": [{"kind": "link", "text": _store_button_text(store), "url": store}],
        },
        {
            "title": "Добавление подписки",
            "text": "Нажмите кнопку ниже — приложение откроется, и подписка добавится "
            "автоматически.",
            "buttons": [{"kind": "import", "text": "Добавить подписку", "url": deep_link}],
        },
        {
            "title": "Подключение и использование",
            "text": "Откройте приложение и нажмите кнопку подключения.",
            "buttons": [],
        },
    ]
    return {
        "key": key,
        "label": label,
        "featured": False,
        "source": "bot",
        "deep_link": deep_link,
        "blocks": _with_stages(blocks),
    }


def build_platforms(
    templates: list[dict[str, Any]],
    *,
    subscription_url: str,
    crypto_link: str | None,
    username: str,
    enabled: list[str],
    hide_link: bool = False,
) -> list[dict[str, Any]]:
    """Per-user platform lists: panel apps (links filled in, panel order) + missing bot apps."""
    values = {"SUBSCRIPTION_LINK": subscription_url, "USERNAME": username}
    if crypto_link:
        values["HAPP_CRYPT3_LINK"] = crypto_link
        values["HAPP_CRYPT4_LINK"] = crypto_link
    bot_links = build_deep_links(subscription_url, crypto_link)
    out: list[dict[str, Any]] = []
    for plat in templates:
        apps: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tpl in plat["apps"]:
            blocks = []
            for b in tpl["blocks"]:
                buttons = [
                    r for r in (_resolve_button(x, values, hide_link) for x in b["buttons"]) if r
                ]
                if b["title"] or b["text"] or buttons:
                    sub = any(x["type"] != "external" for x in b["buttons"])
                    blocks.append(
                        {"title": b["title"], "text": b["text"], "buttons": buttons, "_sub": sub}
                    )
            if not blocks:
                continue
            norm = _norm(tpl["name"]) or "app"
            key, n = norm, 2
            while key in seen:
                key, n = f"{norm}-{n}", n + 1
            seen.add(key)
            imports = [x["url"] for b in blocks for x in b["buttons"] if x["kind"] == "import"]
            apps.append(
                {
                    "key": key,
                    "label": tpl["name"],
                    "featured": bool(tpl.get("featured")),
                    "source": "panel",
                    "deep_link": imports[0] if imports else None,
                    "blocks": _with_stages(blocks),
                }
            )
        names = {_norm(t["name"]) for t in plat["apps"]}
        for k in enabled:
            store = CLIENT_STORES.get(k, {}).get(plat["key"])
            if not store or k not in bot_links or k in seen:
                continue
            if _norm(k) in names or _norm(CLIENT_LABELS[k]) in names:
                continue
            apps.append(_bot_app(k, store, bot_links[k]))
            seen.add(k)
        if apps:
            out.append({"key": plat["key"], "name": plat["name"], "apps": apps})
    return out


def sign_import_links(platforms: list[dict[str, Any]], secret: str, base: str) -> None:
    """Swap every app-scheme import link for its signed /dl URL (in place) — /dl opens only
    links the server issued."""
    for plat in platforms:
        for app in plat["apps"]:
            if app.get("deep_link"):
                app["deep_link"] = dl_url(app["deep_link"], secret, base)
            for block in app["blocks"]:
                for btn in block["buttons"]:
                    if btn["kind"] == "import":
                        btn["url"] = dl_url(btn["url"], secret, base)


async def load_templates(
    client: RemnawaveClient, cache: KeyValueCache | None
) -> list[dict[str, Any]] | None:
    """Parsed subscription-page config: Redis (1 h) first, then the panel; None on any failure."""
    cached: Any = None
    if cache is not None:
        try:
            cached = await cache.get(CACHE_KEY)
        except Exception:  # cache down must not block the screen
            cached = None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8", "replace")
    if cached == _MISS:
        return None
    if cached:
        try:
            data = json.loads(cached)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list) and data:
            return data
    templates: list[dict[str, Any]] | None
    try:
        raw = await asyncio.wait_for(
            client.get_subscription_page_config(), timeout=PANEL_TIMEOUT_SECONDS
        )
        templates = parse_subpage_config(raw)
        if templates is None:
            log.warning("subpage_config_unusable", has_config=raw is not None)
    except Exception as exc:
        log.warning("subpage_config_fetch_failed", error=str(exc)[:200])
        templates = None
    if cache is not None:
        try:
            if templates:
                payload = json.dumps(templates, ensure_ascii=False)
                await cache.set(CACHE_KEY, payload, ex=CACHE_TTL_SECONDS)
            else:
                await cache.set(CACHE_KEY, _MISS, ex=MISS_TTL_SECONDS)
        except Exception:  # best-effort: worst case is one extra panel call
            log.warning("subpage_cache_write_failed")
    return templates


async def platform_apps(
    client: RemnawaveClient,
    cache: KeyValueCache | None,
    *,
    subscription_url: str,
    crypto_link: str | None,
    username: str,
    enabled: list[str],
    hide_link: bool = False,
) -> list[dict[str, Any]] | None:
    """Connect-screen apps per platform from the panel, or None -> keep CONNECTION_APPS."""
    templates = await load_templates(client, cache)
    if not templates:
        return None
    try:
        platforms = build_platforms(
            templates,
            subscription_url=subscription_url,
            crypto_link=crypto_link,
            username=username,
            enabled=enabled,
            hide_link=hide_link,
        )
    except Exception as exc:  # a stale/odd cached shape must not take the screen down
        log.warning("subpage_apps_build_failed", error=str(exc)[:200])
        return None
    return platforms or None
