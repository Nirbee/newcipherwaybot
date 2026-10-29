"""Game pulse (agent v7): target validation, event dedup, aggregation and the card verdict."""

from __future__ import annotations

import json

import pytest

from src.application.services import game_pulse as gp
from src.core import config_registry as registry

NOW = 1_800_000_000.0


def _run(ago: float, rows: list[list[object]]) -> dict[str, object]:
    return {"ts": int(NOW - ago), "r": rows}


# --- targets ------------------------------------------------------------------------------


def test_default_targets_are_the_registry_default_and_skip_unverified_moscow() -> None:
    default = registry.spec("GAME_PULSE_TARGETS").default
    targets = gp.parse_targets(default)
    assert [t["id"] for t in targets] == ["aws-fra", "aws-sto", "rbx-fra"]
    assert targets[2]["resolve_ip"] == "128.116.44.3"
    assert gp.parse_targets("") == targets
    assert gp.parse_targets("not json") == targets


def test_parse_targets_skips_bad_entries_and_caps_at_four() -> None:
    raw = json.dumps(
        [
            {"id": "ok-1", "url": "https://a.example.com/"},
            {"id": "BAD ID", "url": "https://b.example.com/"},
            {"id": "plain", "url": "http://insecure.example.com/"},
            {"id": "inj", "url": "https://x.example.com/ -o /tmp/x"},
            {"id": "ok-1", "url": "https://dup.example.com/"},
            *({"id": f"t{i}", "url": f"https://t{i}.example.com/"} for i in range(5)),
        ]
    )
    ids = [t["id"] for t in gp.parse_targets(raw)]
    assert ids == ["ok-1", "t0", "t1", "t2"]
    assert gp.parse_targets("[]") == []


def test_clean_targets_rejects_too_many_duplicates_and_bad_ip() -> None:
    one = {"id": "a", "url": "https://a.example.com:443/x"}
    assert gp.clean_targets([one])[0]["url"] == "https://a.example.com:443/x"
    with pytest.raises(gp.TargetError):
        gp.clean_targets([one, dict(one)])
    with pytest.raises(gp.TargetError):
        gp.clean_targets([{"id": f"t{i}", "url": "https://a.example.com/"} for i in range(5)])
    with pytest.raises(gp.TargetError):
        gp.clean_targets([{**one, "resolve_ip": "::1"}])


@pytest.mark.parametrize(
    "url",
    [
        "https://a.example.com:8443/",  # only 443
        "https://localhost/",
        "https://router/",  # no dot
        "https://nas.local/",
        "https://pc.lan/",
        "https://db.internal/",
        "https://box.home.arpa/",
        "https://my.keenetic.net/",
        "https://192.168.1.1/",
        "https://10.0.0.1/",
        "https://127.0.0.1/",
        "https://169.254.1.1/",
        "https://a.example.com/\nX-Evil: 1",
        "https://a.example.com/a b",
        "https://a.example.com/\t",
        "https://a.example.com/\x7f",
    ],
)
def test_clean_target_rejects_local_and_odd_addresses(url: str) -> None:
    with pytest.raises(gp.TargetError):
        gp.clean_target({"id": "x", "url": url})


@pytest.mark.parametrize("ip", ["192.168.1.1", "10.1.2.3", "127.0.0.1", "100.64.0.1", "0.0.0.0"])
def test_clean_target_rejects_non_public_resolve_ip(ip: str) -> None:
    with pytest.raises(gp.TargetError):
        gp.clean_target({"id": "x", "url": "https://a.example.com/", "resolve_ip": ip})


def test_clean_target_accepts_public_ip_literal_and_trailing_spaces() -> None:
    t = gp.clean_target({"id": "x", "url": " https://8.8.8.8/ ", "resolve_ip": "128.116.44.3"})
    assert t["url"] == "https://8.8.8.8/" and t["resolve_ip"] == "128.116.44.3"


def test_agent_settings_off_sends_no_targets() -> None:
    targets = gp.default_targets()
    off = gp.agent_settings(False, targets)
    assert off == {"enabled": False, "every_runs": 2, "timeout_s": 3, "targets": []}
    on = gp.agent_settings(True, targets)
    assert on["targets"][2] == {
        "id": "rbx-fra",
        "url": "https://www.roblox.com/",
        "resolve_ip": "128.116.44.3",
    }
    assert "title" not in on["targets"][0]


# --- events -------------------------------------------------------------------------------


def test_merge_events_dedups_by_ts_and_kind_and_drops_junk() -> None:
    stored = [{"ts": int(NOW - 600), "kind": "config_applied", "note": "etag=abc"}]
    incoming = [
        {"ts": int(NOW - 600), "kind": "config_applied", "note": "etag=abc"},  # repeat
        {"ts": int(NOW - 600), "kind": "watchdog_stop", "note": ""},  # same ts, other kind
        {"ts": int(NOW - 60), "kind": "self_update", "note": "v6 -> v7"},
        {"ts": int(NOW - 40 * 86400), "kind": "rollback"},  # older than 30 days
        {"ts": int(NOW + 5 * 86400), "kind": "rollback"},  # router clock in the future
        {"ts": "x", "kind": "rollback"},
        {"ts": int(NOW), "kind": "Bad Kind!"},
        "garbage",
    ]
    merged = gp.merge_events(stored, incoming, NOW)
    assert [(e["kind"], e["ts"]) for e in merged] == [
        ("config_applied", int(NOW - 600)),
        ("watchdog_stop", int(NOW - 600)),
        ("self_update", int(NOW - 60)),
    ]
    # Re-sending the same tail every 5 minutes doesn't grow the journal.
    assert gp.merge_events(merged, incoming, NOW) == merged


def test_merge_events_keeps_the_newest_500() -> None:
    stored = [{"ts": int(NOW) - i, "kind": "config_applied"} for i in range(600)]
    merged = gp.merge_events(stored, [], NOW)
    assert len(merged) == gp.EVENTS_KEEP
    assert merged[-1]["ts"] == int(NOW)


def test_restart_counts_only_count_restarts() -> None:
    events = [
        {"ts": int(NOW - 3600), "kind": "config_applied"},
        {"ts": int(NOW - 7200), "kind": "watchdog_stop"},
        {"ts": int(NOW - 7000), "kind": "watchdog_resume"},
        {"ts": int(NOW - 100), "kind": "self_update"},
        {"ts": int(NOW - 3 * 86400), "kind": "rollback"},
        {"ts": int(NOW - 9 * 86400), "kind": "config_applied"},
    ]
    assert gp.restart_counts(events, NOW) == {"day": 2, "week": 3}


# --- pulse aggregation --------------------------------------------------------------------


def test_append_pulse_sanitizes_and_expires_old_runs() -> None:
    old = [_run(8 * 86400, [["aws-fra", "direct", 40]])]
    runs = gp.append_pulse(
        old,
        {
            "ts": 1,
            "results": [
                {"target": "aws-fra", "path": "direct", "ms": 46},
                {"target": "aws-fra", "path": "proxy-ab", "ms": None},
                {"target": "aws-fra", "path": "proxy-cd", "ms": 999_999},
                {"target": "", "path": "direct", "ms": 1},
                {"target": "unknown", "path": "direct", "ms": 1},
                {"target": "aws-fra", "path": "Bad Path", "ms": 1},
                {"target": "aws-fra", "path": "direct\n", "ms": 1},
                "junk",
            ],
        },
        NOW,
        {"aws-fra"},
    )
    assert runs == [
        {
            "ts": int(NOW),
            "r": [
                ["aws-fra", "direct", 46],
                ["aws-fra", "proxy-ab", None],
                ["aws-fra", "proxy-cd", None],
            ],
        }
    ]
    assert gp.append_pulse(runs, {"results": []}, NOW, {"aws-fra"}) == runs


def test_append_pulse_survives_infinity_and_overflow() -> None:
    report = {
        "results": [
            {"target": "t", "path": "direct", "ms": float("inf")},
            {"target": "t", "path": "proxy-a", "ms": float("nan")},
            {"target": "t", "path": "proxy-b", "ms": 10**400},
            {"target": "t", "path": "proxy-c", "ms": True},
        ]
    }
    runs = gp.append_pulse([], report, NOW, {"t"})
    assert [row[2] for row in runs[0]["r"]] == [None, None, None, None]
    events = [{"ts": float("inf"), "kind": "rollback"}, {"ts": 10**400, "kind": "rollback"}]
    assert gp.merge_events([], events, NOW) == []


def test_append_pulse_caps_rows_targets_and_paths_against_junk() -> None:
    import random

    rnd = random.Random(1)
    targets = {"aws-fra", "aws-sto"}
    report = {
        "results": [
            {
                "target": rnd.choice(["aws-fra", "aws-sto", f"x{i}"]),
                "path": f"proxy-{rnd.randrange(16**8):08x}",
                "ms": rnd.randrange(300),
            }
            for i in range(40)
        ]
    }
    runs = gp.append_pulse([], report, NOW, targets)
    rows = [row for run in runs for row in run["r"]]
    assert all(row[0] in targets for row in rows)
    assert len({row[1] for row in rows}) <= gp.MAX_DEVICE_PATHS
    assert len(rows) <= gp.MAX_TARGETS * gp.MAX_PATHS
    card = gp.pulse_card(runs, gp.default_targets(), {}, NOW)
    assert len(card["paths"]) <= gp.MAX_DEVICE_PATHS
    assert [r["target"] for r in card["rows"]] == ["aws-fra", "aws-sto", "rbx-fra"]


def test_append_pulse_keeps_the_newest_paths_after_a_server_change() -> None:
    old = [_run(600, [["t", p, 50] for p in ("direct", "proxy-a", "proxy-b", "cand-1", "cand-2")])]
    new = {"results": [{"target": "t", "path": p, "ms": 40} for p in ("direct", "proxy-c")]}
    runs = gp.append_pulse(old, {"results": []}, NOW, {"t"})
    runs = gp.append_pulse(runs, new, NOW, {"t"})
    runs = gp.append_pulse(
        runs, {"results": [{"target": "t", "path": "proxy-d", "ms": 40}]}, NOW, {"t"}
    )
    paths = {row[1] for run in runs for row in run["r"]}
    assert len(paths) == gp.MAX_DEVICE_PATHS
    assert {"proxy-c", "proxy-d", "direct"} <= paths


def test_aggregate_median_and_losses_over_1h_and_24h() -> None:
    runs = [
        _run(5 * 3600, [["t", "direct", None], ["t", "vpn", 90]]),
        _run(3 * 3600, [["t", "direct", None], ["t", "vpn", 80]]),
        _run(1800, [["t", "direct", 40], ["t", "vpn", 60]]),
        _run(600, [["t", "direct", 50], ["t", "vpn", None]]),
        _run(2 * 86400, [["t", "direct", 1]]),  # outside 24 h
    ]
    cells = gp.aggregate(runs, NOW)
    direct, vpn = cells[("t", "direct")], cells[("t", "vpn")]
    assert direct.median_1h == 45 and direct.loss_1h == 0
    assert direct.median_24h == 45 and direct.loss_24h == 0.5
    assert direct.samples_24h == 4
    assert vpn.median_1h == 60 and vpn.loss_1h == 0.5
    assert vpn.median_24h == 80 and vpn.loss_24h == 0.25


def test_best_path_lowest_median_under_20_percent_loss() -> None:
    rows: list[list[object]] = []
    runs = []
    for i in range(10):
        rows = [
            ["pubg", "direct", 46],
            ["pubg", "proxy-de", 63],
            ["wd", "direct", 30 if i else None],  # fast but 1 loss in 10 -> 10 %: still fine
            ["wd", "proxy-de", 52],
            ["rbx", "direct", 20 if i % 2 else None],  # 50 % loss -> never best
            ["rbx", "proxy-de", 70],
        ]
        runs.append(_run(60 * i, rows))
    best = gp.best_paths(gp.aggregate(runs, NOW))
    assert best == {"pubg": "direct", "wd": "direct", "rbx": "proxy-de"}


def test_best_path_tie_goes_to_direct() -> None:
    runs = [_run(60, [["t", "cand-aa", 40], ["t", "direct", 40]])]
    assert gp.best_paths(gp.aggregate(runs, NOW)) == {"t": "direct"}


def test_pulse_card_rows_paths_and_verdict() -> None:
    targets = [
        {"id": "aws-fra", "title": "PUBG · Европа", "url": "https://a/", "games": "PUBG"},
        {"id": "aws-sto", "title": "WARDOGS", "url": "https://b/", "games": ""},
        {"id": "rbx-fra", "title": "Roblox", "url": "https://c/", "games": ""},
    ]
    runs = [
        _run(
            300 * i,
            [
                ["aws-fra", "direct", 46],
                ["aws-fra", "proxy-de", 63],
                ["aws-sto", "direct", None],
                ["aws-sto", "proxy-de", 52],
            ],
        )
        for i in range(6)
    ]
    card = gp.pulse_card(runs, targets, {"proxy-de": "Германия"}, NOW)
    assert card["paths"] == [
        {"id": "direct", "title": "Напрямую"},
        {"id": "proxy-de", "title": "Германия"},
    ]
    assert [r["target"] for r in card["rows"]] == ["aws-fra", "aws-sto", "rbx-fra"]
    assert card["rows"][0]["best"] == "direct"
    assert card["rows"][0]["cells"]["direct"]["median_1h"] == 46
    assert card["rows"][1]["best"] == "proxy-de"
    assert card["rows"][2]["cells"] == {}
    assert card["verdict"] == (
        "PUBG · Европа быстрее напрямую (на 17 мс); WARDOGS — через «Германия» (напрямую связи нет)"
    )
    assert card["last_at"] == int(NOW)


def test_pulse_card_without_data() -> None:
    card = gp.pulse_card([], gp.default_targets(), {}, NOW)
    assert card["paths"] == []
    assert card["verdict"] == ""
    assert card["last_at"] is None
