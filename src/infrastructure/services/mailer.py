"""Minimal SMTP mailer for the web cabinet (verification / receipts).

Best-effort: blocking smtplib runs in a thread; a misconfigured or unreachable SMTP
never raises into the request path. Off unless SMTP host + from-address are set.
"""

from __future__ import annotations

import asyncio
import hashlib
import smtplib
import socket
from email.mime.text import MIMEText
from typing import Any

from src.core.logging import get_logger

log = get_logger(__name__)


class SmtpConnectError(OSError):
    """Couldn't open a TCP connection to the SMTP server at all — the message names every
    address tried, so «blocked by the hosting» doesn't hide behind an IPv6 error."""


def _connect_ipv4_first(host: str, port: int, timeout: float | None) -> socket.socket:
    """Like socket.create_connection, but IPv4 first and every failure reported.

    In a Docker container IPv6 is usually unroutable; create_connection tried the AAAA
    address first and, when IPv4 also failed, surfaced only «[Errno 101] Network is
    unreachable» — which says nothing about the real cause (field: the hosting blocked
    outbound SMTP)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    infos.sort(key=lambda i: i[0] != socket.AF_INET)
    errors: list[str] = []
    for family, kind, proto, _name, addr in infos:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(addr)
            return sock
        except OSError as exc:
            errors.append(f"{addr[0]} — {exc.strerror or exc}")
            sock.close()
    raise SmtpConnectError(
        f"сервер не может подключиться к {host}:{port} ({'; '.join(errors) or 'нет адресов'}). "
        "Скорее всего, хостинг блокирует исходящую почту: попробуйте порт 587 "
        "или попросите хостинг открыть исходящие порты 465 и 587."
    )


class _SMTP(smtplib.SMTP):
    def _get_socket(self, host: str, port: int, timeout: Any) -> socket.socket:
        return _connect_ipv4_first(host, port, timeout)


class _SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: Any) -> socket.socket:
        sock = _connect_ipv4_first(host, port, timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)  # type: ignore[attr-defined]


class Mailer:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        from_addr: str,
        *,
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = from_addr
        self._tls = use_tls

    @property
    def configured(self) -> bool:
        return bool(self._host and self._from)

    @property
    def fingerprint(self) -> str:
        """Identifies these exact settings: a successful test letter is remembered against
        it, so any change of server/port/login/password/sender needs a new test."""
        raw = "|".join((self._host, str(self._port), self._user, self._password, self._from))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = to
        # 465 is implicit TLS (Yandex, Mail.ru, many hosts offer only that); 587/25 = STARTTLS.
        if self._port == 465:
            with _SMTP_SSL(self._host, self._port, timeout=15) as smtp:
                if self._user:
                    smtp.login(self._user, self._password)
                smtp.send_message(msg)
            return
        with _SMTP(self._host, self._port, timeout=15) as smtp:
            if self._tls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)

    async def send_strict(self, to: str, subject: str, body: str) -> None:
        """Like send(), but raises — for the admin's «test e-mail», which must show the real
        SMTP error (wrong password, blocked port…) instead of a silent False."""
        await asyncio.to_thread(self._send_sync, to, subject, body)

    async def send(self, to: str, subject: str, body: str) -> bool:
        if not self.configured:
            return False
        try:
            await asyncio.to_thread(self._send_sync, to, subject, body)
        except Exception as exc:
            log.warning("email send failed", to=to, error=str(exc))
            return False
        return True


MAIL_VERIFIED_KEY = "mail:verified"


async def mail_verified(redis: object, mailer: Mailer) -> bool:
    """True once a test letter went out with the CURRENT settings. «Fields filled» isn't
    enough: the hosting may block outbound SMTP (field), and then every code / hint that
    promises an e-mail would be a lie."""
    if not mailer.configured:
        return False
    try:
        stored = await redis.get(MAIL_VERIFIED_KEY)  # type: ignore[attr-defined]
    except Exception:
        return False
    if isinstance(stored, bytes):
        stored = stored.decode()
    return stored == mailer.fingerprint
