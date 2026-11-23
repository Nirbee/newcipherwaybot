"""MultiScript service accounts: the bot never writes to them (panel_guard.GuardedPanelClient).

The cascade relay (``cascade_relay_b74178``) and the status monitor (``ms-status-monitor``) were
adopted into the bot by the 6 Aug panel import. Every panel write path — renew/set expiry,
limits, enable/disable, traffic and device resets, revoke, delete, panel_write_retry, the
device guard — must refuse them at the service level, not only in the admin UI.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any, cast

import pytest

from src.application.dto.panel import PanelDevice, PanelUser, PanelUserRef
from src.application.services.device_guard import DeviceGuardService, GuardConfig
from src.application.services.panel_guard import (
    DEFAULT_SERVICE_USERNAMES,
    HARD_SERVICE_USERNAMES,
    GuardedPanelClient,
    PanelServiceAccountError,
    is_service_username,
    parse_patterns,
    validate_patterns,
)
from src.application.services.panel_import import find_panel_only
from src.application.services.remnawave import RemnawaveService
from src.application.services.subscription import SubscriptionService
from src.core.enums import SubscriptionStatus
from src.core.exceptions import RemnawaveAuthError, RemnawaveTransientError
from src.infrastructure.database.models.subscription import Subscription
from src.infrastructure.database.uow import UnitOfWork
from tests.factories import make_user
from tests.fakes import FakeRemnawaveClient

PATTERNS = parse_patterns(DEFAULT_SERVICE_USERNAMES)


def _panel_user(username: str, *, enabled: bool = True) -> PanelUser:
    return PanelUser(
        uuid=uuid.uuid4(),
        short_id=uuid.uuid4().hex[:16],
        username=username,
        is_enabled=enabled,
        expire_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=3650),
        traffic_limit_bytes=0,
        traffic_used_bytes=0,
        device_limit=None,
        subscription_url=None,
    )


def _seed(fake: FakeRemnawaveClient, username: str) -> PanelUser:
    user = _panel_user(username)
    fake.users[user.uuid] = user
    fake.devices[user.uuid] = [PanelDevice(hwid="hw-1")]
    return user


def test_patterns() -> None:
    assert parse_patterns("abc, b_*;  CdE") == (*HARD_SERVICE_USERNAMES, "abc", "b_*", "cde")
    assert is_service_username("cascade_relay_b74178", PATTERNS)
    assert is_service_username("cascade_relay_ffff01", PATTERNS)  # the wildcard
    assert is_service_username("MS-status-monitor", PATTERNS)
    assert not is_service_username("ms-other", PATTERNS)  # exact name now, not «ms-*»
    assert not is_service_username("sub_abc123", PATTERNS)
    assert not is_service_username("user_5072755730", PATTERNS)
    assert not is_service_username(None, PATTERNS)


def test_clearing_the_setting_keeps_the_hard_minimum() -> None:
    for raw in ("", None, " , ; "):
        patterns = parse_patterns(raw)
        assert patterns == HARD_SERVICE_USERNAMES
        assert is_service_username("cascade_relay_b74178", patterns)
        assert is_service_username("ms-status-monitor", patterns)


@pytest.mark.parametrize(
    "raw",
    [
        "*",
        "?*",
        "s*",
        "ab*",
        "1abc",
        "_abc*",
        "sub_*",
        "sub_x",
        "user_*",
        "us*",
        "*relay*",
        "[a]bc",
    ],
)
def test_too_broad_patterns_are_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        validate_patterns(f"cascade_relay_b74178, {raw}")


def test_safe_patterns_are_accepted() -> None:
    validate_patterns(DEFAULT_SERVICE_USERNAMES)
    validate_patterns("ms-status-monitor, ms-backup-*, relay_node_?")
    validate_patterns("")


async def test_every_write_to_a_service_account_is_refused() -> None:
    fake = FakeRemnawaveClient()
    relay = _seed(fake, "cascade_relay_b74178")
    guard = GuardedPanelClient(fake)
    ref = PanelUserRef(uuid=relay.uuid, short_id=relay.short_id)
    spec = RemnawaveService(guard).build_spec(
        short_id="x",
        telegram_id=None,
        expire_at=dt.datetime.now(dt.UTC),
        traffic_limit_bytes=1,
        device_limit=1,
    )
    writes: list[Any] = [
        guard.update_user(ref, spec),
        guard.enable_user(ref),
        guard.disable_user(ref),
        guard.delete_user(ref),
        guard.reset_traffic(ref),
        guard.revoke_subscription(ref),
        guard.drop_connections(ref),
        guard.delete_device(ref, "hw-1"),
    ]
    for call in writes:
        with pytest.raises(PanelServiceAccountError):
            await call
    # untouched on the panel
    assert fake.users[relay.uuid] == relay
    assert fake.deleted == []
    assert [d.hwid for d in fake.devices[relay.uuid]] == ["hw-1"]
    # reads still work
    assert await guard.get_user(ref) == relay
    assert len(await guard.list_users()) == 1


async def test_ordinary_users_are_written_as_before() -> None:
    fake = FakeRemnawaveClient()
    person = _seed(fake, "user_5072755730")
    guard = GuardedPanelClient(fake)
    await guard.disable_user(person.uuid)  # a bare 2.x uuid ref is fine too
    assert fake.users[person.uuid].is_enabled is False
    await guard.delete_device(PanelUserRef(uuid=person.uuid), "hw-1")
    assert fake.devices[person.uuid] == []


async def test_subscription_service_cannot_renew_a_service_account(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    relay = _seed(fake, "cascade_relay_b74178")
    subs = SubscriptionService(RemnawaveService(GuardedPanelClient(fake)))
    async with uow:
        user = await make_user(uow, telegram_id=None, first_name="cascade_relay_b74178")
        sub = Subscription(
            user_id=user.id,
            remnawave_uuid=relay.uuid,
            short_id=relay.short_id,
            status=SubscriptionStatus.ACTIVE,
            expire_at=relay.expire_at,
        )
        await uow.subscriptions.add(sub)
        for write in (
            subs.renew(uow, sub, days=30),
            subs.push_limits(uow, sub),
            subs.set_expiry(uow, sub, expire_at=dt.datetime.now(dt.UTC)),
            subs.end_grace(uow, sub),
        ):
            with pytest.raises(PanelServiceAccountError):
                await write
    assert fake.users[relay.uuid] == relay


async def test_bot_side_record_protects_without_asking_the_panel() -> None:
    """A subscription the bot recorded under a service name is refused even when the panel
    can't confirm it (down, or the lookup fails) — keys come from the DB loader."""
    fake = FakeRemnawaveClient()  # the panel knows nothing
    ref = PanelUserRef(panel_id=98, short_id="abcd")

    async def loader() -> tuple[tuple[str, ...], frozenset[str]]:
        return PATTERNS, frozenset({"id:98"})

    guard = GuardedPanelClient(fake, loader)
    with pytest.raises(PanelServiceAccountError):
        await guard.disable_user(ref)
    assert await guard.is_service(PanelUserRef(panel_id=99)) is False


@pytest.mark.parametrize("error", [RemnawaveTransientError("timeout"), RemnawaveAuthError("401")])
async def test_unknown_target_and_a_silent_panel_means_no_write(error: Exception) -> None:
    """Fail-closed: if the panel can't say who the target is, the write doesn't happen."""
    fake = FakeRemnawaveClient()
    person = _seed(fake, "user_5072755730")
    fake.lookup_error = error
    guard = GuardedPanelClient(fake)
    with pytest.raises(type(error)):
        await guard.disable_user(person.uuid)
    assert fake.users[person.uuid].is_enabled is True
    # an explicit «no such user» is not a service account — the write goes on (and fails
    # on the panel by itself)
    fake.lookup_error = None
    assert await guard.is_service(PanelUserRef(panel_id=12345)) is False


async def test_create_with_a_service_name_is_refused() -> None:
    fake = FakeRemnawaveClient()
    guard = GuardedPanelClient(fake)
    spec = RemnawaveService(guard).build_spec(
        short_id="x",
        telegram_id=None,
        expire_at=dt.datetime.now(dt.UTC),
        traffic_limit_bytes=0,
        device_limit=None,
    )
    with pytest.raises(PanelServiceAccountError):
        await guard.create_user(dataclasses.replace(spec, username="cascade_relay_00ff00"))
    assert fake.users == {}
    await guard.create_user(spec)  # sub_x — an ordinary bot user
    assert len(fake.users) == 1


async def test_loader_failure_keeps_the_default_list() -> None:
    fake = FakeRemnawaveClient()
    monitor = _seed(fake, "ms-status-monitor")

    async def broken() -> tuple[tuple[str, ...], frozenset[str]]:
        raise RuntimeError("db down")

    guard = GuardedPanelClient(fake, broken)
    with pytest.raises(PanelServiceAccountError):
        await guard.enable_user(monitor.uuid)


async def test_panel_write_retry_skips_a_service_account(
    uow: UnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.infrastructure.taskiq import tasks

    fake = FakeRemnawaveClient()
    relay = _seed(fake, "cascade_relay_b74178")
    fake.users[relay.uuid] = dataclasses.replace(relay, is_enabled=True)
    async with uow:
        user = await make_user(uow, telegram_id=None)
        sub = Subscription(
            user_id=user.id,
            remnawave_uuid=relay.uuid,
            short_id=relay.short_id,
            status=SubscriptionStatus.DISABLED,  # would make the retry DISABLE it
        )
        await uow.subscriptions.add(sub)
        await uow.commit()
        sub_id = sub.id

    class _Container:
        remnawave_client = GuardedPanelClient(fake)

        def uow(self) -> UnitOfWork:
            return uow

    monkeypatch.setattr(tasks, "get_container", lambda: cast("object", _Container()))
    await tasks.panel_write_retry(sub_id)  # no raise -> no futile retries
    assert fake.users[relay.uuid].is_enabled is True


async def test_device_guard_never_flags_a_service_account(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    relay = _seed(fake, "cascade_relay_b74178")
    async with uow:
        user = await make_user(uow, telegram_id=None)
        sub = Subscription(
            user_id=user.id,
            remnawave_uuid=relay.uuid,
            short_id=relay.short_id,
            panel_username="cascade_relay_b74178",
            status=SubscriptionStatus.ACTIVE,
            device_limit=1,
        )
        await uow.subscriptions.add(sub)
        usage = {str(relay.uuid): {f"10.0.0.{i}" for i in range(20)}}
        cfg = GuardConfig(tolerance=0, action="disable", service_patterns=PATTERNS)
        assert await DeviceGuardService(fake).scan(uow, usage, cfg) == []
        assert sub.status is SubscriptionStatus.ACTIVE
        # without the list it WOULD be flagged — the skip is what protects it
        flagged = await DeviceGuardService(GuardedPanelClient(fake)).scan(
            uow, usage, GuardConfig(tolerance=0, action="disable")
        )
        assert len(flagged) == 1 and flagged[0].action == "alert"  # disable was refused
    assert fake.users[relay.uuid].is_enabled is True


async def test_panel_import_never_offers_service_accounts(uow: UnitOfWork) -> None:
    fake = FakeRemnawaveClient()
    _seed(fake, "cascade_relay_b74178")
    _seed(fake, "ms-status-monitor")
    person = _seed(fake, "user_5072755730")
    async with uow:
        found = await find_panel_only(uow, await fake.list_users(), service_patterns=PATTERNS)
    assert [c.panel.username for c in found] == [person.username]
