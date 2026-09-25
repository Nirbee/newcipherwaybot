#!/bin/sh
# CipherWay router agent — keeps a Keenetic router's Xray (XKeen) config in sync with the bot.
#
# Runs on the router (Entware/busybox ash — POSIX sh only). Invoked from cron every 5 minutes
# by the installer (install.sh); safe to run by hand. Talks to src/web/routes/agent.py:
#   GET  /api/agent/config     -> Xray config fragment (outbounds/routing/dns) for XKeen's
#                                  confdir; X-Agent-Version header advertises the current agent
#   GET  /api/agent/agent.sh   -> this script (self-update)
#   POST /api/agent/heartbeat  -> status + diagnostics shown in the admin panel
#
# Safety rules:
# - 503 from /config = panel briefly unreachable, NOT "no config": keep what's applied.
# - A new config is only kept if `xray run -test` accepts it and xray comes back up after the
#   restart; otherwise the previous file is restored. A bad push must never cost the customer
#   their internet.

set -eu

AGENT_VERSION="2"

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

build_diagnostics() {
    jq -n \
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
          opt_free: $opt_free, xray_test: $xray_test}'
}

send_heartbeat() {
    running=false
    is_xray_running && running=true
    diag="$(build_diagnostics 2>/dev/null || echo '{}')"
    body="$(jq -n \
        --argjson xray_running "$running" \
        --arg xray_version "$(xray_version)" \
        --arg last_error "${LAST_ERROR:-}" \
        --arg external_ip "${EXTERNAL_IP:-}" \
        --argjson diagnostics "$diag" \
        '{xray_running: $xray_running, xray_version: $xray_version, last_error: $last_error,
          external_ip: $external_ip, diagnostics: $diagnostics}
         | with_entries(select(.value != ""))')"
    curl -sS -m 15 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -X POST --data "$body" \
        "$API_BASE/api/agent/heartbeat" > "$STATE_DIR/last_heartbeat_code" 2>>"$LOG_FILE" || true
}

# --- main ------------------------------------------------------------------------------

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

    advertised="$(grep -i '^x-agent-version:' "$headers_tmp" 2>/dev/null | sed 's/^[^:]*: *//; s/[[:space:]]*$//')"
    rm -f "$headers_tmp" "$body_tmp"

    if [ "$http_code" != "401" ] && [ "$http_code" != "403" ]; then
        send_heartbeat
        self_update "$advertised"
    fi
}

main "$@"
