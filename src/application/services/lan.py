"""LAN Play — pure rules: room codes, Headscale names, the ACL policy, labels, room names.

Each subscription is one Headscale user with an OPAQUE random name ``lan-<10 hex>`` (stored in
``lan_accounts``; nothing in it points back to the subscription id, the account or the
customer). Its devices are that user's nodes, named by the bot (``cw-<6 hex>``). Rooms live
in our DB; the WHOLE Headscale policy is regenerated from them after every change. The policy
never allows all: for each room, the nodes of its member users may reach each other on every
port — and nothing else is allowed (an empty room list = deny all).

Headscale v0.29 policy syntax (``hscontrol/policy/v2``): a user is written ``name@``, a
destination ``name@:*``; a policy whose ``acls`` AND ``grants`` are both absent means allow-all,
so both keys are always present. Users not registered yet resolve to nobody (not an error).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

# 6 chars, no look-alikes (no 0/O, 1/I/L): 31^6 ≈ 887M codes.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

ROOM_NAME_MAX = 40
PREAUTH_KEY_TTL = dt.timedelta(minutes=10)

_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_HS_USER_RE = re.compile(r"lan-[0-9a-f]{10}")
# Our device name, possibly with the «-N» suffix Headscale adds on a name collision.
_HOSTNAME_RE = re.compile(r"(cw-[0-9a-f]{6})(?:-[0-9a-z]+)?")


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str | None:
    """User input → a canonical code, or None if it can't be one (spaces/dashes/case ignored)."""
    code = "".join(ch for ch in raw.upper() if ch not in " -\t")
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


def new_headscale_user() -> str:
    """A fresh opaque Headscale user name (starts with a letter, as Headscale requires)."""
    return f"lan-{secrets.token_hex(5)}"


def is_lan_user(name: str) -> bool:
    """One of OUR Headscale users (not tagged devices, not an admin's own user)."""
    return _HS_USER_RE.fullmatch(name) is not None


def new_hostname() -> str:
    """The device name the app passes to the daemon — no PC name, no user name."""
    return f"cw-{secrets.token_hex(3)}"


def public_device_name(node_name: str) -> str:
    """What other room members see: our ``cw-xxxxxx`` name, or a neutral placeholder."""
    m = _HOSTNAME_RE.fullmatch(node_name)
    return m.group(1) if m else "устройство"


def is_our_hostname(node_name: str) -> bool:
    return _HOSTNAME_RE.fullmatch(node_name) is not None


def clean_room_name(raw: str) -> str:
    """Drop control, format (bidi overrides, zero-width), surrogate, private-use and
    unassigned characters; collapse whitespace."""
    kept = "".join(ch for ch in raw if unicodedata.category(ch) not in _DROP_CATEGORIES)
    return " ".join(kept.split())[:ROOM_NAME_MAX]


def build_policy(rooms: Iterable[Sequence[str]]) -> dict[str, Any]:
    """Rooms (each = its members' Headscale user names) → the Headscale policy document.

    One ACL per room: src = the members, dst = the members on every port. Deterministic
    (sorted, de-duplicated) so an unchanged room set yields byte-identical JSON."""
    acls: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for members in rooms:
        names = tuple(sorted({str(m) for m in members if m}))
        if not names or names in seen:
            continue
        seen.add(names)
        users = [f"{n}@" for n in names]
        acls.append({"action": "accept", "src": users, "dst": [f"{u}:*" for u in users]})
    acls.sort(key=lambda a: a["src"])
    return {"acls": acls, "grants": []}


def policy_json(policy: dict[str, Any]) -> str:
    return json.dumps(policy, ensure_ascii=False, sort_keys=True, indent=2)


def same_policy(current: str, wanted: dict[str, Any]) -> bool:
    """Is the server's policy text the same document as ``wanted`` (formatting ignored)?"""
    try:
        return bool(json.loads(current) == wanted)
    except ValueError:
        return False  # HuJSON with comments / garbage — overwrite it


def member_label(position: int) -> str:
    """Seat label inside a room: by join order, never a name / e-mail / Telegram id."""
    return f"Игрок {position}"


def subscription_lan_active(
    status_usable: bool,
    expire_at: dt.datetime | None,
    now: dt.datetime | None = None,
    *,
    user_blocked: bool = False,
) -> bool:
    """LAN is for live subscriptions only: a usable status, not past its expiry, and an
    account that isn't blocked."""
    now = now or dt.datetime.now(dt.UTC)
    return status_usable and not user_blocked and (expire_at is None or expire_at > now)
