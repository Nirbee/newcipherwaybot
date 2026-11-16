"""Admin: owner-editable notification templates (per lifecycle stage, screen 08).

Every user-facing message the bot sends on a lifecycle event is a row here — the owner
edits the text (with ``{placeholders}``) and toggles it on/off from the cabinet or the
in-bot admin, instead of the text being hardcoded. ``bootstrap_notifications`` seeds any
missing event on boot with a nice default, so adding a new event in code needs no data
migration and never overwrites the owner's edits.
"""

# ruff: noqa: RUF001  — Russian UI templates mix Cyrillic with Latin {placeholders} by design.

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.infrastructure.database.models.notification_template import NotificationTemplate
from src.infrastructure.database.uow import UnitOfWork
from src.infrastructure.di import AppContainer
from src.web.deps import get_container
from src.web.routes.admin._common import audit
from src.web.routes.admin.deps import AdminIdentity, require_admin

router = APIRouter(prefix="/notifications")

# (event, RU title, default text, available placeholders). Order = display order.
NOTIFICATION_EVENTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "trial_started",
        "Пробный период выдан",
        "🎁 Пробный период активирован на {days} дн. Приятного пользования!",
        ("name", "days"),
    ),
    (
        "purchase",
        "Оплата подписки",
        "✅ Оплата получена — подписка активна! Нажми «Подключить», чтобы настроить приложение.",
        ("name", "plan", "expire"),
    ),
    (
        "balance_topup",
        "Пополнение баланса",
        "💰 Баланс пополнен на {amount}. Текущий баланс: {balance}.",
        ("name", "amount", "balance"),
    ),
    (
        "renewal",
        "Продление подписки",
        "🔄 Подписка продлена до {expire}. Спасибо, что остаёшься с нами!",
        ("name", "plan", "expire"),
    ),
    (
        "autopay_success",
        "Автопродление выполнено",
        "🔁 Автопродление выполнено — подписка продлена до {expire}.",
        ("plan", "expire"),
    ),
    (
        "autopay_failed",
        "Автопродление не удалось",
        "⚠️ Не удалось списать оплату по карте. Продли вручную или пополни баланс — "
        "иначе доступ отключится.",
        (),
    ),
    (
        "plan_changed",
        "Смена тарифа",
        "🔀 Тариф изменён на «{plan}». Подписка действует до {expire}.",
        ("plan", "expire"),
    ),
    (
        "traffic_topup",
        "Докупка трафика",
        "📶 Трафик добавлен к подписке. Приятного пользования!",
        (),
    ),
    (
        "referral_reward",
        "Реферальная награда",
        "🎉 Тебе начислено {amount} за приглашённого друга. Спасибо!",
        ("name", "amount"),
    ),
    (
        "refund",
        "Возврат средств",
        "↩️ Оформлен возврат {amount}. Если это ошибка — напиши в поддержку.",
        ("amount",),
    ),
    (
        "payment_failed",
        "Оплата не прошла",
        "❌ Оплата {amount} не прошла. Попробуй ещё раз или выбери другой способ оплаты.",
        ("amount",),
    ),
    (
        "payment_canceled",
        "Счёт отменён",
        "✖️ Счёт на {amount} отменён. Если ты уже оплатил — напиши в поддержку.",
        ("amount",),
    ),
    (
        "payment_underpaid",
        "Оплата меньше счёта",
        "⚠️ Поступило {paid} при счёте {expected} — оплата не засчитана. "
        "Напиши в поддержку, мы разберёмся и вернём деньги.",
        ("paid", "expected"),
    ),
    (
        "auto_purchase",
        "Автопокупка после пополнения",
        "✅ Подписка «{plan}» оформлена автоматически после пополнения — действует до {expire}.",
        ("plan", "expire"),
    ),
    (
        "auto_purchase_failed",
        "Автопокупка не удалась",
        "⚠️ Баланс пополнен, но оформить подписку автоматически не получилось. "
        "Деньги на балансе — заверши покупку вручную в меню.",
        (),
    ),
    (
        "grace_started",
        "Спасательный круг включён",
        "🛟 Подписка истекла, но мы оставили тебе доступ ещё на {days} дн. "
        "(до {gb} ГБ). Продли, чтобы вернуть полную скорость и лимиты.",
        ("name", "days", "gb", "plan"),
    ),
    (
        "grace_ended",
        "Спасательный круг закончился",
        "⛔ Дополнительный доступ закончился, подписка отключена. "
        "Продли в пару тапов — и всё снова заработает.",
        ("name", "plan"),
    ),
)

_DEFAULT = {ev: text for ev, _, text, _ in NOTIFICATION_EVENTS}
_META = {ev: (title, ph) for ev, title, _, ph in NOTIFICATION_EVENTS}

# --- e-mail letters (people without Telegram: web cabinet / guest buyers) ----------------
# Same {placeholder} mechanism as the Telegram set, but a letter has a subject AND a body, so
# it is a parallel set. Stored in the same table under ``email:<event>`` (body + on/off) and
# ``email:<event>:subject`` — no schema change. Rows exist only once the owner edits a letter;
# untouched letters follow the code default (so better defaults reach them on deploy).
# The body is PLAIN text: the HTML part is built from it with everything escaped, so neither
# the owner's text nor a value (plan name, user name) can inject markup into the letter.
_EMAIL_PAY_PH = ("plan", "amount", "expire", "sub_url", "cabinet_url", "support")
_EMAIL_HOWTO = (
    "Как подключиться:\n"
    "1. Установите приложение: на телефоне и Mac — Happ, v2RayTun или Hiddify; "
    "на Windows — {windows_app}.\n"
    "2. Скопируйте ссылку-подписку и добавьте её в приложение "
    "(кнопка «+» → «Вставить из буфера»).\n"
    "3. Включите VPN."
)
# (event, RU title, default subject, default body, placeholders). Order = display order.
EMAIL_EVENTS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    (
        "purchase",
        "Оплата подписки",
        "Подписка оплачена — VPN готов к работе",
        "Здравствуйте!\n\n"
        "Оплата получена, подписка активна.\n\n"
        "Тариф: {plan}\nСумма: {amount}\nДействует до: {expire}\n\n"
        "Ваша ссылка-подписка:\n{sub_url}\n\n"
        f"{_EMAIL_HOWTO}\n\n"
        "Войти в кабинет: {cabinet_url}\n\n"
        "{support}",
        (*_EMAIL_PAY_PH, "windows_app"),
    ),
    (
        "renewal",
        "Продление подписки",
        "Подписка продлена до {expire}",
        "Здравствуйте!\n\n"
        "Оплата получена, подписка продлена.\n\n"
        "Тариф: {plan}\nСумма: {amount}\nДействует до: {expire}\n\n"
        "Ссылка-подписка прежняя — приложение подхватит продление само:\n{sub_url}\n\n"
        "Войти в кабинет: {cabinet_url}\n\n"
        "{support}",
        _EMAIL_PAY_PH,
    ),
    (
        "plan_changed",
        "Смена тарифа",
        "Тариф изменён на «{plan}»",
        "Здравствуйте!\n\n"
        "Оплата получена, тариф изменён.\n\n"
        "Тариф: {plan}\nСумма: {amount}\nДействует до: {expire}\n\n"
        "Ваша ссылка-подписка:\n{sub_url}\n\n"
        "Войти в кабинет: {cabinet_url}\n\n"
        "{support}",
        _EMAIL_PAY_PH,
    ),
    (
        "traffic_topup",
        "Докупка трафика",
        "Трафик добавлен к подписке",
        "Здравствуйте!\n\n"
        "Оплата получена, трафик добавлен к подписке.\n\n"
        "Сумма: {amount}\nПодписка действует до: {expire}\n\n"
        "Войти в кабинет: {cabinet_url}\n\n"
        "{support}",
        _EMAIL_PAY_PH,
    ),
    (
        "balance_topup",
        "Пополнение баланса",
        "Баланс пополнен на {amount}",
        "Здравствуйте!\n\n"
        "Баланс пополнен на {amount}. Текущий баланс: {balance}.\n\n"
        "Войти в кабинет: {cabinet_url}\n\n"
        "{support}",
        ("amount", "balance", "cabinet_url", "support"),
    ),
    (
        "expiring",
        "Подписка скоро закончится",
        "Подписка закончится через {time}",
        "Здравствуйте!\n\n"
        "Подписка «{plan}» закончится через {time} ({expire}).\n\n"
        "Продлите её заранее, чтобы VPN не отключился:\n{renew_url}\n\n"
        "{support}",
        ("plan", "expire", "hours", "time", "renew_url", "support"),
    ),
    (
        "expired",
        "Подписка закончилась",
        "Подписка закончилась",
        "Здравствуйте!\n\n"
        "Подписка «{plan}» закончилась, VPN отключён.\n\n"
        "Продлите её — и всё снова заработает в том же приложении:\n{renew_url}\n\n"
        "{support}",
        ("plan", "expire", "renew_url", "support"),
    ),
)

_EMAIL_DEFAULT = {ev: (subj, body) for ev, _t, subj, body, _ph in EMAIL_EVENTS}
_EMAIL_META = {ev: (title, ph) for ev, title, _s, _b, ph in EMAIL_EVENTS}
_EMAIL_SUBJECT_MAX = 255


def _email_key(event: str) -> str:
    return f"email:{event}"


def _email_subject_key(event: str) -> str:
    return f"email:{event}:subject"


def render(text: str, **values: Any) -> str:
    """Substitute ``{key}`` placeholders; leaves unknown ones untouched."""
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _tidy(text: str) -> str:
    """An empty block placeholder ({support} with no contact) must not leave a gap."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


async def _email_rows(uow: UnitOfWork, event: str) -> tuple[Any, Any]:
    return (
        await uow.notifications.by_event(_email_key(event)),
        await uow.notifications.by_event(_email_subject_key(event)),
    )


async def email_template(uow: UnitOfWork, event: str, **values: Any) -> tuple[str, str] | None:
    """Rendered ``(subject, plain body)`` of a letter, or None when the owner disabled it.

    Values are substituted raw — this is the plain-text part; the HTML part escapes it
    (see ``src.infrastructure.services.user_mail``). Unknown events render nothing."""
    if event not in _EMAIL_DEFAULT:
        return None
    body_row, subject_row = await _email_rows(uow, event)
    if body_row is not None and not body_row.enabled:
        return None
    default_subject, default_body = _EMAIL_DEFAULT[event]
    subject = subject_row.text if subject_row is not None else default_subject
    body = body_row.text if body_row is not None else default_body
    # A subject is one line: a value with a newline must not smuggle in extra headers.
    subject = " ".join(render(subject, **values).split())[:_EMAIL_SUBJECT_MAX]
    return subject, _tidy(render(body, **values))


async def notification_text(uow: UnitOfWork, event: str, **values: Any) -> str | None:
    """Rendered text for an event, or None when the owner disabled it (caller stays silent).

    Falls back to the code default when the row hasn't been seeded yet, so a notification
    never goes missing between a deploy and the next boot-seed.
    """
    row = await uow.notifications.by_event(event)
    if row is not None and not row.enabled:
        return None
    text = row.text if row is not None else _DEFAULT.get(event)
    return render(text, **values) if text else None


async def bootstrap_notifications(container: AppContainer) -> None:
    """Seed any canonical event missing from the table. Additive + idempotent — never
    overwrites an edited row, and auto-adds events introduced in later releases."""
    async with container.uow() as uow:
        have = {t.event for t in await uow.notifications.ordered()}
        added = False
        for event, _title, text, _ph in NOTIFICATION_EVENTS:
            if event not in have:
                await uow.notifications.add(NotificationTemplate(event=event, text=text))
                added = True
        if added:
            await uow.commit()


def _serialize(event: str, text: str, enabled: bool) -> dict[str, Any]:
    title, placeholders = _META[event]
    return {
        "event": event,
        "title": title,
        "text": text,
        "enabled": enabled,
        "placeholders": list(placeholders),
    }


@router.get("")
async def list_notifications(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    async with container.uow() as uow:
        rows = {t.event: t for t in await uow.notifications.ordered()}
    items = [
        _serialize(
            ev,
            rows[ev].text if ev in rows else default,
            rows[ev].enabled if ev in rows else True,
        )
        for ev, _title, default, _ph in NOTIFICATION_EVENTS
    ]
    return {"items": items}


class NotificationPatch(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=4096)
    enabled: bool | None = None


@router.patch("/{event}")
async def update_notification(
    event: str,
    body: NotificationPatch,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if event not in _DEFAULT:
        raise HTTPException(404, "unknown notification event")
    async with container.uow() as uow:
        row = await uow.notifications.by_event(event)
        if row is None:
            row = NotificationTemplate(event=event, text=_DEFAULT[event])
            await uow.notifications.add(row)
        if body.text is not None:
            row.text = body.text
        if body.enabled is not None:
            row.enabled = body.enabled
        await audit(uow, identity, "notification.update", event)
        await uow.commit()
        return _serialize(event, row.text, row.enabled)


# --- e-mail letters: admin «Уведомления» → «Письма» ---------------------------------------


def _serialize_email(event: str, subject: str, text: str, enabled: bool) -> dict[str, Any]:
    title, placeholders = _EMAIL_META[event]
    default_subject, default_text = _EMAIL_DEFAULT[event]
    return {
        "event": event,
        "title": title,
        "subject": subject,
        "text": text,
        "enabled": enabled,
        "placeholders": list(placeholders),
        "is_default": subject == default_subject and text == default_text,
    }


async def _email_state(uow: UnitOfWork, event: str) -> dict[str, Any]:
    body_row, subject_row = await _email_rows(uow, event)
    default_subject, default_text = _EMAIL_DEFAULT[event]
    return _serialize_email(
        event,
        subject_row.text if subject_row is not None else default_subject,
        body_row.text if body_row is not None else default_text,
        body_row.enabled if body_row is not None else True,
    )


@router.get("/email")
async def list_email_notifications(
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    async with container.uow() as uow:
        items = [await _email_state(uow, ev) for ev, *_rest in EMAIL_EVENTS]
    return {"items": items}


class EmailNotificationPatch(BaseModel):
    subject: str | None = Field(None, min_length=1, max_length=_EMAIL_SUBJECT_MAX)
    text: str | None = Field(None, min_length=1, max_length=4096)
    enabled: bool | None = None


async def _email_upsert(uow: UnitOfWork, event: str) -> tuple[Any, Any]:
    """Both rows of a letter, created from the defaults when the owner touches it first."""
    body_row, subject_row = await _email_rows(uow, event)
    default_subject, default_text = _EMAIL_DEFAULT[event]
    if body_row is None:
        body_row = NotificationTemplate(event=_email_key(event), text=default_text)
        await uow.notifications.add(body_row)
    if subject_row is None:
        subject_row = NotificationTemplate(event=_email_subject_key(event), text=default_subject)
        await uow.notifications.add(subject_row)
    return body_row, subject_row


@router.patch("/email/{event}")
async def update_email_notification(
    event: str,
    body: EmailNotificationPatch,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    if event not in _EMAIL_DEFAULT:
        raise HTTPException(404, "unknown e-mail event")
    async with container.uow() as uow:
        body_row, subject_row = await _email_upsert(uow, event)
        if body.subject is not None:
            subject_row.text = " ".join(body.subject.split())  # one line: no header injection
        if body.text is not None:
            body_row.text = body.text
        if body.enabled is not None:
            body_row.enabled = body.enabled
        await audit(uow, identity, "notification.email.update", event)
        await uow.commit()
        return await _email_state(uow, event)


@router.post("/email/{event}/reset")
async def reset_email_notification(
    event: str,
    identity: AdminIdentity = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> dict[str, Any]:
    """Back to the stock subject + text (the on/off switch is kept as is)."""
    if event not in _EMAIL_DEFAULT:
        raise HTTPException(404, "unknown e-mail event")
    async with container.uow() as uow:
        body_row, subject_row = await _email_upsert(uow, event)
        body_row.text = _EMAIL_DEFAULT[event][1]
        subject_row.text = _EMAIL_DEFAULT[event][0]
        await audit(uow, identity, "notification.email.reset", event)
        await uow.commit()
        return await _email_state(uow, event)
