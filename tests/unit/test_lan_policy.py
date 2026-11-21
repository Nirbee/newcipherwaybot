"""LAN Play pure rules: the Headscale ACL policy (isolation, never allow-all), room codes,
opaque Headscale names, device names, seat labels, room names, the «live subscription» check."""

from __future__ import annotations

import datetime as dt
import json

from src.application.services.lan import (
    CODE_ALPHABET,
    CODE_LENGTH,
    ROOM_NAME_MAX,
    build_policy,
    clean_room_name,
    generate_code,
    is_lan_user,
    is_our_hostname,
    member_label,
    new_headscale_user,
    new_hostname,
    normalize_code,
    policy_json,
    public_device_name,
    same_policy,
    subscription_lan_active,
)

A, B, C, D, E = (f"lan-{i:010x}" for i in range(1, 6))


def _reach(policy: dict, src: str) -> set[str]:
    """Which users ``src`` may reach under the policy (dst entries without the port)."""
    out: set[str] = set()
    for acl in policy["acls"]:
        if f"{src}@" in acl["src"]:
            out |= {d.rsplit(":", 1)[0].rstrip("@") for d in acl["dst"]}
    return out


def test_rooms_are_isolated_from_each_other() -> None:
    policy = build_policy([[A, B, C], [D, E]])
    assert _reach(policy, A) == {A, B, C}
    assert _reach(policy, D) == {D, E}
    assert _reach(policy, "lan-ffffffffff") == set()  # not in any room → reaches nobody
    for acl in policy["acls"]:
        assert acl["action"] == "accept"
        assert all(d.endswith("@:*") for d in acl["dst"])  # every port of the members


def test_member_of_two_rooms_bridges_nobody() -> None:
    """Being in rooms X and Y lets you reach both — but X's members still can't reach Y's."""
    policy = build_policy([[A, B], [B, C]])
    assert _reach(policy, B) == {A, B, C}
    assert C not in _reach(policy, A)
    assert A not in _reach(policy, C)


def test_never_allow_all() -> None:
    for rooms in ([], [[A]], [[A, B], [C, D, E]]):
        policy = build_policy(rooms)
        # Headscale treats a policy without BOTH acls and grants as allow-all
        assert "acls" in policy and "grants" in policy
        assert policy["grants"] == []
        text = json.dumps(policy)
        assert '"*"' not in text and "autogroup" not in text and "*:*" not in text
        assert "sub-" not in text  # no subscription ids in anything peers can see
        for acl in policy["acls"]:
            assert all(is_lan_user(s.rstrip("@")) for s in acl["src"])
    assert build_policy([]) == {"acls": [], "grants": []}  # no rooms = deny all


def test_policy_is_deterministic() -> None:
    a = build_policy([[C, A, B, B], [E, D]])
    b = build_policy([[D, E], [B, A, C]])
    assert policy_json(a) == policy_json(b)
    assert a["acls"][0]["src"] == [f"{A}@", f"{B}@", f"{C}@"]  # de-duplicated, sorted
    assert same_policy(json.dumps(a), b)
    assert not same_policy("", a)
    assert not same_policy("// hujson comment\n{}", a)


def test_codes() -> None:
    codes = {generate_code() for _ in range(300)}
    assert len(codes) > 290
    for code in codes:
        assert len(code) == CODE_LENGTH and set(code) <= set(CODE_ALPHABET)
    assert not set("0O1IL") & set(CODE_ALPHABET)  # no look-alikes
    assert normalize_code(" ab-c2 34 ") == "ABC234"
    assert normalize_code("ABC23") is None
    assert normalize_code("ABC230") is None  # 0 is not in the alphabet
    assert normalize_code("ABCDEFG") is None


def test_opaque_names() -> None:
    names = {new_headscale_user() for _ in range(50)}
    assert len(names) == 50 and all(is_lan_user(n) and n[0].isalpha() for n in names)
    assert not is_lan_user("sub-42") and not is_lan_user("tagged-devices")
    host = new_hostname()
    assert is_our_hostname(host) and len(host) == 9
    assert public_device_name(host) == host
    assert public_device_name(f"{host}-x7k2") == host  # Headscale's collision suffix
    assert public_device_name("desktop-vasya") == "устройство"
    assert member_label(1) == "Игрок 1"


def test_room_names_are_cleaned() -> None:
    assert clean_room_name("  Наш \t сервер \n") == "Наш сервер"
    # bidi override, zero-width space/joiner, BOM, control characters are dropped
    assert clean_room_name("evil‮gnp.exe​‍﻿\x07") == "evilgnp.exe"
    assert clean_room_name("​ ⁦") == ""
    # lone surrogate (Cs), private use (Co, BMP and plane 15), unassigned (Cn) are dropped too
    assert clean_room_name("a\ud800bc\U000f0001d͸e") == "abcde"
    assert len(clean_room_name("я" * 100)) == ROOM_NAME_MAX


def test_live_subscription() -> None:
    now = dt.datetime(2026, 11, 18, tzinfo=dt.UTC)
    assert subscription_lan_active(True, None, now)
    assert subscription_lan_active(True, now + dt.timedelta(hours=1), now)
    assert not subscription_lan_active(True, now - dt.timedelta(seconds=1), now)
    assert not subscription_lan_active(False, now + dt.timedelta(days=1), now)
    assert not subscription_lan_active(True, None, now, user_blocked=True)
