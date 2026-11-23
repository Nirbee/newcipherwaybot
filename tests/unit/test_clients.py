"""«Клиенты»: stale statuses, the clean-up of orphaned subscriptions, the panel snapshot and
the merged list (chips, search, service accounts)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import cast

import pytest

from src.application.dto.panel import PanelDevice, PanelUser
from src.application.services import clients
from src.application.services.bot_config import BotConfigService
from src.application.services.clients import (
    PanelSnapshotService,
    effective_status,
    expire_orphaned,
)
from src.application.services.panel_guard import DEFAULT_SERVICE_USERNAMES, parse_patterns
from src.core.enums import SubscriptionStatus, UserStatus
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.uow import UnitOfWork
from tests.factories import make_user
from tests.fakes import FakeRemnawaveClient

NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
PATTERNS = parse_patterns(DEFAULT_SERVICE_USERNAMES)


def _panel(username: str, **fields: object) -> PanelUser:
    base = PanelUser(
        uuid=uuid.uuid4(),
        short_id=uuid.uuid4().hex[:16],
        username=username,
        is_enabled=True,
        expire_at=NOW + dt.timedelta(days=30),
        traffic_limit_bytes=0,
        traffic_used_bytes=0,
        device_limit=None,
        subscription_url=None,
        status="ACTIVE",
    )
    return dataclasses.replace(base, **fields)  # type: ignore[arg-type]


# --- statuses -----------------------------------------------------------------------------


def test_effective_status_shows_a_lapsed_working_subscription_as_expired() -> None:
    def st(sub: SubscriptionStatus | None, expire: dt.datetime | None, **kw: object) -> str:
        return effective_status(
            user_status=kw.pop("user", UserStatus.ACTIVE),  # type: ignore[arg-type]
            sub_status=sub,
            expire_at=expire,
            grace_until=kw.pop("grace", None),  # type: ignore[arg-type]
            now=NOW,
            panel_status=kw.pop("panel", None),  # type: ignore[arg-type]
        )

    past, future = NOW - dt.timedelta(days=90), NOW + dt.timedelta(days=5)
    assert st(SubscriptionStatus.ACTIVE, past) == "expired"  # the stale ACTIVE of August
    assert st(SubscriptionStatus.TRIAL, past) == "expired"
    assert st(SubscriptionStatus.ACTIVE, future) == "active"
    assert st(SubscriptionStatus.TRIAL, future) == "trial"
    assert st(SubscriptionStatus.ACTIVE, None) == "active"  # no date = open-ended
    # grace keeps its own state even though the paid expiry is in the past
    assert st(SubscriptionStatus.LIMITED, past, grace=NOW + dt.timedelta(days=1)) == "grace"
    assert st(SubscriptionStatus.LIMITED, future) == "limited"
    assert st(SubscriptionStatus.ACTIVE, future, panel="LIMITED") == "limited"
    assert st(SubscriptionStatus.DISABLED, future) == "disabled"
    assert st(None, None) == "none"
    assert st(SubscriptionStatus.ACTIVE, future, user=UserStatus.BLOCKED) == "blocked"


# --- clean-up -------------------------------------------------------------------------------


async def _sub(uow: UnitOfWork, tg: int, **fields: object) -> Subscription:
    user = await make_user(uow, telegram_id=tg)
    sub = Subscription(user_id=user.id, short_id=uuid.uuid4().hex[:12], **fields)  # type: ignore[arg-type]
    await uow.subscriptions.add(sub)
    user.current_subscription_id = sub.id
    return sub


async def test_expire_orphaned_touches_only_lapsed_subs_without_a_panel_user(
    uow: UnitOfWork,
) -> None:
    alive = _panel("sub_alive")
    past = NOW - dt.timedelta(days=100)
    async with uow:
        gone = await _sub(uow, 1, status=SubscriptionStatus.ACTIVE, expire_at=past, remnawave_id=7)
        gone_trial = await _sub(uow, 2, status=SubscriptionStatus.TRIAL, expire_at=past)
        in_panel = await _sub(
            uow, 3, status=SubscriptionStatus.ACTIVE, expire_at=past, remnawave_uuid=alive.uuid
        )
        grace = await _sub(
            uow,
            4,
            status=SubscriptionStatus.LIMITED,
            expire_at=past,
            grace_until=NOW + dt.timedelta(days=1),
        )
        running = await _sub(
            uow, 5, status=SubscriptionStatus.ACTIVE, expire_at=NOW + dt.timedelta(days=3)
        )
        fake = FakeRemnawaveClient()
        # an empty panel answer is a hiccup — nothing changes
        assert (await expire_orphaned(uow, [], NOW, client=fake)).changed == []
        assert gone.status is SubscriptionStatus.ACTIVE

        report = await expire_orphaned(uow, [alive], NOW, client=fake)
        assert sorted(report.changed) == sorted([gone.id, gone_trial.id])
        assert gone.status is SubscriptionStatus.EXPIRED
        assert gone_trial.status is SubscriptionStatus.EXPIRED
        assert in_panel.status is SubscriptionStatus.ACTIVE  # the panel will say itself
        assert grace.status is SubscriptionStatus.LIMITED  # the grace sweep owns it
        assert running.status is SubscriptionStatus.ACTIVE


async def test_expire_orphaned_never_writes_to_the_panel(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    other = _panel("sub_other")
    fake.users[other.uuid] = other
    async with uow:
        await _sub(uow, 9, status=SubscriptionStatus.ACTIVE, expire_at=NOW - dt.timedelta(days=30))
        report = await expire_orphaned(uow, await fake.list_users(), NOW, client=fake)
        assert len(report.changed) == 1
    assert fake.users == {other.uuid: other} and fake.deleted == []


async def test_expire_orphaned_asks_about_each_candidate(uow: UnitOfWork) -> None:
    """The full list missed someone? Each candidate is looked up once more: a panel error
    skips it, only an explicit «not found» expires it."""
    other = _panel("sub_other")
    hidden = _panel("sub_hidden")
    fake = FakeRemnawaveClient()
    fake.users[hidden.uuid] = hidden  # the panel has it, the list didn't show it
    past = NOW - dt.timedelta(days=30)
    async with uow:
        found = await _sub(
            uow, 21, status=SubscriptionStatus.ACTIVE, expire_at=past, remnawave_uuid=hidden.uuid
        )
        lost = await _sub(
            uow, 22, status=SubscriptionStatus.ACTIVE, expire_at=past, remnawave_uuid=uuid.uuid4()
        )
        fake.lookup_error = RuntimeError("panel down")
        report = await expire_orphaned(uow, [other], NOW, client=fake)
        assert report.changed == [] and report.skipped == 2
        fake.lookup_error = None
        report = await expire_orphaned(uow, [other], NOW, client=fake)
        assert report.changed == [lost.id] and report.skipped == 1
        assert found.status is SubscriptionStatus.ACTIVE
        assert lost.status is SubscriptionStatus.EXPIRED


async def test_expire_orphaned_stops_on_too_many_candidates(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    past = NOW - dt.timedelta(days=30)
    async with uow:
        subs = [
            await _sub(uow, 100 + i, status=SubscriptionStatus.ACTIVE, expire_at=past)
            for i in range(4)
        ]
        report = await expire_orphaned(uow, [_panel("sub_other")], NOW, client=fake, limit=3)
        assert report.over_limit and report.candidates == 4 and report.changed == []
        assert all(s.status is SubscriptionStatus.ACTIVE for s in subs)


# --- snapshot -------------------------------------------------------------------------------


class _Counting(FakeRemnawaveClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.down = False

    async def list_users(self) -> list[PanelUser]:
        self.calls += 1
        if self.down:
            raise RuntimeError("panel down")
        return await super().list_users()


async def test_snapshot_is_cached_and_survives_a_panel_outage() -> None:
    fake = _Counting()
    user = _panel("sub_a")
    fake.users[user.uuid] = user
    fake.devices[user.uuid] = [PanelDevice(hwid="1"), PanelDevice(hwid="2")]
    service = PanelSnapshotService(fake, ttl=60)

    first = await service.get()
    await service.get()
    assert fake.calls == 1  # one panel round-trip per minute, however many page loads
    assert first.error is None and first.device_count(user) == 2

    fake.down = True
    service.invalidate()
    down = await service.get()
    assert fake.calls == 2
    assert down.error == "error"  # a code for the page, never the exception text
    assert down.users == [user]  # the last good copy, flagged as stale
    assert down.fetched_at == first.fetched_at


async def test_snapshot_without_any_panel_data() -> None:
    fake = _Counting()
    fake.down = True
    snap = await PanelSnapshotService(fake).get()
    assert snap.error and snap.users == [] and snap.fetched_at is None


# --- the merged list ------------------------------------------------------------------------


async def test_rows_chips_search_and_service_accounts(uow: UnitOfWork) -> None:
    online = _panel(
        "sub_on",
        online_at=NOW - dt.timedelta(seconds=30),
        first_connected_at=NOW - dt.timedelta(days=5),
        device_limit=1,
        tag="VIP",
    )
    never = _panel("user_never", expire_at=NOW + dt.timedelta(days=2))
    relay = _panel("cascade_relay_b74178")
    fake = FakeRemnawaveClient()
    for p in (online, never, relay):
        fake.users[p.uuid] = p
    fake.devices[online.uuid] = [PanelDevice(hwid="a"), PanelDevice(hwid="b")]
    snap = await PanelSnapshotService(fake).get()

    async with uow:
        a = await _sub(
            uow,
            5072755730,
            status=SubscriptionStatus.ACTIVE,
            expire_at=NOW + dt.timedelta(days=30),
            remnawave_uuid=online.uuid,
        )
        await _sub(
            uow,
            2,
            status=SubscriptionStatus.ACTIVE,
            expire_at=NOW + dt.timedelta(days=2),
            remnawave_uuid=never.uuid,
        )
        await _sub(
            uow,
            3,
            status=SubscriptionStatus.ACTIVE,
            expire_at=NOW + dt.timedelta(days=3650),
            remnawave_uuid=relay.uuid,
        )
        await _sub(uow, 4, status=SubscriptionStatus.ACTIVE, expire_at=NOW - dt.timedelta(days=3))
        await make_user(uow, telegram_id=5)  # started the bot, never bought
        await uow.commit()
        data = await clients.load_rows(uow, snap, service_patterns=PATTERNS, now=NOW)

    rows = data.rows
    counts = clients.counters(rows, NOW)
    # «all» = panel users (the relay is not a customer); the one whose panel user is gone is
    # «former», the one who never bought — «no_sub»
    assert counts["all"] == 2
    assert counts["service"] == 1
    assert counts["active"] == 2
    assert counts["online"] == 1
    assert counts["never"] == 1
    assert counts["exp3"] == 1 and counts["exp7"] == 1
    assert counts["expired"] == 0 and counts["expired_week"] == 0
    assert counts["former"] == 1
    assert counts["no_sub"] == 1
    assert counts["devices_over"] == 1  # 2 devices on a limit of 1

    # the panel's names are reported back for the bot to remember
    assert data.panel_names[a.id] == "sub_on"

    def pick(chip: str = "all", q: str = "") -> list[int]:
        out = clients.select_rows(rows, chip=chip, q=q, sort="id", desc=False, now=NOW)
        return [r["telegram_id"] for r in out]

    assert pick("service") == [3]
    assert 3 not in pick()
    assert pick(q="5072") == [5072755730]  # the start of a Telegram ID
    assert pick(q="sub_on") == [5072755730]  # the panel name
    assert pick(q="vip") == [5072755730]  # the panel tag
    assert pick(q=f"id{a.user_id}") == [5072755730]
    online_row = next(r for r in rows if r["telegram_id"] == 5072755730)
    assert online_row["devices"] == 2 and online_row["tag"] == "VIP"
    assert online_row["status"] == "active"
    # keys / demo: no search by e-mail or link code (it would leak them guess by guess)
    sid = online_row["short_id"]
    assert pick(q=sid) == [5072755730]
    out = clients.select_rows(rows, chip="all", q=sid, sort="id", desc=False, now=NOW, private=True)
    assert out == []


def test_csv_neutralises_formulas_but_keeps_numbers() -> None:
    text = clients.to_csv(
        [
            {
                "id": 7,
                "name": '=HYPERLINK("http://evil","x")',
                "username": "+79991234567",
                "email": "@mail",
                "tag": "-1",
                "panel_username": "\tcmd",
                "traffic_used_bytes": 1024,
                "squads": [],
            }
        ]
    )
    row = text.splitlines()[1]
    assert row.startswith("7,")  # a number stays a number
    assert "'=HYPERLINK" in row and "'+7999" in row and "'@mail" in row and "'-1" in row
    assert "'\tcmd" in row
    assert ",1024," in row


def test_csv_has_a_header_and_a_bom() -> None:
    text = clients.to_csv(
        [
            {
                "id": 1,
                "name": "Нина",
                "node": {"name": "Франкфурт"},
                "squads": ["DefaultSUB"],
                "expire_at": None,
            }
        ]
    )
    assert text.startswith("\ufeffID в боте,Имя")
    assert "Нина" in text and "Франкфурт" in text and "DefaultSUB" in text


async def test_hourly_task_expires_orphans_and_tolerates_a_dead_panel(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.infrastructure.taskiq import tasks

    fake = _Counting()
    async with uow:
        sub = await _sub(
            uow, 77, status=SubscriptionStatus.TRIAL, expire_at=NOW - dt.timedelta(days=60)
        )
        await uow.commit()
        sub_id = sub.id
    other = _panel("sub_other")
    fake.users[other.uuid] = other

    class _Container:
        remnawave_client = fake
        bot_config = BotConfigService()

        def uow(self) -> UnitOfWork:
            return uow

    monkeypatch.setattr(tasks, "get_container", lambda: cast("object", _Container()))
    fake.down = True
    assert await tasks.expire_orphaned_subscriptions() == 0
    fake.down = False
    assert await tasks.expire_orphaned_subscriptions() == 1
    async with uow:
        refreshed = await uow.subscriptions.get(sub_id)
        assert refreshed is not None and refreshed.status is SubscriptionStatus.EXPIRED


async def test_hourly_task_alerts_instead_of_mass_expiring(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.infrastructure.services import reports
    from src.infrastructure.taskiq import tasks

    fake = _Counting()
    other = _panel("sub_other")
    fake.users[other.uuid] = other
    async with uow:
        for i in range(clients.ORPHAN_LIMIT + 1):
            await _sub(
                uow, 300 + i, status=SubscriptionStatus.ACTIVE, expire_at=NOW - dt.timedelta(days=9)
            )
        await uow.commit()
    alerts: list[tuple[str, str]] = []

    async def report(container: object, code: str, text: str, **_: object) -> None:
        alerts.append((code, text))

    class _Container:
        remnawave_client = fake
        bot_config = BotConfigService()

        def uow(self) -> UnitOfWork:
            return uow

    monkeypatch.setattr(reports, "send_topic_report", report)
    monkeypatch.setattr(tasks, "get_container", lambda: cast("object", _Container()))
    assert await tasks.expire_orphaned_subscriptions() == 0
    assert len(alerts) == 1 and alerts[0][0] == "alerts"
    assert "ничего не изменено" in alerts[0][1]
    async with uow:
        subs = await uow.subscriptions.list()
        assert all(s.status is SubscriptionStatus.ACTIVE for s in subs)


# --- counted the way the panel (MultiScript) counts --------------------------------------------


async def test_tiles_and_all_count_panel_users_like_multiscript(uow: UnitOfWork) -> None:
    """60 panel users, 2 of them MultiScript's: 58 customers, «active» = panel ACTIVE."""
    fake = FakeRemnawaveClient()
    users = [_panel("cascade_relay_b74178"), _panel("ms-status-monitor")]
    users += [_panel(f"sub_a{i}") for i in range(51)]  # ACTIVE
    users += [_panel(f"sub_e{i}", status="EXPIRED", is_enabled=False) for i in range(5)]
    users += [_panel(f"sub_d{i}", status="DISABLED", is_enabled=False) for i in range(2)]
    for u in users:
        fake.users[u.uuid] = u
    snap = await PanelSnapshotService(fake).get()
    tiles = clients.panel_tiles(snap, PATTERNS, NOW)
    assert tiles is not None
    assert tiles["total"] == 58 and tiles["service"] == 2 and tiles["active"] == 51

    async with uow:
        for i, u in enumerate(users):
            # the stored status lags behind (still ACTIVE for the expired ones)
            await _sub(
                uow,
                1000 + i,
                status=SubscriptionStatus.ACTIVE,
                expire_at=NOW + dt.timedelta(days=10),
                remnawave_uuid=u.uuid,
                panel_username=u.username,
            )
        await _sub(
            uow, 2000, status=SubscriptionStatus.EXPIRED, expire_at=NOW - dt.timedelta(days=60)
        )  # its panel user is gone
        await make_user(uow, telegram_id=3000)  # never bought
        await uow.commit()
        data = await clients.load_rows(uow, snap, service_patterns=PATTERNS, now=NOW)
    counts = clients.counters(data.rows, NOW)
    assert counts["all"] == 58 and counts["service"] == 2
    assert counts["active"] == 51  # by the panel's ACTIVE, not the stale stored status
    assert counts["expired"] == 5 and counts["disabled"] == 2
    assert counts["former"] == 1 and counts["no_sub"] == 1
    expired_row = next(r for r in data.rows if r["panel_username"] == "sub_e0")
    assert expired_row["status"] == "expired"  # shown as the panel has it


async def test_row_shows_the_panel_expiry_and_notes_the_bots(uow: UnitOfWork) -> None:
    panel_end = NOW + dt.timedelta(days=370)
    groketi = _panel("user_groketi", expire_at=panel_end)
    fake = FakeRemnawaveClient()
    fake.users[groketi.uuid] = groketi
    snap = await PanelSnapshotService(fake).get()
    async with uow:
        await _sub(
            uow,
            7,
            status=SubscriptionStatus.EXPIRED,
            expire_at=NOW - dt.timedelta(days=45),
            remnawave_uuid=groketi.uuid,
        )
        await uow.commit()
        data = await clients.load_rows(uow, snap, service_patterns=PATTERNS, now=NOW)
    row = data.rows[0]
    assert row["status"] == "active"  # the panel's word
    assert row["expire_at"] == panel_end.isoformat()
    assert row["bot_expire_at"] is not None  # «в боте: …»


# --- pull: expiries extended in the panel directly ------------------------------------------


async def test_pull_takes_over_a_later_panel_expiry(uow: UnitOfWork) -> None:
    later = NOW + dt.timedelta(days=370)
    groketi = _panel("user_groketi", expire_at=later)
    vip = _panel("VIP-777", expire_at=NOW + dt.timedelta(days=14))
    same = _panel("sub_same", expire_at=NOW + dt.timedelta(days=5, minutes=30))
    grace_user = _panel("sub_grace", expire_at=NOW + dt.timedelta(days=1))
    off = _panel("sub_off", expire_at=later, status="DISABLED", is_enabled=False)
    relay = _panel("cascade_relay_b74178", expire_at=later)
    async with uow:
        g = await _sub(
            uow,
            7,
            status=SubscriptionStatus.EXPIRED,
            expire_at=NOW - dt.timedelta(days=45),
            remnawave_uuid=groketi.uuid,
        )
        v = await _sub(
            uow,
            95,
            status=SubscriptionStatus.ACTIVE,
            expire_at=NOW - dt.timedelta(days=16),
            remnawave_uuid=vip.uuid,
        )
        s = await _sub(
            uow,
            96,
            status=SubscriptionStatus.ACTIVE,
            expire_at=NOW + dt.timedelta(days=5),
            remnawave_uuid=same.uuid,
        )
        gr = await _sub(
            uow,
            33,
            status=SubscriptionStatus.LIMITED,
            expire_at=NOW - dt.timedelta(days=4),
            grace_until=NOW + dt.timedelta(days=1),
            remnawave_uuid=grace_user.uuid,
        )
        o = await _sub(
            uow,
            97,
            status=SubscriptionStatus.EXPIRED,
            expire_at=NOW - dt.timedelta(days=5),
            remnawave_uuid=off.uuid,
        )
        r = await _sub(
            uow,
            94,
            status=SubscriptionStatus.EXPIRED,
            expire_at=NOW - dt.timedelta(days=5),
            remnawave_uuid=relay.uuid,
        )
        report = await clients.pull_from_panel(
            uow, [groketi, vip, same, grace_user, off, relay], NOW, service_patterns=PATTERNS
        )
        assert sorted(report.changed) == sorted([g.id, v.id])
        assert g.expire_at == later and g.status is SubscriptionStatus.ACTIVE
        assert v.status is SubscriptionStatus.ACTIVE
        assert s.expire_at == NOW + dt.timedelta(days=5)  # within the hour of drift
        assert gr.status is SubscriptionStatus.LIMITED  # grace is left alone
        assert o.status is SubscriptionStatus.EXPIRED  # the panel says DISABLED
        assert r.status is SubscriptionStatus.EXPIRED  # a service account
        trail = [a for a in await uow.audit.list() if a.action == "subscription.panel_pull"]
        assert {a.entity for a in trail} == {f"sub:{g.id}", f"sub:{v.id}"}


async def test_pull_stops_on_too_many_changes(uow: UnitOfWork) -> None:
    panels = [_panel(f"sub_p{i}", expire_at=NOW + dt.timedelta(days=90)) for i in range(4)]
    async with uow:
        subs = [
            await _sub(
                uow,
                500 + i,
                status=SubscriptionStatus.EXPIRED,
                expire_at=NOW - dt.timedelta(days=3),
                remnawave_uuid=p.uuid,
            )
            for i, p in enumerate(panels)
        ]
        report = await clients.pull_from_panel(uow, panels, NOW, service_patterns=PATTERNS, limit=3)
        assert report.over_limit and report.candidates == 4 and report.changed == []
        assert all(sub.status is SubscriptionStatus.EXPIRED for sub in subs)
