#!/bin/sh
# CipherWay router agent — polls the bot for this router's Xray config and applies it via XKeen.
#
# Runs on the Keenetic router itself (Entware/busybox ash — POSIX sh only, no bashisms). Meant
# to be invoked from cron every 5 minutes; also safe to run by hand for testing. See README.md
# in this directory for installation.
#
# Talks to src/web/routes/agent.py on the bot:
#   GET  /api/agent/config     -> {"outbounds": [...], "observatory": {...}, "routing": {...}}
#                                  a *fragment*, not a full Xray config — merged into XKeen's
#                                  own confdir alongside its inbounds/log/dns files.
#   POST /api/agent/heartbeat  -> reports whether xray is actually running, its version, etc.
#
# A 503 from /config means the panel is temporarily unreachable — NOT "no config" — so on a
# 503 this script deliberately leaves whatever config is already on disk untouched and only
# sends the heartbeat. Only a 200 (new config) or 304 (unchanged) touch the config file.

set -eu

CONF_FILE="${CIPHERWAY_AGENT_CONF:-/opt/etc/cipherway-agent/agent.conf}"
STATE_DIR="/opt/var/lib/cipherway-agent"
LOG_FILE="/opt/var/log/cipherway-agent.log"
LOCK_DIR="/opt/var/run/cipherway-agent.lock"
LOG_MAX_BYTES=524288

log() {
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "$ts $*" >> "$LOG_FILE"
}

rotate_log_if_needed() {
    [ -f "$LOG_FILE" ] || return 0
    size="$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)"
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
        tail -n 500 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
    fi
}

acquire_lock() {
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        log "another run is still in progress, skipping"
        exit 0
    fi
    trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT INT TERM
}

require_bin() {
    command -v "$1" >/dev/null 2>&1 || {
        log "FATAL: '$1' not found — install it via opkg (see README.md)"
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
    [ -x "$bin" ] || { echo ""; return; }
    "$bin" version 2>/dev/null | head -n1 | sed 's/^Xray //'
}

restart_xray() {
    if command -v xkeen >/dev/null 2>&1; then
        xkeen -restart >>"$LOG_FILE" 2>&1
        return $?
    fi
    if [ -x /opt/etc/init.d/S24xray ]; then
        /opt/etc/init.d/S24xray restart >>"$LOG_FILE" 2>&1
        return $?
    fi
    log "WARN: no known way to restart xray (neither 'xkeen' nor /opt/etc/init.d/S24xray found)"
    return 1
}

json_str() {
    # Minimal JSON string escaping for values we build heartbeat bodies with by hand.
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

send_heartbeat() {
    xray_running_val="false"
    is_xray_running && xray_running_val="true"
    version="$(xray_version)"
    body="{\"xray_running\":$xray_running_val"
    [ -n "$version" ] && body="$body,\"xray_version\":\"$(json_str "$version")\""
    if [ -n "${LAST_ERROR:-}" ]; then
        body="$body,\"last_error\":\"$(json_str "$LAST_ERROR")\""
    fi
    if [ -n "${EXTERNAL_IP:-}" ]; then
        body="$body,\"external_ip\":\"$(json_str "$EXTERNAL_IP")\""
    fi
    body="$body}"
    curl -sS -m 10 -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -X POST --data "$body" \
        "$API_BASE/api/agent/heartbeat" > "$STATE_DIR/last_heartbeat_code" 2>>"$LOG_FILE" || true
}

fetch_external_ip() {
    curl -sS -m 5 "https://api.ipify.org" 2>/dev/null || true
}

main() {
    rotate_log_if_needed
    acquire_lock
    require_bin curl
    require_bin jq

    if [ ! -f "$CONF_FILE" ]; then
        log "FATAL: config file not found at $CONF_FILE — see README.md"
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$CONF_FILE"

    : "${API_BASE:?API_BASE must be set in $CONF_FILE}"
    : "${TOKEN:?TOKEN must be set in $CONF_FILE}"
    : "${CONFDIR:?CONFDIR must be set in $CONF_FILE}"
    : "${OUTFILE:=10_cipherway.json}"

    mkdir -p "$STATE_DIR"
    ETAG_FILE="$STATE_DIR/etag"
    TARGET="$CONFDIR/$OUTFILE"
    LAST_ERROR=""
    EXTERNAL_IP="$(fetch_external_ip)"

    inm=""
    [ -f "$ETAG_FILE" ] && inm="$(cat "$ETAG_FILE")"

    headers_tmp="$STATE_DIR/headers.tmp"
    body_tmp="$STATE_DIR/body.tmp"

    # An empty $inm still produces a syntactically valid (if useless) If-None-Match header —
    # it just never matches a real ETag, so the first-ever run naturally falls through to 200.
    # Keeping this unconditional sidesteps POSIX sh's awkward rules for optionally-quoted args.
    http_code="$(curl -sS -m 15 -o "$body_tmp" -D "$headers_tmp" -w '%{http_code}' \
        -H "Authorization: Bearer $TOKEN" \
        -H "If-None-Match: \"$inm\"" \
        "$API_BASE/api/agent/config" 2>>"$LOG_FILE" || echo "000")"

    case "$http_code" in
        200)
            if ! jq empty "$body_tmp" >/dev/null 2>&1; then
                LAST_ERROR="agent received invalid JSON config, kept previous config"
                log "ERROR: $LAST_ERROR"
            else
                mkdir -p "$CONFDIR"
                cp "$body_tmp" "$TARGET.tmp"
                mv "$TARGET.tmp" "$TARGET"
                new_etag="$(grep -i '^etag:' "$headers_tmp" | sed 's/^[Ee][Tt][Aa][Gg]: *"\{0,1\}//; s/"\{0,1\}[[:space:]]*$//')"
                [ -n "$new_etag" ] && printf '%s' "$new_etag" > "$ETAG_FILE"
                log "config updated -> $TARGET (etag=$new_etag), restarting xray"
                if ! restart_xray; then
                    LAST_ERROR="config applied but xray restart failed"
                    log "ERROR: $LAST_ERROR"
                fi
            fi
            ;;
        304)
            log "config unchanged (304)"
            ;;
        401|403)
            LAST_ERROR="token rejected by server (http $http_code) — device may be revoked"
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

    rm -f "$headers_tmp" "$body_tmp"

    if [ "$http_code" != "401" ] && [ "$http_code" != "403" ]; then
        send_heartbeat
    fi
}

main "$@"
