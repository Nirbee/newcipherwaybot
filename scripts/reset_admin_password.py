"""Emergency access recovery: reset a staff account's password directly in the DB.

Bypasses the admin panel entirely (useful when locked out — a copy-paste mismatch, a stale
hash, etc.) — sets the password to exactly what you type here, no round-trip through .env or
a browser field to introduce whitespace/encoding drift. Refuses to touch a non-staff account.

Usage: uv run python scripts/reset_admin_password.py <username> <new_password>
"""

from __future__ import annotations

import asyncio
import sys

from src.core.logging import configure_logging, get_logger
from src.core.security import hash_password
from src.infrastructure.di import AppContainer

log = get_logger("reset_admin_password")


async def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: reset_admin_password.py <username> <new_password>")
        return 2
    username, password = argv
    if len(password) < 8:
        print("password must be at least 8 characters")
        return 2

    configure_logging(level="INFO", json=False)
    container = AppContainer.from_env()
    try:
        async with container.uow() as uow:
            user = await uow.users.find_one(username=username.lstrip("@"))
            if user is None:
                print(f"no user with username={username!r}")
                return 1
            if not user.role.is_staff:
                print(f"user {username!r} is not a staff account (role={user.role.name}) — refusing")
                return 1
            user.password_hash = hash_password(password)
            role_name = user.role.name
            await uow.commit()
    finally:
        await container.aclose()
    print(f"password reset for {username!r} (role={role_name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
