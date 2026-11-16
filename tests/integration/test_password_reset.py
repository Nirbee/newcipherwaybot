"""«Забыли пароль?» in the web cabinet + the admin «Письма» editor.

Reset links: one-time, only a hash in Redis, 1 h TTL, never for staff/blocked, and the
request endpoint answers the same for any address (no account enumeration)."""

# ruff: noqa: RUF001  — RU letter texts are asserted verbatim.

from __future__ import annotations

import time
from typing import Any, cast

import httpx
import pytest

from src.application.services.account_link import GUEST_FLAG
from src.core.enums import AuthType, Role, UserStatus
from src.core.security import hash_password, jwt_encode, verify_password
from src.infrastructure.database.models.user import User
from src.infrastructure.services import mailer as mailer_mod
from src.web.routes.cabinet_auth import _hash
from tests.integration.test_admin_api import ApiTestContainer, _login, client  # noqa: F401

F = "/api/cabinet/auth/password/forgot"
R = "/api/cabinet/auth/password/reset"
OLD_PASSWORD = "old-password-1"


class _ClockRedis:
    """Redis double with real TTLs on a controllable clock (expiry is what's under test)."""

    def __init__(self) -> None:
        self.now = time.time()
        self.store: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> str | None:
        item = self.store.get(key)
        if item is None:
            return None
        value, until = item
        if until is not None and until <= self.now:
            del self.store[key]
            return None
        return value

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: Any, nx: bool = False, ex: int | None = None) -> bool:
        if nx and self._live(key) is not None:
            return False
        self.store[key] = (str(value), self.now + ex if ex else None)
        return True

    async def getdel(self, key: str) -> str | None:
        value = self._live(key)
        self.store.pop(key, None)
        return value

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    async def incr(self, key: str) -> int:
        value = int(self._live(key) or 0) + 1
        until = self.store[key][1] if key in self.store else None
        self.store[key] = (str(value), until)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        if self._live(key) is None:
            return False
        self.store[key] = (self.store[key][0], self.now + seconds)
        return True

    async def ttl(self, key: str) -> int:
        return 60 if self._live(key) is not None else -2

    async def aclose(self) -> None: ...


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    async def fake_send(
        self: mailer_mod.Mailer, to: str, subject: str, body: str, *, html: str | None = None
    ) -> bool:
        sent.append({"to": to, "subject": subject, "body": body, "html": html or ""})
        return True

    monkeypatch.setattr(mailer_mod.Mailer, "send", fake_send)
    return sent


async def _setup(container: ApiTestContainer, *, smtp: bool = True) -> None:
    from src.infrastructure.database.models.user import User
    from tests.factories import make_user

    container.redis = _ClockRedis()  # type: ignore[assignment]
    async with container.uow() as uow:
        await container.bot_config.set_values(
            uow,
            {
                "WEB_CABINET_ENABLED": True,
                "CABINET_EMAIL_VERIFICATION": True,
                "CABINET_URL": "https://cabinet.example.ru/web/",
                **({"SMTP_HOST": "smtp.example.com", "SMTP_FROM": "x@example.com"} if smtp else {}),
            },
        )

        def email_user(email: str, **kw: Any) -> User:
            return User(
                auth_type=AuthType.EMAIL,
                email=email,
                password_hash=hash_password(OLD_PASSWORD),
                referral_code=f"r{abs(hash(email)) % 10**8}",
                **kw,
            )

        await uow.users.add(
            email_user(
                "guest@example.com",
                email_verified=False,
                notification_settings={GUEST_FLAG: True},
            )
        )
        await uow.users.add(email_user("boss@example.com", email_verified=True, role=Role.ADMIN))
        await uow.users.add(
            email_user("banned@example.com", email_verified=True, status=UserStatus.BLOCKED)
        )
        await make_user(uow, telegram_id=9009)
        await uow.commit()


def _token(mail: dict[str, str]) -> str:
    return mail["body"].split("#reset=", 1)[1].split()[0]


async def test_forgot_answers_the_same_for_any_address(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container)
    answers = []
    for email in (
        "guest@example.com",
        "nobody@example.com",
        "boss@example.com",
        "banned@example.com",
    ):
        res = await http.post(F, json={"email": email})
        answers.append((res.status_code, res.json()))
    assert answers == [(200, {"ok": True})] * 4  # nothing tells the addresses apart
    # only the ordinary account got a letter — never staff, never blocked
    assert [m["to"] for m in outbox] == ["guest@example.com"]
    mail = outbox[0]
    assert mail["subject"] == "Восстановление пароля"
    assert "https://cabinet.example.ru/web/#reset=" in mail["body"]
    assert "Задать новый пароль" in mail["html"]
    # Redis keeps only the hash of the link, never the token itself
    token = _token(mail)
    flat = str(container.redis.store)  # type: ignore[attr-defined]
    assert token not in flat and _hash(token) in flat


async def test_forgot_address_caps_answer_ok_and_send_nothing(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    """3 letters an hour, 10 a day per address. Over the cap the answer is still the same
    «ok» (a 429 would differ by address) — the letter is just not sent."""
    http, container = client
    await _setup(container)
    for hour in range(4):
        for _ in range(3):
            res = await http.post(F, json={"email": "guest@example.com"})
            assert (res.status_code, res.json()) == (200, {"ok": True})
        if hour == 0:  # the 4th within the hour: same answer, no letter
            assert (await http.post(F, json={"email": "guest@example.com"})).json() == {"ok": True}
            assert len(outbox) == 3
        container.redis.now += 3601  # type: ignore[attr-defined]
    # hours 1-3 sent 3 each until the daily cap of 10 counted requests (the 4th of hour 0 too)
    assert len(outbox) == 9
    # the per-IP limit still answers 429
    for _ in range(10):
        await http.post(F, json={"email": "nobody@example.com"})
    assert (await http.post(F, json={"email": "nobody@example.com"})).status_code == 429


async def test_forgot_without_mail_settings_says_so(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container, smtp=False)
    for email in ("guest@example.com", "nobody@example.com"):
        res = await http.post(F, json={"email": email})
        assert res.status_code == 503 and "почта не настроена" in res.json()["detail"]
    assert outbox == []


async def test_reset_is_single_use_and_ends_other_sessions(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container)
    async with container.uow() as uow:
        guest = await uow.users.get_by_email("guest@example.com")
        assert guest is not None
        guest.email_verified = True  # so the old password can open a session first
        await uow.commit()
    old = (
        await http.post(
            "/api/cabinet/auth/login", json={"email": "guest@example.com", "password": OLD_PASSWORD}
        )
    ).json()
    assert old["refresh_token"]
    # a one-click link minted earlier (guest purchase / payment letter), valid until the reset
    auto = jwt_encode(
        {"sub": guest.id, "type": "auto_login", "web": True, "iat": int(time.time()) - 10},
        container.settings.app.jwt_secret,
        72 * 3600,
    )

    await http.post(F, json={"email": "guest@example.com"})
    token = _token(outbox[-1])
    assert (await http.post(R, json={"token": token, "password": "short"})).status_code == 422
    res = await http.post(R, json={"token": token, "password": "brand-new-pass"})
    assert res.status_code == 200, res.text
    assert res.json()["access_token"] and res.json()["refresh_token"]

    # one use only
    again = await http.post(R, json={"token": token, "password": "another-pass-2"})
    assert again.status_code == 400
    # the old session is gone, the new password works, the old one doesn't
    gone = await http.post(
        "/api/cabinet/auth/refresh", json={"refresh_token": old["refresh_token"]}
    )
    assert gone.status_code == 401
    # and so is every auto-login link issued before the reset
    retired = await http.post("/api/cabinet/auth/login/auto", json={"token": auto})
    assert retired.status_code == 401
    async with container.uow() as uow:
        user = await uow.users.get_by_email("guest@example.com")
        assert user is not None
        assert verify_password("brand-new-pass", user.password_hash or "")
        assert not verify_password(OLD_PASSWORD, user.password_hash or "")
        assert user.email_verified is True
        assert GUEST_FLAG not in (user.notification_settings or {})  # no more one-click links


async def test_reset_link_expires_after_an_hour(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container)
    await http.post(F, json={"email": "guest@example.com"})
    token = _token(outbox[-1])
    container.redis.now += 3601  # type: ignore[attr-defined]
    res = await http.post(R, json={"token": token, "password": "brand-new-pass"})
    assert res.status_code == 400
    async with container.uow() as uow:
        user = await uow.users.get_by_email("guest@example.com")
        assert user is not None and verify_password(OLD_PASSWORD, user.password_hash or "")


async def test_new_request_and_password_change_kill_older_links(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container)
    await http.post(F, json={"email": "guest@example.com"})
    first = _token(outbox[-1])
    await http.post(F, json={"email": "guest@example.com"})
    second = _token(outbox[-1])
    assert (
        await http.post(R, json={"token": first, "password": "brand-new-pass"})
    ).status_code == 400

    async with container.uow() as uow:  # the password changed by another route meanwhile
        user = await uow.users.get_by_email("guest@example.com")
        assert user is not None
        user.password_hash = hash_password("changed-elsewhere")
        await uow.commit()
    assert (
        await http.post(R, json={"token": second, "password": "brand-new-pass"})
    ).status_code == 400


async def test_reset_never_unlocks_staff_even_with_a_valid_link(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    """Defence in depth: even a link minted for a since-promoted account is refused."""
    import json

    from src.web.routes.cabinet_auth import _RESET_TOKEN_PREFIX, _password_fingerprint

    http, container = client
    await _setup(container)
    async with container.uow() as uow:
        boss = await uow.users.get_by_email("boss@example.com")
        assert boss is not None
        payload = json.dumps({"uid": boss.id, "ph": _password_fingerprint(boss)})
    token = "t" * 40
    await container.redis.set(f"{_RESET_TOKEN_PREFIX}{_hash(token)}", payload, ex=3600)
    res = await http.post(R, json={"token": token, "password": "brand-new-pass"})
    assert res.status_code == 403
    async with container.uow() as uow:
        boss = await uow.users.get_by_email("boss@example.com")
        assert boss is not None and verify_password(OLD_PASSWORD, boss.password_hash or "")


async def test_admin_email_letters_edit_toggle_and_reset(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
) -> None:
    from src.web.routes.admin.notifications import EMAIL_EVENTS, email_template

    http, container = client
    auth = await _login(http)
    items = (await http.get("/api/admin/notifications/email", headers=auth)).json()["items"]
    assert [i["event"] for i in items] == [e for e, *_ in EMAIL_EVENTS]
    assert all(i["enabled"] and i["is_default"] for i in items)

    res = await http.patch(
        "/api/admin/notifications/email/expiring",
        headers=auth,
        json={"subject": "Скоро\nконец: {time}", "text": "Осталось {time}. {renew_url}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["subject"] == "Скоро конец: {time}" and res.json()["is_default"] is False
    async with container.uow() as uow:
        rendered = await email_template(uow, "expiring", time="2 часа", renew_url="https://x.ru/")
    assert rendered == ("Скоро конец: 2 часа", "Осталось 2 часа. https://x.ru/")

    # the Telegram list is untouched by letter rows
    tg = (await http.get("/api/admin/notifications", headers=auth)).json()["items"]
    assert not any(i["event"].startswith("email") for i in tg)

    off = await http.patch(
        "/api/admin/notifications/email/expiring", headers=auth, json={"enabled": False}
    )
    assert off.json()["enabled"] is False
    reset = (await http.post("/api/admin/notifications/email/expiring/reset", headers=auth)).json()
    assert reset["is_default"] is True and reset["enabled"] is False  # switch kept as is
    assert (
        await http.patch("/api/admin/notifications/email/nope", headers=auth, json={"text": "x"})
    ).status_code == 404


# --- security review fixes ---------------------------------------------------------------


async def _guest_purchase(
    http: httpx.AsyncClient, container: ApiTestContainer, monkeypatch: pytest.MonkeyPatch
) -> str:
    """Someone buys «as a guest» on an address that isn't theirs; returns their token."""
    from src.web.routes import cabinet as cabinet_routes
    from tests.factories import make_plan

    async def fake_pay(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"redirect_url": "https://pay.example.com/x"}

    monkeypatch.setattr(cabinet_routes, "_pay_with_gateway", fake_pay)
    async with container.uow() as uow:
        plan, _ = await make_plan(uow, code="guest-plan")
        await uow.commit()
    res = await http.post(
        "/api/cabinet/auth/guest/purchase",
        json={"email": "victim@example.com", "plan_id": plan.id, "days": 30, "method": "yookassa"},
    )
    assert res.status_code == 200, res.text
    return str(res.json()["auto_login_token"])


async def test_guest_preclaim_chain_is_cut_by_the_mailbox_owner(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guest purchase on someone else's e-mail -> the buyer can't bind the account to their
    Telegram / OAuth; once the real owner resets the password every session of the buyer
    (access token, refresh token, auto-login token) is dead."""
    import types

    from src.core import security

    http, container = client
    await _setup(container)
    async with container.uow() as uow:
        await container.bot_config.set_values(
            uow,
            {
                "BOT_USERNAME": "cipherway_bot",
                "OAUTH_GOOGLE_CLIENT_ID": "cid",
                "OAUTH_GOOGLE_CLIENT_SECRET": "sec",
            },
        )
        await uow.commit()
    real_time = time.time
    # the buyer's tokens are minted a little before the owner's reset
    monkeypatch.setattr(security, "time", types.SimpleNamespace(time=lambda: real_time() - 10))
    auto = await _guest_purchase(http, container, monkeypatch)
    session = (await http.post("/api/cabinet/auth/login/auto", json={"token": auto})).json()
    bearer = {"Authorization": f"Bearer {session['access_token']}"}
    monkeypatch.setattr(security, "time", time)

    res = await http.post("/api/cabinet/link/telegram", headers=bearer)
    assert res.status_code == 403 and "Сначала задайте пароль" in res.json()["detail"]
    res = await http.get("/api/cabinet/auth/oauth/google/authorize?link=1", headers=bearer)
    assert res.status_code == 403 and "Сначала задайте пароль" in res.json()["detail"]
    assert (await http.get("/api/cabinet/linked", headers=bearer)).status_code == 200

    # the owner of the mailbox takes the account back
    await http.post(F, json={"email": "victim@example.com"})
    token = _token(outbox[-1])
    owner = await http.post(R, json={"token": token, "password": "owners-password"})
    assert owner.status_code == 200
    owner_bearer = {"Authorization": f"Bearer {owner.json()['access_token']}"}

    assert (await http.get("/api/cabinet/linked", headers=bearer)).status_code == 401
    refreshed = await http.post(
        "/api/cabinet/auth/refresh", json={"refresh_token": session["refresh_token"]}
    )
    assert refreshed.status_code == 401
    retired = await http.post("/api/cabinet/auth/login/auto", json={"token": auto})
    assert retired.status_code == 401
    # the owner's own session (issued by the reset, same second) works, and binding is allowed
    assert (await http.get("/api/cabinet/linked", headers=owner_bearer)).status_code == 200
    linked = await http.post("/api/cabinet/link/telegram", headers=owner_bearer)
    assert linked.status_code == 200
    code = linked.json()["url"].rsplit("link_", 1)[1]
    async with container.uow() as uow:
        victim = await uow.users.get_by_email("victim@example.com")
        assert victim is not None
    stored = str(await container.redis.get(f"link_tg:{code}"))
    assert stored.startswith(f"{victim.id}:")  # "<uid>:<issued>"


async def test_auto_login_only_for_unclaimed_guest_accounts(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    from src.web.routes.cabinet_auth import _auto_login_token

    http, container = client
    await _setup(container)
    async with container.uow() as uow:
        guest = await uow.users.get_by_email("guest@example.com")
        owner = await uow.users.add(
            User(
                auth_type=AuthType.EMAIL,
                email="owner@example.com",
                email_verified=True,
                password_hash=hash_password(OLD_PASSWORD),
                referral_code="rown12345",
            )
        )
        await uow.commit()
        assert guest is not None
    guest_auto = _auto_login_token(cast("Any", container), guest.id)
    owner_auto = _auto_login_token(cast("Any", container), owner.id)
    auto_url = "/api/cabinet/auth/login/auto"

    # read-only preview: masked e-mail, nothing spent
    res = await http.post(f"{auto_url}/preview", json={"token": guest_auto})
    assert res.json() == {"email": "g***@example.com"}
    assert (await http.post(auto_url, json={"token": guest_auto})).status_code == 200
    # an account with a password of its own: never a one-click login
    assert (await http.post(auto_url, json={"token": owner_auto})).status_code == 401
    assert (await http.post(f"{auto_url}/preview", json={"token": owner_auto})).status_code == 401


async def test_reset_preview_is_read_only_and_masks(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    http, container = client
    await _setup(container)
    await http.post(F, json={"email": "guest@example.com"})
    token = _token(outbox[-1])
    for _ in range(2):  # previewing twice spends nothing
        res = await http.post(f"{R}/preview", json={"token": token})
        assert res.json() == {"email": "g***@example.com"}
    res = await http.post(R, json={"token": token, "password": "brand-new-pass"})
    assert res.status_code == 200
    assert (await http.post(f"{R}/preview", json={"token": token})).status_code == 400


async def test_reset_drops_pending_link_email_and_confirm_keeps_proven_address(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
) -> None:
    import hashlib
    import json

    http, container = client
    await _setup(container)
    async with container.uow() as uow:
        guest = await uow.users.get_by_email("guest@example.com")
        assert guest is not None
    pending = {
        "email": "other@example.com",
        "password_hash": hash_password("x" * 10),
        "code_hash": hashlib.sha256(b"123456").hexdigest(),
        "attempts": 0,
    }
    await container.redis.set(f"link_email:{guest.id}", json.dumps(pending), ex=900)
    await http.post(F, json={"email": "guest@example.com"})
    res = await http.post(R, json={"token": _token(outbox[-1]), "password": "brand-new-pass"})
    assert res.status_code == 200
    assert await container.redis.get(f"link_email:{guest.id}") is None

    # a code requested earlier can't swap out an address that is confirmed by now
    await container.redis.set(f"link_email:{guest.id}", json.dumps(pending), ex=900)
    bearer = {"Authorization": f"Bearer {res.json()['access_token']}"}
    res = await http.post(
        "/api/cabinet/link/email/confirm", headers=bearer, json={"code": "123456"}
    )
    assert res.status_code == 409
    async with container.uow() as uow:
        fresh = await uow.users.get(guest.id)
        assert fresh is not None and fresh.email == "guest@example.com"


async def test_two_racing_reset_links_apply_once(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
    outbox: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other link committed between our read and our write: the compare-and-set on the
    old hash matches no row, so the second password is NOT applied."""
    from sqlalchemy.orm.attributes import set_committed_value

    from src.infrastructure.database.dao.base import BaseDAO

    http, container = client
    await _setup(container)
    await http.post(F, json={"email": "guest@example.com"})
    token = _token(outbox[-1])
    async with container.uow() as uow:
        guest = await uow.users.get_by_email("guest@example.com")
        assert guest is not None
        stale_hash = guest.password_hash
        guest.password_hash = hash_password("won-the-race")  # the other link, committed
        await uow.commit()

    real_get = BaseDAO.get

    async def stale_get(self: Any, id_: int) -> Any:  # our read saw the pre-race row
        obj = await real_get(self, id_)
        if isinstance(obj, User) and obj.email == "guest@example.com":
            set_committed_value(obj, "password_hash", stale_hash)
        return obj

    monkeypatch.setattr(BaseDAO, "get", stale_get)
    res = await http.post(R, json={"token": token, "password": "lost-the-race"})
    monkeypatch.setattr(BaseDAO, "get", real_get)
    assert res.status_code == 400
    async with container.uow() as uow:
        user = await uow.users.get_by_email("guest@example.com")
        assert user is not None and verify_password("won-the-race", user.password_hash or "")


async def test_oauth_claims_guest_account_and_link_guards(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
) -> None:
    import datetime as dt
    import types

    from fastapi import HTTPException

    from src.infrastructure.database.models.cabinet_token import CabinetRefreshToken
    from src.web.routes.cabinet_auth import _oauth_link, _oauth_login

    _http, container = client
    await _setup(container)
    c = cast("Any", container)
    async with container.uow() as uow:
        guest = await uow.users.get_by_email("guest@example.com")
        assert guest is not None
        uow.session.add(
            CabinetRefreshToken(
                user_id=guest.id,
                token_hash="h" * 64,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=7),
            )
        )
        await uow.commit()
    info = types.SimpleNamespace(
        provider_id="g-1", email="guest@example.com", email_verified=True, name="Гость"
    )

    # binding OAuth to an unclaimed guest account: refused
    with pytest.raises(HTTPException) as refused:
        await _oauth_link(c, "google", info, guest.id)
    assert refused.value.status_code == 403

    # signing in with OAuth on that address = the mailbox owner claims the account
    user = await _oauth_login(c, "google", info)
    assert user.id == guest.id
    async with container.uow() as uow:
        fresh = await uow.users.get(guest.id)
        assert fresh is not None
        settings_ = fresh.notification_settings or {}
        assert GUEST_FLAG not in settings_ and int(settings_["pw_changed_at"]) > 0
        assert fresh.email_verified is True
        row = await uow.cabinet_tokens.find_one(token_hash="h" * 64)
        assert row is not None and row.revoked_at is not None

    # an OAuth link flow started before the claim is void
    info2 = types.SimpleNamespace(provider_id="y-1", email=None, email_verified=False, name="")
    with pytest.raises(HTTPException) as stale:
        await _oauth_link(c, "yandex", info2, guest.id, issued=1)
    assert stale.value.status_code == 401
    ok = await _oauth_link(c, "yandex", info2, guest.id, issued=int(time.time()) + 1)
    assert ok["ok"] is True


async def test_bot_rejects_link_codes_older_than_the_reset(
    client: tuple[httpx.AsyncClient, ApiTestContainer],  # noqa: F811
) -> None:
    import types

    from src.application.services.account_link import pack_link_code, unpack_link_code
    from src.bot.handlers.start import acclink_confirm
    from tests.factories import make_user

    _http, container = client
    await _setup(container)
    assert unpack_link_code(b"42") == (42, 0)  # a pre-upgrade code
    assert unpack_link_code("42:1700000000") == (42, 1700000000)
    assert unpack_link_code("junk") is None

    async with container.uow() as uow:
        web = await uow.users.get_by_email("boss@example.com")
        assert web is not None
        web.role = Role.USER
        web.notification_settings = {"pw_changed_at": 2_000_000_000}
        tg = await make_user(uow, telegram_id=7007)
        await uow.commit()
    answers: list[str] = []

    async def answer(text: str = "", show_alert: bool = False) -> None:
        answers.append(text)

    for issued in (0, 1_999_999_999):  # legacy code, and one minted just before the reset
        await container.redis.set("link_tg:abc", pack_link_code(web.id, issued), ex=900)
        cb = types.SimpleNamespace(data="acclink:ok:abc", answer=answer, message=None)
        await acclink_confirm(cast("Any", cb), cast("Any", container), tg)
    assert answers == ["Ссылка устарела", "Ссылка устарела"]
    async with container.uow() as uow:
        assert await uow.users.get(web.id) is not None  # nothing merged
