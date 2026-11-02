"""Connect-screen apps from the Remnawave subscription page (+ INCY from CONNECTION_APPS).

The fixture is the live panel config (Remnawave 3.2.1, 2026-11-02) trimmed to four platforms.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from src.application.services import subpage_apps
from src.application.services.connection import sign_dl, verify_dl
from src.application.services.subpage_apps import (
    CACHE_KEY,
    build_platforms,
    load_templates,
    parse_subpage_config,
    platform_apps,
    sign_import_links,
)
from src.core.exceptions import RemnawaveTransientError
from tests.fakes import FakeRemnawaveClient

_URL = "https://sub.example/u/AbCdEf123"
_USER = "sub_abc123"
_CONFIG: dict[str, Any] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "subpage_config.json").read_text(encoding="utf-8")
)["response"]["config"]


class _Cache:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        self.ttl[key] = ex
        return True


class _BrokenCache:
    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis down")

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        raise ConnectionError("redis down")


def _build(config: Any = None, **kw: Any) -> dict[str, list[dict[str, Any]]]:
    templates = parse_subpage_config(_CONFIG if config is None else config)
    assert templates is not None
    out = build_platforms(
        templates,
        subscription_url=_URL,
        crypto_link=kw.pop("crypto_link", None),
        username=_USER,
        enabled=kw.pop("enabled", ["happ", "incy"]),
        **kw,
    )
    return {p["key"]: p["apps"] for p in out}


def _app(apps: list[dict[str, Any]], label: str) -> dict[str, Any]:
    return next(a for a in apps if a["label"] == label)


def _buttons(app: dict[str, Any], kind: str) -> list[dict[str, str]]:
    return [x for b in app["blocks"] for x in b["buttons"] if x["kind"] == kind]


def test_parses_live_config_platforms_and_order() -> None:
    templates = parse_subpage_config(_CONFIG)
    assert templates is not None
    # our device order first, the panel's extra platforms (TVs) after
    assert [p["key"] for p in templates] == ["ios", "android", "windows", "appleTV"]
    assert [p["name"] for p in templates] == ["iOS", "Android", "Windows", "Apple TV"]
    names = {p["key"]: [a["name"] for a in p["apps"]] for p in templates}
    assert names["ios"] == ["Happ", "Clash Mi", "v2RayTun", "Shadowrocket"]
    assert names["windows"] == ["FlClashX", "Koala Clash", "Prizrak-Box", "Happ", "Throne"]
    # templates stay user-agnostic (cacheable for everyone)
    assert "{{SUBSCRIPTION_LINK}}" in json.dumps(templates)


def test_panel_order_kept_and_incy_appended_where_catalogue_has_a_link() -> None:
    plats = _build()
    labels = {k: [a["label"] for a in apps] for k, apps in plats.items()}
    assert labels["ios"] == ["Happ", "Clash Mi", "v2RayTun", "Shadowrocket", "INCY"]
    assert labels["android"] == ["Happ", "FlClashX", "v2RayTun", "INCY"]
    assert labels["windows"] == ["FlClashX", "Koala Clash", "Prizrak-Box", "Happ", "Throne", "INCY"]
    # no INCY download for TVs in the bot's catalogue -> nothing appended
    assert labels["appleTV"] == ["Happ"]
    # Happ is in CONNECTION_APPS but the panel already lists it -> not duplicated
    assert labels["ios"].count("Happ") == 1
    incy = _app(plats["ios"], "INCY")
    assert incy["source"] == "bot" and incy["deep_link"] == f"incy://add/{_URL}"
    assert _buttons(incy, "link")[0]["url"].endswith("id6756943388")
    assert [b["stage"] for b in incy["blocks"]] == ["install", "subscribe", "connect"]


def test_subscription_link_and_username_filled_in() -> None:
    plats = _build()
    ios = plats["ios"]
    assert _app(ios, "Happ")["deep_link"] == f"happ://add/{_URL}"
    assert _app(ios, "Clash Mi")["deep_link"] == (
        f"clashmi://install-config?xhwid=true&overwrite=no&name={_USER}&url={_URL}"
    )
    assert _app(ios, "Shadowrocket")["deep_link"] == f"shadowrocket://add/{_URL}#{_USER}"
    assert _app(plats["windows"], "Koala Clash")["deep_link"] == (
        f"koala-clash://install-config?url={_URL}"
    )
    # Throne has no deep link — the panel offers «copy the link» instead
    throne = _app(plats["windows"], "Throne")
    assert throne["deep_link"] is None
    assert _buttons(throne, "copy") == [{"kind": "copy", "text": "Скопировать ссылку", "url": _URL}]
    assert "{{" not in json.dumps(plats)
    # every live panel app with an import button keeps it (its scheme is allow-listed for /dl)
    no_link = {a["label"] for apps in plats.values() for a in apps if a["deep_link"] is None}
    assert no_link == {"Throne"}


def test_store_buttons_and_russian_texts() -> None:
    happ = _app(_build()["ios"], "Happ")
    links = _buttons(happ, "link")
    assert [x["text"] for x in links] == ["App Store (RU)", "App Store (Global)"]
    assert all(x["url"].startswith("https://apps.apple.com/") for x in links)
    assert happ["blocks"][0]["title"] == "Установка приложения"
    assert happ["blocks"][1]["buttons"][0]["text"] == "Добавить подписку"


def test_steps_mapped_onto_wizard_stages() -> None:
    plats = _build()
    stages = lambda a: [b["stage"] for b in a["blocks"]]  # noqa: E731
    assert stages(_app(plats["ios"], "Happ")) == ["install", "subscribe", "connect"]
    # HWID tip before adding; routing rules after it; «how to connect» last
    assert stages(_app(plats["ios"], "Shadowrocket")) == [
        "install",
        "install",
        "subscribe",
        "subscribe",
        "connect",
    ]
    assert stages(_app(plats["windows"], "Throne")) == [
        "install",
        "install",
        "subscribe",
        "subscribe",
        "connect",
    ]


def test_hide_link_drops_copy_buttons_only() -> None:
    plats = _build(hide_link=True)
    throne = _app(plats["windows"], "Throne")
    assert _buttons(throne, "copy") == []
    assert _buttons(throne, "link")  # downloads still there
    assert _app(plats["windows"], "Happ")["deep_link"] == f"happ://add/{_URL}"
    # the step the panel made «add the subscription» stays one even without its button
    assert [b["stage"] for b in throne["blocks"]][2] == "subscribe"
    assert "_sub" not in json.dumps(plats)


def test_unsafe_or_unresolvable_links_are_dropped() -> None:
    config = {
        "platforms": {
            "ios": {
                "apps": [
                    {
                        "name": "Evil",
                        "blocks": [
                            {
                                "title": {"ru": "Шаг"},
                                "buttons": [
                                    {
                                        "type": "external",
                                        "link": "javascript:alert(1)",
                                        "text": "x",
                                    },
                                    {"type": "external", "link": "tg://resolve", "text": "x"},
                                    {
                                        "type": "subscriptionLink",
                                        "link": "ms-msdt:{{SUBSCRIPTION_LINK}}",
                                    },
                                    {
                                        "type": "subscriptionLink",
                                        "link": "happ://crypt4/{{HAPP_CRYPT4_LINK}}",
                                    },
                                    {"type": "subscriptionLink", "link": "happ://x/{{UNKNOWN}}"},
                                    # plain http and an upper-cased scheme: refused (as in /dl)
                                    {"type": "external", "link": "http://plain.example"},
                                    {"type": "subscriptionLink", "link": "http://x/{{USERNAME}}"},
                                    {
                                        "type": "subscriptionLink",
                                        "link": "HAPP://add/{{SUBSCRIPTION_LINK}}",
                                    },
                                    {"type": "weird", "link": "https://ok.example"},
                                    {
                                        "type": "external",
                                        "link": "https://ok.example",
                                        "text": "ok",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    }
    evil = _build(config, enabled=["happ"])["ios"][0]
    assert [x["url"] for b in evil["blocks"] for x in b["buttons"]] == ["https://ok.example"]
    assert evil["deep_link"] is None
    # the crypto placeholder resolves when the subscription has a happ crypto link
    with_crypto = _build(config, enabled=["happ"], crypto_link="CRYPTO")["ios"][0]
    assert with_crypto["deep_link"] == "happ://crypt4/CRYPTO"


def test_sign_import_links_wraps_app_schemes_only() -> None:
    plats = build_platforms(
        parse_subpage_config(_CONFIG) or [],
        subscription_url=_URL,
        crypto_link=None,
        username=_USER,
        enabled=["happ", "incy"],
    )
    sign_import_links(plats, "s3cret", "https://vpn.example")
    apps = {p["key"]: p["apps"] for p in plats}
    happ = _app(apps["ios"], "Happ")
    to = f"happ://add/{_URL}"
    signed = "https://vpn.example/dl?" + urlencode({"to": to, "sig": sign_dl(to, "s3cret")})
    assert happ["deep_link"] == signed
    assert _buttons(happ, "import")[0]["url"] == signed
    assert _app(apps["ios"], "INCY")["deep_link"].startswith("https://vpn.example/dl?to=incy")
    # store links and «copy the link» stay untouched
    assert _buttons(happ, "link")[0]["url"].startswith("https://apps.apple.com/")
    assert _buttons(_app(apps["windows"], "Throne"), "copy")[0]["url"] == _URL
    assert verify_dl(to, sign_dl(to, "s3cret"), "s3cret")
    assert not verify_dl(to, sign_dl(to, "other"), "s3cret")
    assert not verify_dl(to, "", "s3cret")


def test_name_match_ignores_case_spaces_and_dashes() -> None:
    config = copy.deepcopy(_CONFIG)
    config["platforms"]["ios"]["apps"].append(
        {"name": " in-cy ", "blocks": [{"title": "Установка", "buttons": []}]}
    )
    labels = [a["label"] for a in _build(config)["ios"]]
    assert labels.count("INCY") == 0 and "in-cy" in labels


def test_english_fallback_and_duplicate_names_get_unique_keys() -> None:
    config = {
        "platforms": {
            "android": {
                "displayName": {"en": "Android"},
                "apps": [
                    {"name": "Happ", "blocks": [{"title": {"en": "Install"}, "buttons": []}]},
                    {"name": "happ", "blocks": [{"description": "Второй", "buttons": []}]},
                ],
            }
        }
    }
    apps = _build(config, enabled=["happ"])["android"]
    assert [a["key"] for a in apps] == ["happ", "happ-2"]
    assert apps[0]["blocks"][0]["title"] == "Install"


@pytest.mark.parametrize(
    "config",
    [
        None,
        "garbage",
        {},
        {"platforms": []},
        {"platforms": {"ios": {"apps": "nope"}}},
        {"platforms": {"ios": {"apps": {"a": 1}}}},
        {"platforms": {"ios": {"apps": [{"name": "X", "blocks": {"b": 1}}]}}},
        {"platforms": {"ios": {"apps": [{"name": "", "blocks": []}, {"name": "X", "blocks": []}]}}},
    ],
)
def test_unusable_config_is_none(config: Any) -> None:
    assert parse_subpage_config(config) is None


async def test_load_caches_parsed_config_for_an_hour() -> None:
    client = FakeRemnawaveClient()
    client.subpage_config = _CONFIG
    cache = _Cache()
    first = await load_templates(client, cache)
    second = await load_templates(client, cache)
    assert first == second and first is not None
    assert client.subpage_config_fetches == 1
    assert cache.ttl[CACHE_KEY] == 3600


async def test_panel_down_falls_back_and_is_not_retried_every_render() -> None:
    client = FakeRemnawaveClient()
    client.subpage_config = RemnawaveTransientError("panel 502")
    cache = _Cache()
    kw: dict[str, Any] = {
        "subscription_url": _URL,
        "crypto_link": None,
        "username": _USER,
        "enabled": ["happ", "incy"],
    }
    assert await platform_apps(client, cache, **kw) is None
    assert await platform_apps(client, cache, **kw) is None
    assert client.subpage_config_fetches == 1  # miss remembered (5 min)
    assert cache.ttl[CACHE_KEY] == 300


async def test_broken_config_falls_back() -> None:
    client = FakeRemnawaveClient()
    client.subpage_config = {"platforms": "broken"}
    out = await platform_apps(
        client, None, subscription_url=_URL, crypto_link=None, username=_USER, enabled=["happ"]
    )
    assert out is None


async def test_slow_panel_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Slow(FakeRemnawaveClient):
        async def get_subscription_page_config(self) -> dict[str, Any] | None:
            await asyncio.sleep(5)
            return _CONFIG

    monkeypatch.setattr(subpage_apps, "PANEL_TIMEOUT_SECONDS", 0.01)
    assert await load_templates(_Slow(), None) is None


async def test_cache_outage_still_serves_from_panel() -> None:
    client = FakeRemnawaveClient()
    client.subpage_config = _CONFIG
    out = await platform_apps(
        client,
        _BrokenCache(),
        subscription_url=_URL,
        crypto_link=None,
        username=_USER,
        enabled=["happ", "incy"],
    )
    assert out is not None and out[0]["key"] == "ios"


async def test_garbage_in_cache_refetches() -> None:
    client = FakeRemnawaveClient()
    client.subpage_config = _CONFIG
    cache = _Cache()
    cache.store[CACHE_KEY] = "{not json"
    assert await load_templates(client, cache) is not None
    assert client.subpage_config_fetches == 1
