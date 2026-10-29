#!/bin/sh
# CipherWay router agent — keeps a Keenetic router's Xray (XKeen) config in sync with the bot.
#
# Runs on the router (Entware/busybox ash — POSIX sh only). Invoked from cron every 5 minutes
# by the installer (install.sh); safe to run by hand. Talks to src/web/routes/agent.py:
#   GET  /api/agent/config     -> Xray config fragment (outbounds/routing/dns) for XKeen's
#                                  confdir; X-Agent-Version header advertises the current agent
#   GET  /api/agent/agent.sh   -> this script (self-update)
#   GET  /api/agent/pulse      -> game pulse settings (v7, off unless enabled for this router)
#   POST /api/agent/heartbeat  -> status + diagnostics shown in the admin panel
#
# What leaves the router: the agent's own state (versions, config files, DNS of one test
# domain), results of its own test connections (self-test, game pulse: ms / failed), the
# times of its own actions (config applied, watchdog, self-update) and, for diagnostics, the
# last 15 lines of Xray's error log. That tail is the one place clients' activity can show
# up: Xray logs connections it failed to make, so it may name a site or address a device
# tried to reach (LAN addresses are masked as «LAN» before sending). The agent doesn't
# capture traffic, read conntrack or DNS queries, or list the devices on the network.
#
# Safety rules:
# - 503 from /config = panel briefly unreachable, NOT "no config": keep what's applied.
# - A new config is only kept if `xray run -test` accepts it and xray comes back up after the
#   restart; otherwise the previous file is restored. A bad push must never cost the customer
#   their internet.

set -eu

AGENT_VERSION="7"

CONF_FILE="${CIPHERWAY_AGENT_CONF:-/opt/etc/cipherway-agent/agent.conf}"
SELF="/opt/etc/cipherway-agent/agent.sh"
STATE_DIR="/opt/var/lib/cipherway-agent"
LOG_FILE="/opt/var/log/cipherway-agent.log"
LOCK_DIR="/opt/var/run/cipherway-agent.lock"
XRAY_ASSET_DIR="/opt/etc/xray/dat"
XKEEN_CFG_DIR="/opt/etc/xkeen"
LOG_MAX_BYTES=524288
DNS_PROBE_DOMAIN="4pda.to"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# Event journal: "<unix_ts> <kind> <note>", last 50 kept; the heartbeat carries the last 20 and
# the server drops repeats. Shows when Xray was restarted or the VPN switched off — players'
# drops line up against these. Never fails the run.
event() {
    ev_file="$STATE_DIR/events"
    {
        printf '%s %s %s\n' "$(date +%s)" "$1" \
            "$(printf '%s' "${2:-}" | tr '\t\r\n' '   ' | cut -c1-120)" >> "$ev_file"
        if [ "$(wc -l < "$ev_file")" -gt 50 ]; then
            tail -n 50 "$ev_file" > "$ev_file.tmp" && mv "$ev_file.tmp" "$ev_file"
        fi
    } 2>/dev/null || true
}

rotate_log_if_needed() {
    [ -f "$LOG_FILE" ] || return 0
    size="$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)"
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
        tail -n 500 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

acquire_lock() {
    mkdir -p "$(dirname "$LOCK_DIR")"
    # A lock older than 10 minutes belongs to a run that was killed mid-way (power cut).
    if [ -d "$LOCK_DIR" ] && [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log "another run is still in progress, skipping"
        exit 0
    fi
    trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM
}

require_bin() {
    command -v "$1" >/dev/null 2>&1 || {
        log "FATAL: '$1' not found — install it: opkg install $1"
        exit 1
    }
}

xray_bin() {
    command -v xray 2>/dev/null || echo "/opt/sbin/xray"
}

is_xray_running() {
    pidof xray >/dev/null 2>&1
}

xray_version() {
    bin="$(xray_bin)"
    [ -x "$bin" ] || return 0
    "$bin" version 2>/dev/null | head -n1 | sed 's/^Xray //'
}

xray_config_ok() {
    XRAY_LOCATION_ASSET="$XRAY_ASSET_DIR" "$(xray_bin)" run -test -confdir "$CONFDIR" \
        > "$STATE_DIR/xray_test.log" 2>&1
}

xray_test_summary() {
    grep -v '^[[:space:]]*$' "$STATE_DIR/xray_test.log" 2>/dev/null | tail -n 3 | tr '\n' ' ' \
        | cut -c1-400
}

restart_xray() {
    if command -v xkeen >/dev/null 2>&1; then
        xkeen -restart >> "$LOG_FILE" 2>&1 && return 0
    fi
    if [ -x /opt/etc/init.d/S05xkeen ]; then
        /opt/etc/init.d/S05xkeen restart >> "$LOG_FILE" 2>&1 && return 0
    fi
    log "WARN: could not restart xray (no xkeen / S05xkeen)"
    event xray_restart_failed "no xkeen / S05xkeen or restart failed"
    return 1
}

# XKeen ships inbounds with sniffing routeOnly:true, so a proxied connection still goes to the
# IP the client got from the ISP's resolver — for a blocked domain that's the ISP's stub page,
# and the app fails even through the VPN (browsers dodge it with their own DoH). routeOnly:false
# lets Xray dial the sniffed domain instead, resolved on the VPN server.
enforce_route_only_false() {
    [ "${FIX_ROUTE_ONLY:-1}" = "1" ] || return 0
    inb="$CONFDIR/03_inbounds.json"
    [ -f "$inb" ] || return 0
    grep -q '"routeOnly": *true' "$inb" || return 0
    cp "$inb" "$STATE_DIR/03_inbounds.json.bak"
    sed -i 's/"routeOnly": *true/"routeOnly": false/g' "$inb"
    if xray_config_ok; then
        log "baseline: set routeOnly=false in $inb"
        NEED_RESTART=1
    else
        cp "$STATE_DIR/03_inbounds.json.bak" "$inb"
        log "WARN: routeOnly change rejected by xray -test, reverted: $(xray_test_summary)"
    fi
}

# Returns 0 if the new config is now live, 1 if it was rejected and the old one restored.
apply_config() {
    new="$1"
    had_old=0
    if [ -f "$TARGET" ]; then
        cp "$TARGET" "$STATE_DIR/current.bak"
        had_old=1
    fi
    cp "$new" "$TARGET.tmp" && mv "$TARGET.tmp" "$TARGET"

    if ! xray_config_ok; then
        LAST_ERROR="server config rejected by xray -test, kept previous: $(xray_test_summary)"
        restore_previous "$had_old"
        return 1
    fi

    restart_xray || true
    sleep 5
    if ! is_xray_running; then
        LAST_ERROR="xray did not start with the new config, rolled back"
        restore_previous "$had_old"
        event rollback "xray did not start with the new config"
        restart_xray || true
        return 1
    fi
    NEED_RESTART=0
    return 0
}

restore_previous() {
    if [ "$1" = "1" ]; then
        cp "$STATE_DIR/current.bak" "$TARGET"
    else
        rm -f "$TARGET"
    fi
    log "ERROR: $LAST_ERROR"
}

self_update() {
    wanted="$1"
    [ -n "$wanted" ] && [ "$wanted" != "$AGENT_VERSION" ] || return 0
    tmp="$STATE_DIR/agent.sh.new"
    if ! curl -fsS -m 30 -o "$tmp" "$API_BASE/api/agent/agent.sh" 2>>"$LOG_FILE"; then
        log "WARN: self-update download failed"
        return 0
    fi
    if ! sh -n "$tmp" 2>>"$LOG_FILE" || ! grep -q "^AGENT_VERSION=\"$wanted\"" "$tmp"; then
        log "WARN: self-update to v$wanted rejected (syntax/version check failed)"
        rm -f "$tmp"
        return 0
    fi
    chmod +x "$tmp"
    mv "$tmp" "$SELF"
    log "self-updated v$AGENT_VERSION -> v$wanted (takes effect next run)"
    event self_update "v$AGENT_VERSION -> v$wanted"
}

# --- diagnostics -----------------------------------------------------------------------

file_entries() {
    [ -f "$1" ] || { echo "missing"; return; }
    grep -v '^[[:space:]]*#' "$1" | grep -v '^[[:space:]]*$' | tr '\n' ' ' | sed 's/ *$//'
}

route_only_state() {
    inb="$CONFDIR/03_inbounds.json"
    [ -f "$inb" ] || { echo "missing"; return; }
    grep -o '"routeOnly": *[a-z]*' "$inb" | sed 's/.*: *//' | sort -u | tr '\n' ' ' | sed 's/ *$//'
}

resolve() {
    nslookup "$1" ${2:+"$2"} 2>/dev/null \
        | awk '/^Name:/ {n=1; next} n && /Address/ {print $NF}' \
        | tr '\n' ' ' | sed 's/ *$//'
}

cron_state() {
    state="stopped"
    pidof crond >/dev/null 2>&1 && state="running"
    where=""
    for f in /opt/var/spool/cron/crontabs/root /opt/etc/crontabs/root; do
        grep -q "cipherway-agent" "$f" 2>/dev/null && where="$where $f"
    done
    echo "crond=$state entry:${where:- none}"
}

router_model() {
    command -v ndmc >/dev/null 2>&1 || return 0
    ndmc -c "show version" 2>/dev/null \
        | awk -F': *' '/^ *(model|title|release):/ {printf "%s ", $2}' | sed 's/ *$//'
}

xkeen_version() {
    grep -m1 '^xkeen_current_version=' /opt/sbin/.xkeen/01_info/01_info_variable.sh 2>/dev/null \
        | cut -d'"' -f2
}

# --- self-test -------------------------------------------------------------------------
# The server's config adds loopback SOCKS listeners (cwtest-*): one per VPN server and one
# through the balancer. Probing them shows, per server, whether traffic really passes and where
# it exits — a router can look "online" while every connection silently falls back to direct.
# The large download catches ISPs that freeze connections to foreign hosts after ~16 KB.

probe() {
    # $1 = name, $2 = socks port ("" = straight from the router, no VPN),
    # $3 = address-echo URL instead of our server, which also skips the big download. The
    #      clients'-path probe needs a foreign one: our .ru server is, correctly, reached
    #      direct by the split rules.
    p_name="$1"
    p_port="$2"
    p_url="${3:-$API_BASE/api/agent/ip}"
    p_proxy=""
    [ -n "$p_port" ] && p_proxy="--socks5-hostname 127.0.0.1:$p_port"
    p_ip=""
    p_err=""
    p_big=""
    # shellcheck disable=SC2086
    p_res="$(curl -sS -m 10 $p_proxy -H "Authorization: Bearer $TOKEN" \
        -o "$STATE_DIR/probe.out" -w '%{http_code} %{time_total}' \
        "$p_url" 2>"$STATE_DIR/probe.err")" || true
    p_code="${p_res%% *}"
    p_secs="${p_res#* }"
    if [ "$p_code" = "200" ] && [ -n "${3:-}" ]; then
        p_ip="$(head -c 64 "$STATE_DIR/probe.out" | tr -d '\r\n ')"
    elif [ "$p_code" = "200" ]; then
        p_ip="$(head -c 64 "$STATE_DIR/probe.out" | tr -d '\r\n ')"
        # shellcheck disable=SC2086
        p_bres="$(curl -sS -m 20 $p_proxy -H "Authorization: Bearer $TOKEN" \
            -o /dev/null -w '%{http_code} %{size_download}' \
            "$API_BASE/api/agent/probe" 2>"$STATE_DIR/probe.err")" || true
        p_bytes="${p_bres#* }"
        if [ "${p_bres%% *}" = "200" ] && [ "${p_bytes%.*}" -ge 200000 ] 2>/dev/null; then
            p_big="ok"
        else
            p_big="оборвалось на ${p_bytes:-0} байт: $(head -c 160 "$STATE_DIR/probe.err" | tr '\r\n' '  ')"
        fi
    else
        p_err="$(head -c 200 "$STATE_DIR/probe.err" | tr '\r\n' '  ')"
        [ -n "$p_err" ] || p_err="HTTP ${p_code:-нет ответа}"
        p_secs=""
    fi
    jq -n --arg name "$p_name" --arg ip "$p_ip" --arg secs "$p_secs" --arg big "$p_big" \
        --arg error "$p_err" --arg code "$p_code" \
        '{name: $name, ok: ($code == "200"), ip: $ip, secs: $secs, big: $big, error: $error}'
}

self_test() {
    [ -f "$TARGET" ] && is_xray_running || { echo '[]'; return 0; }
    {
        probe direct ""
        jq -r '.inbounds[]? | select(.tag | startswith("cwtest-")) | "\(.tag) \(.port)"' \
            "$TARGET" 2>/dev/null | while read -r t_tag t_port; do
            if [ "$t_tag" = "cwtest-routed" ]; then
                probe routed "$t_port" "https://api.ipify.org"
            elif [ "${t_tag#cwtest-cand-}" != "$t_tag" ]; then
                # Candidate server (AUTO routers): light probe only — the server just needs to
                # know it passes and how fast; no 256 KB download per candidate every 5 min.
                probe "${t_tag#cwtest-}" "$t_port" "$API_BASE/api/agent/ip"
            else
                probe "${t_tag#cwtest-}" "$t_port"
            fi
        done
    } | jq -s '.'
}

xray_error_tail() {
    # sed, not jq: XKeen's JSON files may carry comments, which jq rejects.
    f="$(sed -n 's/.*"error"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$CONFDIR/01_log.json" 2>/dev/null | head -n1)" || true
    [ -n "$f" ] || f="/opt/var/log/xray/error.log"
    [ -f "$f" ] || return 0
    # LAN addresses (which device on the customer's network) are masked: the server only
    # needs the error, not who hit it.
    tail -n 15 "$f" 2>/dev/null \
        | sed -E \
            -e 's/(^|[^0-9.])192\.168\.[0-9]{1,3}\.[0-9]{1,3}(:[0-9]+)?/\1LAN/g' \
            -e 's/(^|[^0-9.])10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(:[0-9]+)?/\1LAN/g' \
            -e 's/(^|[^0-9.])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}(:[0-9]+)?/\1LAN/g' \
        | cut -c1-220 | tail -c 2500
}

build_diagnostics() {
    tests="$(self_test 2>/dev/null)" || tests='[]'
    printf '%s' "$tests" | jq empty >/dev/null 2>&1 || tests='[]'
    jq -n \
        --argjson self_test "$tests" \
        --arg xray_errors "$(xray_error_tail)" \
        --arg agent_version "$AGENT_VERSION" \
        --arg xkeen_version "$(xkeen_version)" \
        --arg router "$(router_model)" \
        --arg route_only "$(route_only_state)" \
        --arg ports_proxied "$(file_entries "$XKEEN_CFG_DIR/port_proxying.lst")" \
        --arg ports_excluded "$(file_entries "$XKEEN_CFG_DIR/port_exclude.lst")" \
        --arg confdir_files "$(ls "$CONFDIR" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//')" \
        --arg cron "$(cron_state)" \
        --arg dns_probe "$DNS_PROBE_DOMAIN" \
        --arg dns_router "$(resolve "$DNS_PROBE_DOMAIN")" \
        --arg dns_1111 "$(resolve "$DNS_PROBE_DOMAIN" 1.1.1.1)" \
        --arg opt_free "$(df -h /opt 2>/dev/null | awk 'NR==2 {print $4}')" \
        --arg xray_test "$(xray_test_summary)" \
        '{agent_version: $agent_version, xkeen_version: $xkeen_version, router: $router,
          route_only: $route_only, ports_proxied: $ports_proxied,
          ports_excluded: $ports_excluded, confdir_files: $confdir_files, cron: $cron,
          dns_probe: $dns_probe, dns_router: $dns_router, dns_1111: $dns_1111,
          opt_free: $opt_free, xray_test: $xray_test, self_test: $self_test,
          xray_errors: $xray_errors}'
}

events_json() {
    # The journal's last 20 lines as [{ts, kind, note}].
    [ -s "$STATE_DIR/events" ] || { echo '[]'; return 0; }
    tail -n 20 "$STATE_DIR/events" \
        | awk '$1 ~ /^[0-9]+$/ && NF >= 2 {
            ts = $1; kind = $2; $1 = ""; $2 = ""; sub(/^ +/, ""); print ts "\t" kind "\t" $0 }' \
        | jq -Rn '[inputs | split("\t") | {ts: (.[0] | tonumber), kind: .[1], note: (.[2] // "")}]'
}

# --- game pulse (v7) -------------------------------------------------------------------
# Switched on per router in the admin card (off by default). Every N-th run the agent times a
# request to a few game-region addresses the server lists — straight from the router and
# through each VPN server's test port — so the admin sees which path is fastest from this ISP.
# Only the agent's own short connections are timed. Hard limits keep a weak router unloaded:
# at most 4 targets x 5 paths, one curl (two HEADs) per pair, 60 s per run (the rest goes next
# time).

PULSE_MAX_TARGETS=4
PULSE_MAX_PATHS=5
PULSE_BUDGET_S=60

pulse_settings() {
    # $1 = revision advertised on /config. Cached in pulse.json: refreshed hourly, or at once
    # when the revision changes (pulse switched on/off, targets edited). A failed fetch keeps
    # the cache; without one there is no pulse.
    ps_file="$STATE_DIR/pulse.json"
    if [ ! -f "$ps_file" ] || [ -n "$(find "$ps_file" -mmin +60 2>/dev/null)" ] \
        || { [ -n "$1" ] && [ "$1" != "$(cat "$STATE_DIR/pulse.rev" 2>/dev/null)" ]; }; then
        if curl -fsS -m 15 -H "Authorization: Bearer $TOKEN" -o "$ps_file.tmp" \
            "$API_BASE/api/agent/pulse" </dev/null 2>/dev/null \
            && jq -e '.enabled | type == "boolean"' "$ps_file.tmp" >/dev/null 2>&1; then
            mv "$ps_file.tmp" "$ps_file"
            printf '%s' "$1" > "$STATE_DIR/pulse.rev"
        else
            rm -f "$ps_file.tmp"
        fi
    fi
    [ -f "$ps_file" ]
}

pulse_time() {
    # $1 = URL, $2 = fixed IP ("" = normal DNS), $3 = test SOCKS port ("" = straight from the
    # router). Prints the path's round trip in ms; nothing on an error or timeout (= loss).
    # Two HEADs in one curl: the first opens the connection (tunnel to the VPN node, TCP, TLS)
    # and is not counted; the second goes over that same connection, so its time to the first
    # byte is one round trip — measured the same way direct and through a server. Counted only
    # if curl really reused the connection (num_connects 0 on the second request).
    pt_url="$1"
    pt_ip="$2"
    pt_port="$3"
    pt_hp="${pt_url#*://}"
    pt_hp="${pt_hp%%/*}"
    pt_host="${pt_hp%%:*}"
    pt_tport=443
    [ "$pt_hp" = "$pt_host" ] || pt_tport="${pt_hp#*:}"
    set -- -s -I -o /dev/null -o /dev/null -m "$PULSE_TIMEOUT" \
        --connect-timeout "$PULSE_TIMEOUT" -w '%{num_connects} %{time_starttransfer}\n'
    if [ -n "$pt_ip" ]; then
        # A pinned address (the name would resolve to whichever edge is nearest): local
        # resolve via --resolve, so --socks5 hands the proxy the IP, not the name.
        set -- "$@" --resolve "$pt_host:$pt_tport:$pt_ip"
        [ -z "$pt_port" ] || set -- "$@" --socks5 "127.0.0.1:$pt_port"
    elif [ -n "$pt_port" ]; then
        set -- "$@" --socks5-hostname "127.0.0.1:$pt_port"
    fi
    pt_t="$(curl "$@" "$pt_url" "$pt_url" </dev/null 2>/dev/null)" || true
    printf '%s\n' "$pt_t" \
        | awk 'NR == 2 && $1 == "0" && $2 + 0 > 0 { printf "%d", $2 * 1000 + 0.5 }'
}

game_pulse() {
    # $1 = pulse revision from /config. Leaves the report in PULSE_JSON ("null" = none).
    PULSE_JSON="null"
    is_xray_running && [ -f "$TARGET" ] || return 0
    pulse_settings "${1:-}" || return 0
    pf="$STATE_DIR/pulse.json"
    jq -e '.enabled == true' "$pf" >/dev/null 2>&1 || return 0
    every="$(jq -r '.every_runs // 2' "$pf" 2>/dev/null)" || every=2
    case "$every" in ''|*[!0-9]*|0) every=2 ;; esac
    PULSE_TIMEOUT="$(jq -r '.timeout_s // 3' "$pf" 2>/dev/null)" || PULSE_TIMEOUT=3
    case "$PULSE_TIMEOUT" in ''|*[!0-9]*|0) PULSE_TIMEOUT=3 ;; esac
    [ "$PULSE_TIMEOUT" -le 10 ] || PULSE_TIMEOUT=10
    runs="$(cat "$STATE_DIR/pulse_runs" 2>/dev/null || echo 0)"
    case "$runs" in ''|*[!0-9]*) runs=0 ;; esac
    runs=$((runs + 1))
    echo "$runs" > "$STATE_DIR/pulse_runs"
    [ $((runs % every)) -eq 0 ] || return 0

    # Paths: direct + each server's test port (not «routed»/«balancer»: the clients' path and
    # the balancer repeat what the servers' own ports already show).
    {
        echo "direct -"
        jq -r '.inbounds[]? | select(.tag | startswith("cwtest-"))
            | select(.tag != "cwtest-routed" and .tag != "cwtest-balancer")
            | "\(.tag | ltrimstr("cwtest-")) \(.port)"' "$TARGET" 2>/dev/null
    } | head -n "$PULSE_MAX_PATHS" > "$STATE_DIR/pulse.paths"
    pairs="$STATE_DIR/pulse.pairs"
    jq -r --argjson n "$PULSE_MAX_TARGETS" '.targets[:$n][]? | select(.id and .url)
        | "\(.id) \(.url) \(if (.resolve_ip // "") == "" then "-" else .resolve_ip end)"' \
        "$pf" 2>/dev/null | while read -r p_id p_url p_ip; do
            while read -r p_path p_port; do
                echo "$p_id $p_url $p_ip $p_path $p_port"
            done < "$STATE_DIR/pulse.paths"
        done > "$pairs"
    total="$(wc -l < "$pairs" | tr -d ' ')"
    [ "${total:-0}" -gt 0 ] || return 0
    # Out of budget last time? Start where it stopped, so every pair gets its turn.
    start="$(cat "$STATE_DIR/pulse_next" 2>/dev/null || echo 0)"
    case "$start" in ''|*[!0-9]*) start=0 ;; esac
    [ "$start" -lt "$total" ] || start=0
    { tail -n +"$((start + 1))" "$pairs"; head -n "$start" "$pairs"; } > "$pairs.order"

    began="$(date +%s)"
    measured=0
    : > "$STATE_DIR/pulse.res"
    while read -r p_id p_url p_ip p_path p_port; do
        # Worst case per pair: both requests time out (curl's -m is per request).
        [ $(($(date +%s) - began + 2 * PULSE_TIMEOUT)) -le "$PULSE_BUDGET_S" ] || break
        [ "$p_ip" != "-" ] || p_ip=""
        [ "$p_port" != "-" ] || p_port=""
        printf '%s\t%s\t%s\n' "$p_id" "$p_path" "$(pulse_time "$p_url" "$p_ip" "$p_port")" \
            >> "$STATE_DIR/pulse.res"
        measured=$((measured + 1))
    done < "$pairs.order"
    echo $(( (start + measured) % total )) > "$STATE_DIR/pulse_next"

    results="$(jq -Rn '[inputs | split("\t") | {target: .[0], path: .[1],
        ms: (if (.[2] // "") == "" then null else (.[2] | tonumber) end)}]' \
        < "$STATE_DIR/pulse.res" 2>/dev/null)" || results='[]'
    PULSE_JSON="$(jq -n --argjson r "${results:-[]}" --arg ts "$began" \
        '{ts: ($ts | tonumber), results: $r}' 2>/dev/null)" || PULSE_JSON="null"
    [ -n "$PULSE_JSON" ] || PULSE_JSON="null"
    log "pulse: $measured of $total measurements in $(($(date +%s) - began)) s"
}

send_heartbeat() {
    running=false
    is_xray_running && running=true
    diag="$(build_diagnostics 2>/dev/null || echo '{}')"
    events="$(events_json 2>/dev/null)" || events='[]'
    printf '%s' "$events" | jq -e 'type == "array"' >/dev/null 2>&1 || events='[]'
    printf '%s' "${PULSE_JSON:-null}" | jq -e 'type == "object"' >/dev/null 2>&1 \
        || PULSE_JSON="null"
    body="$(jq -n \
        --argjson xray_running "$running" \
        --arg xray_version "$(xray_version)" \
        --arg last_error "${LAST_ERROR:-}" \
        --arg external_ip "${EXTERNAL_IP:-}" \
        --argjson diagnostics "$diag" \
        --argjson events "$events" \
        --argjson pulse "$PULSE_JSON" \
        '{xray_running: $xray_running, xray_version: $xray_version, last_error: $last_error,
          external_ip: $external_ip, diagnostics: $diagnostics, events: $events, pulse: $pulse}
         | with_entries(select(.value != "" and .value != null and .value != []))')"
    curl -sS -m 15 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -X POST --data "$body" \
        "$API_BASE/api/agent/heartbeat" > "$STATE_DIR/last_heartbeat_code" 2>>"$LOG_FILE" || true
}

# --- main ------------------------------------------------------------------------------

# --- watchdog: never let a bad config strand the router ---------------------------------
# On XKeen the router's own traffic can go through Xray too. A config that breaks routing
# (field: a «VPN off» config left XKeen's template routing pointing at its placeholder proxy;
# another time a hand-edited fallback) took the internet down for everyone — this agent
# included, so no fix from the server could ever arrive. Fail open: when the server has been
# unreachable 2 runs in a row AND nothing else answers either while Xray is up, stop XKeen,
# fetch the config again over the plain connection and let it start Xray back up. If only
# our server is down but the internet works, nothing is touched.

internet_ok() {
    curl -sS -m 8 -o /dev/null https://ya.ru 2>/dev/null \
        || curl -sS -m 8 -o /dev/null https://www.google.com 2>/dev/null
}

watchdog() {
    if [ "$1" != "000" ]; then
        rm -f "$STATE_DIR/net_fail"
        return 0
    fi
    fails="$(cat "$STATE_DIR/net_fail" 2>/dev/null || echo 0)"
    fails=$((fails + 1))
    echo "$fails" > "$STATE_DIR/net_fail"
    if [ "$fails" -lt 2 ] || ! is_xray_running || internet_ok; then
        return 0
    fi
    log "WATCHDOG: no server and no internet for $fails runs with Xray up — stopping XKeen (fail-open)"
    event watchdog_stop "no server and no internet for $fails runs"
    touch "$STATE_DIR/stopped_by_agent"
    if command -v xkeen >/dev/null 2>&1; then
        xkeen -stop >> "$LOG_FILE" 2>&1 </dev/null || true
    elif [ -x /opt/etc/init.d/S05xkeen ]; then
        /opt/etc/init.d/S05xkeen stop >> "$LOG_FILE" 2>&1 </dev/null || true
    fi
    # Retry right away with a full fetch (no etag), so the fresh config restarts Xray.
    rm -f "$STATE_DIR/net_fail" "$ETAG_FILE" "$STATE_DIR/headers.tmp" "$STATE_DIR/body.tmp"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    trap - EXIT INT TERM
    exec sh "$SELF"
}

main() {
    rotate_log_if_needed
    acquire_lock
    require_bin curl
    require_bin jq

    if [ ! -f "$CONF_FILE" ]; then
        log "FATAL: config file not found at $CONF_FILE"
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$CONF_FILE"
    : "${API_BASE:?API_BASE must be set in $CONF_FILE}"
    : "${TOKEN:?TOKEN must be set in $CONF_FILE}"
    : "${CONFDIR:=/opt/etc/xray/configs}"
    : "${OUTFILE:=10_cipherway.json}"

    mkdir -p "$STATE_DIR"
    ETAG_FILE="$STATE_DIR/etag"
    TARGET="$CONFDIR/$OUTFILE"
    LAST_ERROR=""
    NEED_RESTART=0
    EXTERNAL_IP="$(curl -sS -m 5 https://api.ipify.org 2>/dev/null || true)"

    enforce_route_only_false

    inm=""
    [ -f "$ETAG_FILE" ] && inm="$(cat "$ETAG_FILE")"
    headers_tmp="$STATE_DIR/headers.tmp"
    body_tmp="$STATE_DIR/body.tmp"
    # An empty $inm still yields a valid (never-matching) If-None-Match header — keeping it
    # unconditional sidesteps POSIX sh's awkward rules for optionally-quoted arguments.
    http_code="$(curl -sS -m 20 -o "$body_tmp" -D "$headers_tmp" -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        -H "If-None-Match: \"$inm\"" \
        "$API_BASE/api/agent/config" 2>>"$LOG_FILE" || echo "000")"

    # curl prints 000 on a network failure and `|| echo` appends another: normalise.
    case "$http_code" in 000*) http_code="000" ;; esac
    watchdog "$http_code"

    case "$http_code" in
        200)
            if ! jq empty "$body_tmp" >/dev/null 2>&1; then
                LAST_ERROR="server sent invalid JSON, kept previous config"
                log "ERROR: $LAST_ERROR"
            else
                new_etag="$(grep -i '^etag:' "$headers_tmp" | sed 's/^[Ee][Tt][Aa][Gg]: *"\{0,1\}//; s/"\{0,1\}[[:space:]]*$//')"
                mkdir -p "$CONFDIR"
                if apply_config "$body_tmp"; then
                    [ -n "$new_etag" ] && printf '%s' "$new_etag" > "$ETAG_FILE"
                    log "config applied -> $TARGET (etag=$new_etag)"
                    event config_applied "etag=$(printf '%s' "$new_etag" | cut -c1-12)"
                else
                    rm -f "$ETAG_FILE"
                fi
            fi
            ;;
        304)
            ;;
        401|403)
            LAST_ERROR="token rejected by server (http $http_code) — device revoked or wrong token"
            log "ERROR: $LAST_ERROR"
            ;;
        429)
            log "rate limited (429), will retry next run"
            ;;
        503)
            LAST_ERROR="panel temporarily unavailable, kept previous config"
            log "WARN: $LAST_ERROR"
            ;;
        *)
            LAST_ERROR="unexpected response fetching config (http $http_code)"
            log "ERROR: $LAST_ERROR"
            ;;
    esac

    if [ "$NEED_RESTART" = "1" ]; then
        restart_xray || true
    fi

    # Back from a watchdog stop: the server answered, so bring the VPN back (a fresh config
    # was just applied — the etag was dropped — and that restarts Xray; start it otherwise).
    if [ -f "$STATE_DIR/stopped_by_agent" ] && { [ "$http_code" = "200" ] || [ "$http_code" = "304" ]; }; then
        is_xray_running || restart_xray || true
        rm -f "$STATE_DIR/stopped_by_agent"
        LAST_ERROR="${LAST_ERROR:-VPN временно выключался агентом: с включённым Xray роутер терял интернет. Конфиг получен заново, VPN включён.}"
        log "WATCHDOG: server reachable again, VPN back on"
        event watchdog_resume "server reachable again"
    fi

    advertised="$(grep -i '^x-agent-version:' "$headers_tmp" 2>/dev/null | sed 's/^[^:]*: *//; s/[[:space:]]*$//')"
    pulse_rev="$(grep -i '^x-pulse-rev:' "$headers_tmp" 2>/dev/null | sed 's/^[^:]*: *//; s/[[:space:]]*$//')"
    rm -f "$headers_tmp" "$body_tmp"

    PULSE_JSON="null"
    if [ "$http_code" != "401" ] && [ "$http_code" != "403" ]; then
        case "$http_code" in 200|304) game_pulse "$pulse_rev" || true ;; esac
        # A failed heartbeat must never cost the router its self-update (and with it the way
        # back to an older agent): under set -e a jq/curl error here would end the run first.
        send_heartbeat || true
        self_update "$advertised"
    fi
}

main "$@"
