#!/bin/sh
# CipherWay router installer — one command on a Keenetic router with Entware:
#
#   opkg update && opkg install curl && curl -fsSL <API_BASE>/api/agent/install.sh \
#       -o /tmp/cw-install.sh && sh /tmp/cw-install.sh <TOKEN> <API_BASE>
#
# Idempotent: safe to re-run (e.g. to repair a router or re-issue a token). Installs packages,
# XKeen + Xray (answering XKeen's interactive installer for the pinned, tested version), the
# baseline XKeen settings, the agent and its cron job, runs the agent once and reports the
# result to the admin panel. Stops early with a plain-Russian reason if a prerequisite that
# only the Keenetic web UI can provide is missing.

set -u

INSTALLER_VERSION="2"
XKEEN_VERSION="2.0"
XRAY_VERSION="v26.7.28"   # the server may override: the version the VPN nodes run
AGENT_DIR="/opt/etc/cipherway-agent"
CONFDIR="/opt/etc/xray/configs"
LOG="/opt/var/log/cipherway-install.log"
XK_LOG="/opt/var/log/cipherway-xkeen.log"
XK_NOISE="Некорректн"   # XKeen's «invalid input» line
MIN_FREE_MB=60

TOKEN="${1:-}"
API_BASE="${2:-https://cabinet.cipherway.net.ru}"
API_BASE="${API_BASE%/}"

STEPS=""
FAILED=""

say() { printf '\n==> %s\n' "$*"; echo "$(date '+%F %T') $*" >> "$LOG"; }
ok() { printf '    ok: %s\n' "$*"; STEPS="$STEPS$1=ok;"; }
warn() { printf '    ВНИМАНИЕ: %s\n' "$*"; echo "WARN $*" >> "$LOG"; }

fail() {
    step="$1"; shift
    printf '\n!!! ОШИБКА: %s\n' "$*"
    echo "FAIL $step: $*" >> "$LOG"
    STEPS="$STEPS$step=fail;"
    FAILED="$step: $*"
    report
    printf '\nУстановка остановлена. Отчёт отправлен в админ-панель (если был доступ к серверу).\n'
    exit 1
}

report() {
    command -v jq >/dev/null 2>&1 || return 0
    command -v curl >/dev/null 2>&1 || return 0
    # XKeen's «invalid input» spam filtered out, so the tail shows what actually happened.
    log_tail="$(grep -v "$XK_NOISE" "$LOG" 2>/dev/null | tail -n 60)"
    body="$(jq -n \
        --arg installer_version "$INSTALLER_VERSION" \
        --arg xkeen_version "$XKEEN_VERSION" \
        --arg steps "$STEPS" \
        --arg failed "$FAILED" \
        --arg arch "$(opkg print-architecture 2>/dev/null | awk '/aarch64|mips|arm/ {print $2}' | tr '\n' ' ')" \
        --arg opt_device "$(mount | awk '$3 == "/opt" {print $1}')" \
        --arg log_tail "$log_tail" \
        '{installer_version: $installer_version, xkeen_version: $xkeen_version,
          steps: $steps, failed: $failed, arch: $arch, opt_device: $opt_device,
          log_tail: $log_tail}')"
    curl -sS -m 20 -o /dev/null \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -X POST --data "$body" "$API_BASE/api/agent/install-report" 2>/dev/null || true
}

mkdir -p /opt/var/log
echo "=== install $(date '+%F %T') installer v$INSTALLER_VERSION" >> "$LOG"

# --- 1. preflight ----------------------------------------------------------------------

say "Проверка роутера"
[ -n "$TOKEN" ] || { printf 'Использование: sh cw-install.sh <ТОКЕН> [АДРЕС_СЕРВЕРА]\n'; exit 2; }
[ -x /opt/bin/opkg ] || fail preflight "Entware не найден (/opt/bin/opkg). Установите Entware на USB-флешку через веб-интерфейс Keenetic (компонент OPKG)."

case "$(opkg print-architecture 2>/dev/null)" in
    *aarch64*|*mipsel*|*mips*) ;;
    *) fail preflight "Неподдерживаемая архитектура роутера: $(opkg print-architecture | tr '\n' ' ')" ;;
esac

if [ ! -f "/lib/modules/$(uname -r)/xt_TPROXY.ko" ]; then
    fail preflight "Нет модулей ядра Netfilter. В веб-интерфейсе Keenetic: Общие настройки → Изменить набор компонентов → включите «Модули ядра подсистемы Netfilter», обновите роутер и запустите установку снова."
fi

free_mb="$(df -m /opt 2>/dev/null | awk 'NR==2 {print $4}')"
if [ -n "$free_mb" ] && [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    fail preflight "Мало места на накопителе Entware: ${free_mb} МБ, нужно минимум ${MIN_FREE_MB} МБ."
fi
ok preflight "Entware, архитектура, модули ядра, место (${free_mb:-?} МБ)"

# --- 2. packages -----------------------------------------------------------------------

say "Установка пакетов (curl, jq, tar, ca-bundle)"
opkg update >> "$LOG" 2>&1 </dev/null || warn "opkg update завершился с ошибкой, продолжаю"
opkg install curl jq tar ca-bundle >> "$LOG" 2>&1 </dev/null \
    || fail packages "Не удалось установить пакеты. Проверьте интернет на роутере. Подробности: $LOG"
ok packages "установлены"

say "Проверка токена"
code="$(curl -sS -m 20 -o /tmp/cw-whoami.json -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" "$API_BASE/api/agent/whoami" 2>>"$LOG" || echo 000)"
case "$code" in
    200) server_xray="$(jq -r '.xray_version // empty' /tmp/cw-whoami.json 2>/dev/null)"
         [ -n "$server_xray" ] && XRAY_VERSION="$server_xray"
         ok token "роутер «$(jq -r '.label // "?"' /tmp/cw-whoami.json)», клиент $(jq -r '.client // "?"' /tmp/cw-whoami.json)" ;;
    401|403) fail token "Сервер не принял токен (HTTP $code). Скопируйте команду из админки заново или выпустите новый токен." ;;
    *) fail token "Сервер $API_BASE недоступен (HTTP $code). Проверьте интернет на роутере." ;;
esac

# --- 3. XKeen + Xray -------------------------------------------------------------------

GH_MIRROR="$API_BASE/gh"

# Some ISPs freeze connections to GitHub after ~16 KB — XKeen and Xray then never finish
# downloading. If our server passes a large download fine, XKeen's own «gh_proxy» setting is
# pointed at the server's GitHub mirror (top-level key: XKeen refuses to start when an «xkeen»
# section has no policies).
use_mirror=0
big="$(curl -sS -m 25 -H "Authorization: Bearer $TOKEN" -o /dev/null -w '%{http_code} %{size_download}' \
    "$API_BASE/api/agent/probe" 2>>"$LOG" || true)"
if [ "${big%% *}" = "200" ] && [ "${big#* }" -ge 200000 ] 2>/dev/null; then
    use_mirror=1
    xk_cfg="/opt/etc/xkeen/xkeen.json"
    mkdir -p /opt/etc/xkeen
    if [ ! -s "$xk_cfg" ]; then
        printf '{\n  "gh_proxy": "%s"\n}\n' "$GH_MIRROR" > "$xk_cfg"
    elif ! grep -q '"gh_proxy"' "$xk_cfg"; then
        jq --arg u "$GH_MIRROR" '. + {gh_proxy: $u}' "$xk_cfg" > "$xk_cfg.tmp" 2>>"$LOG" \
            && mv "$xk_cfg.tmp" "$xk_cfg"
    fi
    echo "GitHub mirror: $GH_MIRROR" >> "$LOG"
else
    warn "большая загрузка с сервера не прошла (${big:-нет ответа}) — XKeen будет качать с GitHub напрямую"
fi

# Answer for one XKeen question, chosen by its text ($1 = XKeen's output since the previous
# answer). Nothing = a question this installer doesn't know -> stop and report it, instead of
# the old fixed answer list running out and XKeen printing «Некорректный ввод» forever.
xkeen_answer() {
    case "$1" in
        *"Выберите час"*) echo 4 ;;
        *"Выберите минуту"*) echo 0 ;;
        *"Выберите день"*) echo 0 ;;                         # no geo-file auto-update schedule
        *"ядро проксирования"*) echo 1 ;;                     # Xray
        *"Введите версию Xray"*) echo "$XRAY_VERSION" ;;     # same core as the VPN nodes
        *"порядковый номер релиза"*) echo 9 ;;                # 9 = type the version
        *"номера действий через пробел"*) echo 0 ;;           # GeoSite/GeoIP: skip
        *"автообновления"*) echo 0 ;;
        *"российские IP-адреса"*) echo 1 ;;
        *"автозагрузку"*) echo 1 ;;                            # start XKeen on boot
        *"IPv6"*) echo 0 ;;                                    # leave IPv6 as it is
        *"Продолжить установку"*) echo 1 ;;
        *) ;;
    esac
}

strip_ansi() { sed 's/\x1b\[[0-9;]*[A-Za-z]//g; s/\r//g'; }

# Hard failure on a fresh install; with XK_SOFT=1 (only swapping the Xray core on a working
# router) a warning, and the router keeps the core it has.
xk_fail() {
    if [ "${XK_SOFT:-0}" = "1" ]; then warn "$1"; else fail xkeen "$1"; fi
}

run_xkeen_install() {
    # $@ = the command to run: XKeen's bootstrap (which ends in `xkeen -i`) or `xkeen -ux`
    fifo="/tmp/cw-xkeen.in"
    rm -f "$fifo"
    mkfifo "$fifo" || { xk_fail "Не удалось создать канал для ответов установщику XKeen."; return 1; }
    : > "$XK_LOG"
    # No autoinstall_mode: it would take the newest Xray; the release question is answered
    # with the pinned version instead.
    "$@" < "$fifo" > "$XK_LOG" 2>&1 &
    xk_pid=$!
    exec 3> "$fifo"      # keeps the channel open: XKeen waits for an answer instead of EOF

    waited=0; stable=0; last_size=-1; answered_at=0; answers=0
    while kill -0 "$xk_pid" 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        size="$(wc -c < "$XK_LOG" 2>/dev/null || echo 0)"
        if [ "$size" = "$last_size" ]; then stable=$((stable + 1)); else stable=0; last_size="$size"; fi

        if [ $((waited % 15)) -eq 0 ]; then
            printf '    … %s с: %s\n' "$waited" \
                "$(strip_ansi < "$XK_LOG" | grep -v '^[[:space:]]*$' | tail -n 1)"
        fi

        # XKeen is waiting at a question: output ends in ": " and stopped growing.
        if [ "$stable" -ge 2 ] && [ "$size" != "$answered_at" ] \
            && [ "$(tail -c 2 "$XK_LOG" 2>/dev/null)" = ": " ]; then
            ctx="$(tail -c +$((answered_at + 1)) "$XK_LOG" | strip_ansi | tail -n 25)"
            # The question came back with «Некорректный ввод»: XKeen rejected our answer
            # (its menu changed). Stop now rather than answering the same thing forever.
            if [ "$answers" -gt 0 ] && printf '%s' "$ctx" | grep -q "$XK_NOISE"; then
                kill "$xk_pid" 2>/dev/null
                exec 3>&-
                tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
                xk_fail "XKeen не принял ответ установщика на вопрос «$(printf '%s' "$ctx" | grep -v '^[[:space:]]*$' | grep -v "$XK_NOISE" | tail -n 2 | tr '\n' ' ')». Подробности отправлены в админ-панель."; return 1
            fi
            ans="$(xkeen_answer "$ctx")"
            if [ -z "$ans" ] || [ "$answers" -ge 20 ]; then
                kill "$xk_pid" 2>/dev/null
                exec 3>&-
                printf '%s\n' "$ctx" >> "$LOG"
                xk_fail "XKeen задал вопрос, на который у установщика нет ответа: «$(printf '%s' "$ctx" | grep -v '^[[:space:]]*$' | tail -n 3 | tr '\n' ' ')». Текст вопроса отправлен в админ-панель."; return 1
            fi
            echo "$ans" >&3
            answers=$((answers + 1))
            answered_at="$size"
            printf '    %s → %s\n' "$(printf '%s' "$ctx" | grep -v '^[[:space:]]*$' | grep -v ': $' | tail -n 1)" "$ans"
            echo ">>> answer: $ans" >> "$LOG"
        fi

        # XKeen rejected an answer (its menu changed): stop now, not after 15 minutes.
        if [ "$(tail -c +$((answered_at + 1)) "$XK_LOG" 2>/dev/null | grep -c "$XK_NOISE")" -ge 2 ]; then
            kill "$xk_pid" 2>/dev/null
            exec 3>&-
            tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
            xk_fail "XKeen не принял ответ установщика (изменились вопросы). Подробности отправлены в админ-панель."; return 1
        fi
        if [ "$waited" -ge 1200 ]; then
            kill "$xk_pid" 2>/dev/null
            exec 3>&-
            tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
            xk_fail "Установка XKeen идёт больше 20 минут — прерываю. Подробности отправлены в админ-панель."; return 1
        fi
    done
    exec 3>&-
    rm -f "$fifo"
    strip_ansi < "$XK_LOG" | grep -v '^[[:space:]]*$' | tail -n 40 >> "$LOG"
}

if command -v xkeen >/dev/null 2>&1 && [ -x /opt/sbin/xray ] && [ -f /opt/etc/init.d/S05xkeen ]; then
    say "XKeen уже установлен — пропускаю установку"
    cur_xray="$(/opt/sbin/xray version 2>/dev/null | head -n 1 | awk '{print $2}')"
    if [ -n "$cur_xray" ] && [ "v${cur_xray#v}" != "$XRAY_VERSION" ]; then
        say "Xray на роутере $cur_xray, на серверах ${XRAY_VERSION#v} — ставлю ту же версию"
        XK_SOFT=1
        if run_xkeen_install xkeen -ux && [ "v$(/opt/sbin/xray version 2>/dev/null | head -n 1 | awk '{print $2}')" = "$XRAY_VERSION" ]; then
            ok xkeen "уже был; Xray заменён на $XRAY_VERSION"
        else
            warn "Xray остался версии $cur_xray — замена не удалась, подробности в админке"
            ok xkeen "уже был (Xray $cur_xray)"
        fi
        XK_SOFT=0
    else
        ok xkeen "уже был"
    fi
else
    say "Установка XKeen $XKEEN_VERSION и Xray (несколько минут, ход установки ниже)"
    cd /tmp || true
    xk_boot="https://raw.githubusercontent.com/jameszeroX/XKeen/main/install.sh"
    got=0
    if [ "$use_mirror" = "1" ]; then
        curl -fsSL -m 60 "$GH_MIRROR/$xk_boot" -o /tmp/xkeen-install.sh 2>>"$LOG" && got=1
    fi
    [ "$got" = "1" ] || curl -fsSL -m 60 "$xk_boot" -o /tmp/xkeen-install.sh 2>>"$LOG" \
        || fail xkeen "Не удалось скачать установщик XKeen (GitHub недоступен у этого провайдера)."
    printf '    скачиваю XKeen %s…\n' "$XKEEN_VERSION"
    # XKeen's bootstrap unpacks XKeen and then itself `exec`s `xkeen -i` — so it runs under
    # the answering loop too (fed /dev/null it looped on «Некорректный ввод» forever).
    run_xkeen_install sh /tmp/xkeen-install.sh --legacy "$XKEEN_VERSION"

    if [ ! -x /opt/sbin/xkeen ]; then
        fail xkeen "Не удалось скачать XKeen $XKEEN_VERSION (GitHub недоступен у этого провайдера). Подробности отправлены в админ-панель."
    fi
    if [ ! -x /opt/sbin/xray ] || [ ! -f /opt/etc/init.d/S05xkeen ]; then
        fail xkeen "XKeen установился не полностью (нет /opt/sbin/xray или S05xkeen) — скорее всего, не скачался Xray. Подробности отправлены в админ-панель."
    fi
    ok xkeen "XKeen $XKEEN_VERSION и Xray $XRAY_VERSION установлены"
fi

# --- 4. baseline XKeen settings --------------------------------------------------------

say "Базовые настройки XKeen"
mkdir -p "$CONFDIR"
if [ -f "$CONFDIR/03_inbounds.json" ]; then
    sed -i 's/"routeOnly": *true/"routeOnly": false/g' "$CONFDIR/03_inbounds.json"
fi
if [ -f "$CONFDIR/01_log.json" ]; then
    sed -i 's/"loglevel": *"none"/"loglevel": "warning"/' "$CONFDIR/01_log.json"
fi
ok baseline "routeOnly=false, журнал ошибок Xray включён"

# --- 5. agent --------------------------------------------------------------------------

say "Установка агента CipherWay"
mkdir -p "$AGENT_DIR" /opt/var/lib/cipherway-agent /opt/var/run
curl -fsS -m 60 "$API_BASE/api/agent/agent.sh" -o "$AGENT_DIR/agent.sh.new" 2>>"$LOG" \
    || fail agent "Не удалось скачать агент с сервера."
sh -n "$AGENT_DIR/agent.sh.new" || fail agent "Скачанный агент повреждён (ошибка синтаксиса)."
mv "$AGENT_DIR/agent.sh.new" "$AGENT_DIR/agent.sh"
chmod +x "$AGENT_DIR/agent.sh"
cat > "$AGENT_DIR/agent.conf" <<EOF
API_BASE="$API_BASE"
TOKEN="$TOKEN"
CONFDIR="$CONFDIR"
OUTFILE="10_cipherway.json"
EOF
chmod 600 "$AGENT_DIR/agent.conf"
rm -f /opt/var/lib/cipherway-agent/etag
ok agent "агент и конфиг записаны"

# --- 6. cron ---------------------------------------------------------------------------

say "Настройка запуска по расписанию (cron)"
cron_line="*/5 * * * * $AGENT_DIR/agent.sh >/dev/null 2>&1"
if [ -x /opt/etc/init.d/S05crond ]; then
    cron_file="/opt/var/spool/cron/crontabs/root"; cron_init="/opt/etc/init.d/S05crond"
elif [ -x /opt/etc/init.d/S10cron ]; then
    cron_file="/opt/etc/crontabs/root"; cron_init="/opt/etc/init.d/S10cron"
else
    opkg install cron >> "$LOG" 2>&1 </dev/null || fail cron "Не удалось установить cron."
    cron_file="/opt/etc/crontabs/root"; cron_init="/opt/etc/init.d/S10cron"
fi
for f in /opt/var/spool/cron/crontabs/root /opt/etc/crontabs/root; do
    [ -f "$f" ] && sed -i '/cipherway-agent/d' "$f"
done
mkdir -p "$(dirname "$cron_file")"
echo "$cron_line" >> "$cron_file"
"$cron_init" restart >> "$LOG" 2>&1 </dev/null || "$cron_init" start >> "$LOG" 2>&1 </dev/null
pidof crond >/dev/null 2>&1 || fail cron "cron не запустился ($cron_init)."
ok cron "каждые 5 минут ($cron_file)"

# --- 7. first run ----------------------------------------------------------------------

say "Первый запуск агента и Xray"
"$AGENT_DIR/agent.sh" </dev/null || true
if ! pidof xray >/dev/null 2>&1; then
    xkeen -start >> "$LOG" 2>&1 </dev/null || true
fi
sleep 3
if pidof xray >/dev/null 2>&1; then
    ok first_run "Xray работает"
else
    warn "Xray не запущен после первого запуска агента — смотрите /opt/var/log/cipherway-agent.log"
    STEPS="${STEPS}first_run=xray_down;"
fi
tail -n 5 /opt/var/log/cipherway-agent.log 2>/dev/null | sed 's/^/    /'

report
printf '\nГотово. Через 1–2 минуты роутер появится в админ-панели со статусом «Онлайн»,\n'
printf 'там же будет диагностика. Повторный запуск этой команды безопасен.\n'
