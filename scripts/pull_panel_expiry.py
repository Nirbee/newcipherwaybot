"""One-off reconciliation: pull `expire_at` FROM Remnawave INTO the local `subscriptions` table.

The bot normally treats its own DB as authoritative for expire_at (it computes the date on
purchase/renewal and pushes it to the panel — see src/application/services/subscription.py and
src/application/services/resync.py). That breaks down when an admin edits dates directly in the
panel (a single fix, or a bulk squad-wide extension): nothing pulls that change back into the bot
unless Remnawave is configured to call this bot's /webhook/panel, which most deployments never
set up. This script is the manual fallback for that case — run it once after any direct-in-panel
date edit so the bot's cabinet/mini-app (which read the local DB, not the panel) show the right
date again.

Dry-run by default: prints every subscription whose local expire_at disagrees with the panel's by
more than a minute, without writing anything. Pass --apply to actually commit the panel's value
into the local DB.

Usage:
  uv run python scripts/pull_panel_expiry.py            # preview only
  uv run python scripts/pull_panel_expiry.py --apply     # write the panel's dates into the DB
"""

from __future__ import annotations

import asyncio
import sys

from src.core.logging import configure_logging, get_logger
from src.infrastructure.di import AppContainer

log = get_logger("pull_panel_expiry")

_DRIFT_SECONDS = 60
_WORKING_SET_LIMIT = 100_000


async def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    configure_logging(level="INFO", json=False)

    container = AppContainer.from_env()
    try:
        async with container.uow() as uow:
            subs = await uow.subscriptions.live_with_panel(_WORKING_SET_LIMIT)
            log.info("checking subscriptions", count=len(subs))

            changed = 0
            for sub in subs:
                panel_ref = sub.panel_ref
                if panel_ref is None:
                    continue
                try:
                    panel = await container.remnawave_client.get_user(panel_ref)
                except Exception as exc:
                    log.warning("panel fetch failed", sub=sub.id, error=str(exc))
                    continue
                if panel is None or panel.expire_at is None:
                    continue

                drift = (
                    sub.expire_at is None
                    or abs((panel.expire_at - sub.expire_at).total_seconds()) > _DRIFT_SECONDS
                )
                if not drift:
                    continue

                print(
                    f"sub #{sub.id} (user {sub.user_id}): "
                    f"bot={sub.expire_at} -> panel={panel.expire_at}"
                )
                if apply:
                    sub.expire_at = panel.expire_at
                changed += 1

            if apply and changed:
                await uow.commit()
    finally:
        await container.aclose()

    print(
        f"\n{'Applied' if apply else 'Would update'} {changed} subscription(s)."
        + ("" if apply else " Re-run with --apply to write these changes.")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
