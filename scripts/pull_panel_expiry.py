"""One-off reconciliation: pull `expire_at` + the panel's numeric id INTO `subscriptions`.

Two independent drifts this fixes in one pass over the panel (each subscription is fetched once):

1. expire_at: the bot treats its own DB as authoritative (computes the date on purchase/renewal,
   pushes it to the panel — see src/application/services/subscription.py and resync.py). That
   breaks down when an admin edits dates directly in the panel (a single fix, or a bulk
   squad-wide extension): nothing pulls that change back into the bot unless Remnawave is
   configured to call this bot's /webhook/panel, which most deployments never set up. Run this
   after any direct-in-panel date edit so the bot's cabinet/mini-app (which read the local DB,
   not the panel) show the right date again.

2. remnawave_id: subscriptions imported from another bot (or created on a pre-3.0 panel) only
   ever got a uuid/short_id, never the numeric id Remnawave >=3.0 addresses users by. Every panel
   call for those has to *guess* the numeric id from a username/shortUuid pattern this bot
   controls — which fails for anything not created by this bot — then fall back to a slower,
   uncached-across-restarts telegram_id lookup (see client.py's `_v3_id`). Once resolved here
   and written to `subscriptions.remnawave_id`, `Subscription.panel_ref` carries the id directly
   and every future panel call for that subscription skips resolution entirely — this is the
   permanent fix; the telegram_id fallback stays as a safety net for whatever this misses.

Dry-run by default: prints what would change without writing anything. Pass --apply to commit.

Usage:
  uv run python scripts/pull_panel_expiry.py            # preview only
  uv run python scripts/pull_panel_expiry.py --apply     # write the changes to the DB
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

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
                # A uuid/short_id-only ref can't be resolved to a v3 numeric id when the panel
                # user's name doesn't match this bot's own naming (e.g. imported from another
                # bot) — the client's last-resort lookup is by telegram_id, so attach it (same
                # as subscription.py's grant/renew/change do before calling apply()).
                user = await uow.users.get(sub.user_id)
                if user is not None:
                    panel_ref = replace(panel_ref, telegram_id=user.telegram_id)
                try:
                    panel = await container.remnawave_client.get_user(panel_ref)
                except Exception as exc:
                    log.warning("panel fetch failed", sub=sub.id, error=str(exc))
                    continue
                if panel is None:
                    continue

                touched = False
                if panel.expire_at is not None:
                    drift = (
                        sub.expire_at is None
                        or abs((panel.expire_at - sub.expire_at).total_seconds()) > _DRIFT_SECONDS
                    )
                    if drift:
                        print(
                            f"sub #{sub.id} (user {sub.user_id}): expire_at "
                            f"bot={sub.expire_at} -> panel={panel.expire_at}"
                        )
                        if apply:
                            sub.expire_at = panel.expire_at
                        touched = True

                if sub.remnawave_id is None and panel.panel_id is not None:
                    print(
                        f"sub #{sub.id} (user {sub.user_id}): remnawave_id "
                        f"None -> {panel.panel_id}"
                    )
                    if apply:
                        sub.remnawave_id = panel.panel_id
                    touched = True

                if touched:
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
