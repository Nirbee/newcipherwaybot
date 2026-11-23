"""DevVersion: upload decoding (gzip bomb), report checks, the digest, summaries, settings and the
offline provider lookup / download (no network: httpx is mocked, the .mmdb check is stubbed)."""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from src.application.services import devversion as dv
from src.core import config_registry as registry
from src.infrastructure.services import geoip


def _report(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": 1,
        "consent_version": "1",
        "install_id": str(uuid.uuid4()),
        "app_version": "0.5.0-dev",
        "os": {"build": "19045", "edition": "Pro"},
        "mode": "tun",
        "period": {"from": "2026-11-23T10:00:00Z", "to": "2026-11-23T11:00:00Z"},
        "system": {},
        "apps": [],
        "probes": [],
        "nodes": [],
        "events": [],
        "game_tests": [],
    }
    body.update(overrides)
    return body


def _raw(body: Any) -> bytes:
    return json.dumps(body).encode()


# --- decoding ---------------------------------------------------------------------------------
def test_decode_identity_and_gzip() -> None:
    raw = _raw(_report())
    assert dv.decode_body(raw, "") == raw
    assert dv.decode_body(raw, "identity") == raw
    assert dv.decode_body(gzip.compress(raw), "GZip") == raw


def test_gzip_bomb_is_cut_at_the_output_cap() -> None:
    bomb = gzip.compress(b'{"a":"' + b"0" * (50 * 1024 * 1024) + b'"}')
    assert len(bomb) < dv.MAX_REPORT_BYTES  # small on the wire
    with pytest.raises(dv.ReportError) as err:
        dv.decode_body(bomb, "gzip")
    assert (err.value.status, err.value.code) == (413, "report_too_large")
    # exactly at the cap still passes
    exact = b"0" * dv.MAX_JSON_BYTES
    assert dv.decode_body(gzip.compress(exact), "gzip") == exact


@pytest.mark.parametrize(
    ("body", "encoding", "status", "code"),
    [
        (b"not gzip", "gzip", 400, "bad_gzip"),
        (gzip.compress(b"{}")[:-6], "gzip", 400, "bad_gzip"),  # truncated
        (gzip.compress(b"{}") + gzip.compress(b"{}"), "gzip", 400, "bad_gzip"),  # 2 members
        (b"{}", "br", 415, "unsupported_encoding"),
        (b" " * (dv.MAX_JSON_BYTES + 1), "", 413, "report_too_large"),
    ],
    ids=["not-gzip", "truncated", "two-members", "brotli", "too-large"],
)
def test_decode_rejects(body: bytes, encoding: str, status: int, code: str) -> None:
    with pytest.raises(dv.ReportError) as err:
        dv.decode_body(body, encoding)
    assert (err.value.status, err.value.code) == (status, code)


# --- report checks ------------------------------------------------------------------------------
def test_valid_report_is_kept_whole() -> None:
    body = _report(install_id="{12345678-1234-4234-8234-1234567890AB}", future_section={"x": 1})
    report = dv.parse_report(_raw(body))
    assert report.install_id == "12345678-1234-4234-8234-1234567890ab"
    assert (report.schema, report.consent_version, report.app_version) == (1, "1", "0.5.0-dev")
    assert report.payload["future_section"] == {"x": 1}  # unknown sections are stored as sent
    assert report.size == len(_raw(body))


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"schema": 2}, "unsupported_schema"),
        ({"schema": "1"}, "unsupported_schema"),
        ({"schema": True}, "unsupported_schema"),
        ({"consent_version": "2"}, "consent_version"),
        ({"consent_version": None}, "consent_version"),
        ({"install_id": "not-a-uuid"}, "install_id"),
        ({"install_id": 5}, "install_id"),
        ({"app_version": ""}, "app_version"),
        ({"app_version": "1.0 <script>"}, "app_version"),
        ({"app_version": "1" * 33}, "app_version"),
        ({"mode": "vpn"}, "mode"),
        ({"apps": {}}, "apps_not_list"),
        ({"game_tests": "x"}, "game_tests_not_list"),
        ({"system": []}, "system_not_object"),
        ({"os": "Windows"}, "os_not_object"),
        ({"events": [{"line": "x" * (dv.MAX_STR_LEN + 1)}]}, "string_too_long"),
        ({"events": [{"line": "a\x00b"}]}, "bad_string"),
        ({"events": [{"a\x00": 1}]}, "bad_key"),
        ({"events": [{"k" * 129: 1}]}, "bad_key"),
        ({"events": ["\ud800"]}, "bad_string"),
        ({"events": [0] * (dv.MAX_LIST_ITEMS + 1)}, "list_too_long"),
        ({"system": {f"k{i}": 1 for i in range(dv.MAX_OBJECT_KEYS + 1)}}, "too_many_keys"),
        ({"events": [2**63]}, "bad_number"),
        ({f"x{i}": 1 for i in range(dv.MAX_TOP_KEYS)}, "too_many_sections"),
    ],
    ids=lambda v: v if isinstance(v, str) else "case",
)
def test_report_rejects(overrides: dict[str, Any], code: str) -> None:
    with pytest.raises(dv.ReportError) as err:
        dv.parse_report(_raw(_report(**overrides)))
    assert (err.value.status, err.value.code) == (422, code)


def test_depth_values_and_numbers() -> None:
    deep: Any = 1
    for _ in range(dv.MAX_DEPTH):
        deep = [deep]
    with pytest.raises(dv.ReportError, match="too_deep"):
        dv.parse_report(_raw(_report(system={"x": deep})))
    # nesting too deep for the JSON parser itself is a clean 400, not a crash
    with pytest.raises(dv.ReportError, match="invalid_json"):
        dv.parse_report(b'{"a":' + b"[" * 100_000 + b"]" * 100_000 + b"}")
    for raw in (b'{"schema": NaN}', b'{"x": Infinity}', b"[1", b'{"a": "\xff"}'):
        with pytest.raises(dv.ReportError, match="invalid_json"):
            dv.parse_report(raw)
    with pytest.raises(dv.ReportError, match="bad_number"):
        dv.parse_report(_raw(_report()).replace(b'"events": []', b'"events": [1e400]'))
    with pytest.raises(dv.ReportError, match="invalid_report"):
        dv.parse_report(b"[1, 2]")
    many = [[0] * 10_000 for _ in range(21)]  # 210k values in 10k-item lists
    with pytest.raises(dv.ReportError, match="too_many_values"):
        dv.parse_report(_raw(_report(events=many)))


# --- digest and summaries -----------------------------------------------------------------------
def _sample() -> dv.Report:
    return dv.parse_report(
        _raw(
            _report(
                nodes=[
                    {"node": "Германия", "protocol": "vless-reality", "latency_ms": 40, "ok": True},
                    {"tile": "Германия", "proto": "vless-reality", "delay_ms": 60, "loss": 2},
                    {"node": "Германия", "protocol": "vless-reality", "ok": False},
                    {"node": "Польша", "protocol": "hysteria2", "latency_ms": 55, "failures": 3},
                    "garbage",
                ],
                probes=[
                    {"target": "https://www.youtube.com/x", "route": "direct", "ok": False},
                    {"target": "discord.com", "route": "direct", "failures": 2, "app": "Discord"},
                    {"target": "discord.com", "route": "vpn", "ok": False},  # via VPN: not direct
                    {"target": "x.com", "route": "vpn", "node": "Польша", "latency_ms": 70},
                ],
                apps=[
                    {
                        "name": "Discord",
                        "direct_failures": 4,
                        "domains": [{"domain": "Gateway.Discord.gg", "direct_failures": 4}],
                    },
                    {"name": "Steam", "direct_failures": 0},
                ],
                game_tests=[
                    {"game": "PUBG", "route": "vpn", "ping_ms": 80, "loss_pct": 1, "jitter_ms": 5},
                    {"game": "PUBG", "route": "direct", "ok": False},
                ],
            )
        )
    )


def test_digest_reads_known_fields_with_aliases() -> None:
    digest = dv.build_digest(_sample())
    assert digest["mode"] == "tun"
    assert digest["counts"] == {"apps": 2, "probes": 4, "nodes": 5, "events": 0, "game_tests": 2}
    assert digest["links"] == [
        ["Германия", "vless-reality", 40.0, None, 0],
        ["Германия", "vless-reality", 60.0, 2.0, 0],
        ["Германия", "vless-reality", None, None, 1],
        ["Польша", "hysteria2", 55.0, None, 3],
        ["Польша", None, 70.0, None, 0],
    ]
    assert sorted(map(tuple, digest["direct"])) == [
        ("app", "Discord", 6),
        ("domain", "discord.com", 2),
        ("domain", "gateway.discord.gg", 4),
        ("domain", "www.youtube.com", 1),
    ]
    assert digest["games"] == [
        ["PUBG", "vpn", 80.0, 1.0, 5.0, 0],
        ["PUBG", "direct", None, None, None, 1],
    ]


def test_digest_clamps_values_and_survives_broken_urls() -> None:
    report = dv.parse_report(
        _raw(
            _report(
                nodes=[
                    {"node": "N" * 200, "latency_ms": 10**9, "loss_pct": 250, "failures": 10**6},
                    {"node": "x", "latency_ms": -5, "loss_pct": -3, "failures": -2},
                ],
                probes=[
                    {"target": "https://[broken", "route": "direct", "failures": 5},
                    {"target": "a.example", "route": "direct", "failures": 10**6},
                ],
                apps=[{"name": "App", "direct_failures": 10**6}],
                game_tests=[{"game": "G", "latency_ms": 70_000, "jitter_ms": 99_999}],
            )
        )
    )
    digest = dv.build_digest(report)
    assert digest["links"] == [["N" * 64, None, 60000.0, 100.0, 1000], ["x", None, None, 0.0, 0]]
    assert sorted(map(tuple, digest["direct"])) == [
        ("app", "App", 1000),
        ("domain", "a.example", 1000),
        ("domain", "https://[broken", 5),
    ]
    assert digest["games"] == [["G", None, 60000.0, None, 60000.0, 0]]
    # per-report caps on the digest itself
    many = _report(nodes=[{"node": f"n{i}"} for i in range(500)])
    many["probes"] = [
        {"target": f"d{i}.example", "route": "direct", "ok": False} for i in range(500)
    ]
    many["game_tests"] = [{"game": f"g{i}"} for i in range(500)]
    big = dv.build_digest(dv.parse_report(_raw(many)))
    assert (len(big["links"]), len(big["direct"]), len(big["games"])) == (50, 100, 100)


def test_summary_groups_by_provider() -> None:
    digest = dv.build_digest(_sample())
    other = dv.build_digest(
        dv.parse_report(
            _raw(_report(nodes=[{"node": "Германия", "protocol": "vless-reality", "rtt_ms": 100}]))
        )
    )
    rows = [
        dv.DigestRow(1, 501, 12389, "Rostelecom", digest),
        dv.DigestRow(2, 502, 12389, None, other),
        dv.DigestRow(3, 503, None, None, other),
        dv.DigestRow(4, 504, 8359, "MTS", {"links": [["bad"]], "direct": [["app", "x", "n"]]}),
    ]
    out = dv.summarize(rows)
    assert (out["reports"], out["truncated"]) == (4, False)
    de = [r for r in out["links"] if r["asn"] == 12389 and r["node"] == "Германия"]
    assert de == [
        {
            "asn": 12389,
            "as_org": "Rostelecom",
            "node": "Германия",
            "protocol": "vless-reality",
            "subscriptions": 2,
            "reports": 2,
            "samples": 4,
            "failures": 1,
            "latency_median": 60.0,
            "loss_median": 2.0,
            "single_source": False,
        }
    ]
    assert out["links"][0] == de[0]  # the row most subscriptions saw comes first
    assert all(r["single_source"] for r in out["links"][1:])
    assert out["direct_failures"][0] == {
        "kind": "app",
        "name": "Discord",
        "subscriptions": 1,
        "providers": 1,
        "reports": 1,
        "failures": 6,
        "single_source": True,
    }
    pubg = next(g for g in out["games"] if g["route"] == "vpn")
    assert (pubg["latency_median"], pubg["jitter_median"], pubg["failures"]) == (80.0, 5.0, 0)


def test_summary_ranks_by_distinct_subscriptions_not_raw_sums() -> None:
    """One noisy subscription with huge numbers can't top the list or drag a median."""
    noisy = {
        "direct": [["domain", "noisy.example", 1000]],
        "links": [["DE", "vless", 60000.0, 100.0, 1000]] * 150,
    }
    quiet = {"direct": [["domain", "real.example", 1]], "links": [["DE", "vless", 40.0, 0.0, 0]]}
    rows = [dv.DigestRow(i, 1, 1, "ISP", noisy) for i in range(50)]  # 50 reports, ONE sub
    rows += [dv.DigestRow(100 + i, 10 + i, 1 + i % 3, "ISP", quiet) for i in range(3)]
    out = dv.summarize(rows)
    top, second = out["direct_failures"][:2]
    assert (top["name"], top["subscriptions"], top["providers"]) == ("real.example", 3, 3)
    assert (second["name"], second["subscriptions"], second["reports"]) == ("noisy.example", 1, 50)
    assert second["failures"] == dv.SUB_FAILURES_CAP  # 50 x 1000 capped per subscription
    link = next(r for r in out["links"] if r["asn"] == 1)
    # the noisy subscription adds at most SUB_SAMPLES_CAP samples, not 50 x 150
    assert link["samples"] == dv.SUB_SAMPLES_CAP + 1 and link["subscriptions"] == 2
    assert (top["single_source"], second["single_source"]) == (False, True)
    # the cell ceiling stops the walk and says so
    capped = dv.summarize(rows, max_cells=500)
    assert capped["truncated"] is True and capped["reports"] == 3


def test_summary_caps_groups_per_subscription_and_cuts_after_ranking() -> None:
    def links(names: range) -> dict[str, Any]:
        return {"links": [[f"n{i}", "p", 10.0, 0.0, 0] for i in names]}

    # one subscription inventing node names: at most SUB_GROUPS_CAP groups (nodes + games)
    flood = [
        dv.DigestRow(1, 7, 1, None, links(range(30))),
        dv.DigestRow(2, 7, 1, None, links(range(30, 60))),
    ]
    flood.append(dv.DigestRow(3, 7, 1, None, {"games": [["G", "vpn", 1.0, 0.0, 0.0, 0]]}))
    out = dv.summarize(flood)
    assert len(out["links"]) + len(out["games"]) == dv.SUB_GROUPS_CAP == 50
    # a group several subscriptions saw survives the cut even when it sorts last by name/ASN
    rows = [dv.DigestRow(i, 100 + i, 1, None, links(range(i * 10, i * 10 + 10))) for i in range(5)]
    shared = {"links": [["zz-real", "p", 30.0, 0.0, 0]]}
    rows += [dv.DigestRow(50 + i, 200 + i, 99999, "Late ISP", shared) for i in range(3)]
    cut = dv.summarize(rows, top=1)  # top=1 keeps 5 link rows
    assert len(cut["links"]) == 5
    assert (cut["links"][0]["node"], cut["links"][0]["subscriptions"]) == ("zz-real", 3)
    assert cut["links"][0]["single_source"] is False


# --- settings -----------------------------------------------------------------------------------
def test_settings_defaults_and_guards() -> None:
    snap = {s.key: s.default for s in registry.all_specs()}
    cfg = dv.config_from(snap)
    assert cfg.public() == {
        "enabled": True,
        "reason": None,
        "consent_version": "1",
        "upload_interval_s": 3600,
        "max_report_bytes": 512 * 1024,
        "flags": {},
    }
    bad = {**snap, "DEVVERSION_UPLOAD_INTERVAL": 5, "DEVVERSION_FLAGS": "[1]"}
    assert (dv.config_from(bad).upload_interval_s, dv.config_from(bad).flags) == (600, {})
    assert dv.check_interval("900") == 900
    for raw in (599, 86401, "x", None):
        with pytest.raises(ValueError, match="DEVVERSION_UPLOAD_INTERVAL"):
            dv.check_interval(raw)
    changes: dict[str, Any] = {
        "DEVVERSION_FLAGS": {"a": 1},
        "DEVVERSION_UPLOAD_INTERVAL": "1200",
        "DEVVERSION_ALLOWED_SUBSCRIPTIONS": "7, 3 7;12",
    }
    dv.settings_guard(changes)
    assert changes == {
        "DEVVERSION_FLAGS": '{"a":1}',
        "DEVVERSION_UPLOAD_INTERVAL": 1200,
        "DEVVERSION_ALLOWED_SUBSCRIPTIONS": "3,7,12",
    }


def test_flags_are_a_flat_bounded_object() -> None:
    assert dv.clean_flags('{"a": true}') == '{"a":true}'
    assert dv.clean_flags({"new_probe_2": 1, "mode": "x" * 64}) == (
        '{"new_probe_2":1,"mode":"' + "x" * 64 + '"}'
    )
    for raw in (
        "[]",
        "nope",
        5,
        {"x": "y" * 65},
        {"x": {"nested": 1}},
        {"x": [1]},
        {"x": None},
        {"x": 1.5},
        {"Bad": True},
        {"has-dash": True},
        {"k" * 33: True},
        {f"f{i}": True for i in range(51)},
    ):
        with pytest.raises(ValueError, match="DEVVERSION_FLAGS"):
            dv.clean_flags(raw)
    # a bad stored value never reaches the app as is
    assert dv.parse_flags('{"ok": true, "Bad": 1, "n": {"x": 1}}') == {"ok": True}


def test_allow_list_and_refusals() -> None:
    assert dv.parse_allowed("5, 6;7 x -1 0") == frozenset({5, 6, 7})
    assert dv.clean_allowed([3, "1"]) == "1,3"
    assert dv.clean_allowed("") == ""
    for raw in ("1, x", "-5", [0]):
        with pytest.raises(ValueError, match="DEVVERSION_ALLOWED_SUBSCRIPTIONS"):
            dv.clean_allowed(raw)
    open_cfg = dv.DevVersionConfig(True, 3600, {"a": True})
    listed = dv.DevVersionConfig(True, 3600, {"a": True}, allowed=frozenset({5}))
    assert open_cfg.refusal(9, trial=False) is None
    assert open_cfg.refusal(9, trial=True) == "trial_not_allowed"
    assert listed.refusal(5, trial=False) is None
    assert listed.refusal(9, trial=False) == "not_allowed"
    assert listed.public("not_allowed")["enabled"] is False
    assert listed.public("not_allowed")["flags"] == {}
    off = dv.DevVersionConfig(False, 3600, {})
    assert (off.public()["enabled"], off.public()["reason"]) == (False, "disabled")


# --- offline provider lookup ------------------------------------------------------------------
class _Meta:
    def __init__(self, database_type: str) -> None:
        self.database_type = database_type


class _Reader:
    def __init__(self, records: dict[str, Any], database_type: str = "") -> None:
        self.records = records
        self.asked: list[str] = []
        self.database_type = database_type

    def get(self, ip: str) -> Any:
        self.asked.append(ip)
        return self.records.get(ip)

    def metadata(self) -> _Meta:
        return _Meta(self.database_type)

    def close(self) -> None: ...


def _fake_open(opened: list[tuple[str, int]], *, wrong_type: bool = False) -> Any:
    def fake_open(path: str, mode: int = 0) -> _Reader:
        name = Path(path).name
        opened.append((name, mode))
        kind = "ASN" if "asn" in name else "City"
        if wrong_type:
            kind = "City" if kind == "ASN" else "ASN"
        return _Reader({"1.1.1.1": {"x": 1}}, f"DBIP-{kind}-Lite (compat=GeoLite2-{kind})")

    return fake_open


def test_lookup_without_databases_is_empty(tmp_path: Path) -> None:
    assert geoip.lookup(str(tmp_path), "8.8.8.8") == geoip.GeoInfo()
    assert geoip.status(str(tmp_path)) == {"asn": None, "city": None}


def test_lookup_reads_asn_and_city(tmp_path: Path) -> None:
    geo = geoip.GeoIp(str(tmp_path))
    asn = _Reader(
        {
            "95.24.1.1": {
                "autonomous_system_number": 8402,
                "autonomous_system_organization": "Beeline",
            }
        }
    )
    city = _Reader(
        {
            "95.24.1.1": {
                "country": {"iso_code": "RU"},
                "subdivisions": [{"names": {"en": "Moscow", "ru": "Москва"}}],
                "city": {"names": {"en": "Moscow"}},
            }
        }
    )
    for db, reader in ((geo.asn, asn), (geo.city, city)):
        db.reader, db.checked = reader, time.monotonic() + 3600  # skip the mtime check
    assert geo.lookup("::ffff:95.24.1.1") == geoip.GeoInfo(
        asn=8402, as_org="Beeline", country="RU", region="Москва", city="Moscow"
    )
    # private / garbage addresses are never looked up
    for ip in ("10.0.0.1", "127.0.0.1", "192.168.1.5", "fe80::1", "", "nope"):
        assert geo.lookup(ip) == geoip.GeoInfo()
    assert asn.asked == ["95.24.1.1"]


def _gz_server(router: respx.MockRouter, months: dict[str, bytes | int]) -> None:
    for name in ("dbip-asn-lite", "dbip-city-lite"):
        for month, body in months.items():
            url = f"https://download.db-ip.com/free/{name}-{month}.mmdb.gz"
            if isinstance(body, int):
                router.get(url).mock(return_value=httpx.Response(body))
            else:
                router.get(url).mock(return_value=httpx.Response(200, content=body))


async def test_refresh_falls_back_to_last_month_and_swaps_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(geoip.maxminddb, "open_database", _fake_open(opened))
    now = dt.datetime(2026, 12, 2, tzinfo=dt.UTC)
    with respx.mock(assert_all_called=False) as router:
        _gz_server(router, {"2026-12": 404, "2026-11": gzip.compress(b"MMDB")})
        done = await geoip.refresh(str(tmp_path), now=now)
    assert done == {geoip.ASN_FILE: "2026-11", geoip.CITY_FILE: "2026-11"}
    assert (tmp_path / geoip.ASN_FILE).read_bytes() == b"MMDB"
    # checked (type + a probe lookup) before the swap, memory-mapped
    assert opened == [
        (f"{geoip.ASN_FILE}.tmp", geoip.maxminddb.MODE_MMAP_EXT),
        (f"{geoip.CITY_FILE}.tmp", geoip.maxminddb.MODE_MMAP_EXT),
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == [geoip.ASN_FILE, geoip.CITY_FILE]
    # fresh files → nothing downloaded
    with respx.mock(assert_all_called=False) as router:
        assert await geoip.refresh(str(tmp_path), now=now) == {}
        assert not router.calls


async def test_refresh_rejects_a_database_of_the_wrong_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geoip.maxminddb, "open_database", _fake_open([], wrong_type=True))
    now = dt.datetime(2026, 12, 2, tzinfo=dt.UTC)
    with respx.mock(assert_all_called=False) as router:
        _gz_server(router, {"2026-12": gzip.compress(b"MMDB")})
        done = await geoip.refresh(str(tmp_path), now=now)
    assert done == {geoip.ASN_FILE: "error", geoip.CITY_FILE: "error"}
    assert list(tmp_path.iterdir()) == []


async def test_refresh_follows_redirects_only_to_db_ip_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geoip.maxminddb, "open_database", _fake_open([]))
    now = dt.datetime(2026, 12, 2, tzinfo=dt.UTC)
    base = "https://download.db-ip.com/free"
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{base}/dbip-asn-lite-2026-12.mmdb.gz").mock(
            return_value=httpx.Response(302, headers={"Location": "https://cdn.db-ip.com/a.gz"})
        )
        router.get("https://cdn.db-ip.com/a.gz").mock(
            return_value=httpx.Response(200, content=gzip.compress(b"MMDB"))
        )
        router.get(f"{base}/dbip-city-lite-2026-12.mmdb.gz").mock(
            return_value=httpx.Response(302, headers={"Location": "http://cdn.db-ip.com/c.gz"})
        )
        evil = router.get("https://evil.example/c.gz").mock(return_value=httpx.Response(200))
        done = await geoip.refresh(str(tmp_path), now=now)
        assert done == {geoip.ASN_FILE: "2026-12", geoip.CITY_FILE: "error"}  # http:// refused
        router.get(f"{base}/dbip-city-lite-2026-12.mmdb.gz").mock(
            return_value=httpx.Response(302, headers={"Location": "https://evil.example/c.gz"})
        )
        done = await geoip.refresh(str(tmp_path), now=now)
        assert done == {geoip.CITY_FILE: "error"}
        assert not evil.called
    assert geoip._allowed_url("https://db-ip.com/x") and geoip._allowed_url(
        "https://download.db-ip.com/x"
    )
    for bad in ("https://db-ip.com.evil.example/x", "https://notdb-ip.com/x", "ftp://db-ip.com/"):
        assert not geoip._allowed_url(bad)


async def test_refresh_has_one_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow_fetch(client: Any, url: str, dest: Path) -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr(geoip, "_TOTAL_TIMEOUT", 0.05)
    monkeypatch.setattr(geoip, "_fetch", slow_fetch)
    done = await geoip.refresh(str(tmp_path), now=dt.datetime(2026, 12, 2, tzinfo=dt.UTC))
    assert done == {geoip.ASN_FILE: "timeout", geoip.CITY_FILE: "timeout"}


def test_install_inflates_in_bounded_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geoip.maxminddb, "open_database", _fake_open([]))
    monkeypatch.setattr(geoip, "_INFLATE_STEP", 1000)  # many decompress() calls per chunk
    data = bytes(range(256)) * 200 + b"0" * 50_000
    gz = tmp_path / "db.gz"
    gz.write_bytes(gzip.compress(data))
    target = tmp_path / geoip.ASN_FILE
    geoip._install(gz, target, "asn")
    assert target.read_bytes() == data
    # past the size cap it stops mid-stream and leaves nothing behind
    monkeypatch.setattr(geoip, "_MAX_MMDB_BYTES", 10_000)
    other = tmp_path / geoip.CITY_FILE
    with pytest.raises(ValueError, match="too large"):
        geoip._install(gz, other, "city")
    assert not other.exists() and not (tmp_path / f"{geoip.CITY_FILE}.tmp").exists()


async def test_refresh_keeps_the_old_file_on_a_broken_download(tmp_path: Path) -> None:
    old = tmp_path / geoip.ASN_FILE
    old.write_bytes(b"OLD")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))
    now = dt.datetime.now(dt.UTC)
    this, prev = geoip._months(now)
    with respx.mock(assert_all_called=False) as router:
        _gz_server(router, {this: b"not gzip at all", prev: 404})
        done = await geoip.refresh(str(tmp_path), now=now)
    assert done == {geoip.ASN_FILE: "error", geoip.CITY_FILE: "error"}
    assert old.read_bytes() == b"OLD"
    assert sorted(p.name for p in tmp_path.iterdir()) == [geoip.ASN_FILE]  # no leftovers


async def test_refresh_if_due_is_switchable_and_throttled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_refresh(directory: str, *, now: dt.datetime | None = None) -> dict[str, str]:
        calls.append(directory)
        return {"x": "y"}

    class _Redis:
        def __init__(self) -> None:
            self.keys: set[str] = set()

        async def set(self, key: str, value: str, nx: bool = False, ex: int = 0) -> bool:
            if nx and key in self.keys:
                return False
            self.keys.add(key)
            return True

    class _Web:
        geoip_dir = str(tmp_path)
        geoip_download = True

    class _Container:
        redis = _Redis()

        class settings:
            web = _Web

    monkeypatch.setattr(geoip, "refresh", fake_refresh)
    container: Any = _Container()
    assert await geoip.refresh_if_due(container) == {"x": "y"}
    assert await geoip.refresh_if_due(container) == {}  # one attempt per 6 hours
    _Web.geoip_download = False
    container.redis.keys.clear()
    assert await geoip.refresh_if_due(container) == {}
    assert calls == [str(tmp_path)]


# --- backups --------------------------------------------------------------------------------
async def test_backups_skip_devversion_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both dumps — the scheduled encrypted backup and update.sh's pre-update one — keep the
    table's schema but not its rows."""
    from src.infrastructure.taskiq import tasks

    argv: list[str] = []

    async def fake_exec(*args: str, **kwargs: Any) -> Any:
        argv.extend(args)
        raise FileNotFoundError("pg_dump")

    class _Db:
        host, port, user, name, password = "db", 5432, "vpn", "vpn", "secret"

    class _Container:
        class settings:
            database = _Db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "get_container", lambda: _Container())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert await tasks.run_backup() is None
    assert argv[0] == "pg_dump" and "--exclude-table-data=devversion_reports" in argv
    update = (Path(__file__).resolve().parents[2] / "scripts/update.sh").read_text(encoding="utf-8")
    dumps = [line for line in update.splitlines() if "pg_dump" in line and "exec" in line]
    assert dumps and all("--exclude-table-data=devversion_reports" in line for line in dumps)
