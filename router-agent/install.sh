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

INSTALLER_VERSION="3"
XKEEN_VERSION="2.0"
XRAY_VERSION="v26.7.28"   # the server may override: the version the VPN nodes run
AGENT_DIR="/opt/etc/cipherway-agent"
CONFDIR="/opt/etc/xray/configs"
LOG="/opt/var/log/cipherway-install.log"
XK_LOG="/opt/var/log/cipherway-xkeen.log"
XK_NOISE="Некорректн"   # XKeen's «invalid input» line
MIN_FREE_MB=60
XK_SILENT_LIMIT=300   # XKeen printing nothing this long = stalled download or unknown prompt

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

# Anti-hang insurance: every step that talks to the network or to another installer runs under
# a time limit, so a stalled download (ISPs that freeze foreign connections after ~16 KB) stops
# with a clear reason instead of hanging the technician's terminal. fd 4 = the terminal, for
# progress lines while the step's own output goes to the log.
exec 4>&1

# Stop a process and its children (a killed installer must not leave its curl hanging).
kill_tree() {
    command -v pkill >/dev/null 2>&1 && pkill -P "$1" 2>/dev/null
    kill "$1" 2>/dev/null
    sleep 1
    command -v pkill >/dev/null 2>&1 && pkill -9 -P "$1" 2>/dev/null
    kill -9 "$1" 2>/dev/null
}

# $1 = seconds, $2 = what the technician sees, rest = the command. 124 = killed on timeout.
run_limited() {
    rl_limit="$1"; rl_what="$2"; shift 2
    "$@" 4>&- &
    rl_pid=$!
    rl_t=0
    while kill -0 "$rl_pid" 2>/dev/null; do
        sleep 1
        rl_t=$((rl_t + 1))
        [ $((rl_t % 15)) -eq 0 ] && printf '    … %s: %s с\n' "$rl_what" "$rl_t" >&4
        if [ "$rl_t" -ge "$rl_limit" ]; then
            kill_tree "$rl_pid"
            wait "$rl_pid" 2>/dev/null
            echo "TIMEOUT ${rl_limit}s: $rl_what ($*)" >> "$LOG"
            return 124
        fi
    done
    wait "$rl_pid"
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
run_limited 180 "обновление списка пакетов" opkg update >> "$LOG" 2>&1 </dev/null \
    || warn "opkg update завершился с ошибкой или завис (3 мин), продолжаю"
run_limited 300 "установка пакетов" opkg install curl jq tar ca-bundle >> "$LOG" 2>&1 </dev/null \
    || fail packages "Не удалось установить пакеты (ошибка или больше 5 минут). Проверьте интернет на роутере. Подробности: $LOG"
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
# answer). Matched on the question's HEADER line, not on its «Ваш выбор:» prompt: XKeen asks with
# `read -p`, and the router's shell prints that prompt only to a real terminal — through our
# answer channel it never appears (the field hang: stuck under «0. Пропустить загрузку ядра…»).
# Phrases are the exact headers of XKeen 2.0 (checked against its source), specific enough that
# XKeen's follow-up messages («Выполнен пропуск настройки автообновления») don't match them.
# Nothing = a question this installer doesn't know.
xkeen_answer() {
    case "$1" in
        *"Введите версию Xray"*) echo "$XRAY_VERSION" ;;     # same core as the VPN nodes
        *"порядковый номер релиза"*) echo 9 ;;                # 9 = type the version
        *"Выберите ядро проксирования"*) echo 1 ;;            # Xray
        *"номера действий через пробел"*) echo 0 ;;           # GeoSite/GeoIP: skip
        *"номер действия для автообновления"*) echo 0 ;;      # no geo-file auto-update
        *"Выберите день"*) echo 0 ;;                          # (auto-update time) cancel
        *"Добавить XKeen в автозагрузку"*) echo 1 ;;          # start XKeen on boot
        *"исключить российские IP-адреса"*) echo 1 ;;
        *"Текущее состояние IPv6"*) echo 0 ;;                 # leave IPv6 as it is
        *"Инициирована установка XKeen"*|*"Продолжить установку"*) echo 1 ;;
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
    # $@ = xkeen arguments (-i full install, -ux Xray core only)
    fifo="/tmp/cw-xkeen.in"
    rm -f "$fifo"
    mkfifo "$fifo" || { xk_fail "Не удалось создать канал для ответов установщику XKeen."; return 1; }
    : > "$XK_LOG"
    # No autoinstall_mode: it would take the newest Xray; the release question is answered
    # with the pinned version instead.
    xkeen "$@" < "$fifo" > "$XK_LOG" 2>&1 4>&- &
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

        # Output stopped growing: XKeen may be waiting for an answer. Its prompt itself is
        # usually invisible (see xkeen_answer), so the question is recognised by its header.
        if [ "$stable" -ge 2 ] && [ "$size" != "$answered_at" ]; then
            ctx="$(tail -c +$((answered_at + 1)) "$XK_LOG" | strip_ansi | tail -n 25)"
            last="$(printf '%s\n' "$ctx" | grep -v '^[[:space:]]*$' | tail -n 1)"
            ans=""
            unknown=""
            # «Некорректный ввод» after one of our answers: XKeen rejected it (menu changed).
            if [ "$answers" -gt 0 ] && printf '%s' "$ctx" | grep -q "$XK_NOISE"; then
                kill_tree "$xk_pid"
                exec 3>&-
                tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
                xk_fail "XKeen не принял ответ установщика: «$(printf '%s' "$ctx" | grep -v '^[[:space:]]*$' | tail -n 3 | tr '\n' ' ')». Подробности отправлены в админ-панель."; return 1
            fi
            ans="$(xkeen_answer "$ctx")"
            if [ -z "$ans" ]; then
                # A visible prompt («…: ») or a menu (last line «  N. …») that has sat silent for
                # 30 s is a question this installer doesn't know — say so now, with its text.
                if [ "$(tail -c 2 "$XK_LOG" 2>/dev/null)" = ": " ]; then
                    unknown=1
                elif [ "$stable" -ge 30 ] && printf '%s' "$last" | grep -q '^[[:space:]]*[0-9][0-9]*\. '; then
                    unknown=1
                fi
            fi
            if [ -n "$unknown" ] || { [ -n "$ans" ] && [ "$answers" -ge 20 ]; }; then
                kill_tree "$xk_pid"
                exec 3>&-
                printf '%s\n' "$ctx" >> "$LOG"
                xk_fail "XKeen задал вопрос, на который у установщика нет ответа: «$(printf '%s' "$ctx" | grep -v '^[[:space:]]*$' | tail -n 6 | tr '\n' ' ')». Текст вопроса отправлен в админ-панель."; return 1
            fi
            if [ -n "$ans" ]; then
                echo "$ans" >&3
                answers=$((answers + 1))
                answered_at="$size"
                q="$(printf '%s\n' "$ctx" | grep -v '^[[:space:]]*$' | grep -v '^[[:space:]]*[0-9][0-9]*\. ' | tail -n 1 | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
                printf '    %s → %s\n' "$q" "$ans"
                echo ">>> answer: $ans" >> "$LOG"
            fi
        fi

        # XKeen rejected an answer (its menu changed): stop now, not after 15 minutes.
        if [ "$(tail -c +$((answered_at + 1)) "$XK_LOG" 2>/dev/null | grep -c "$XK_NOISE")" -ge 2 ]; then
            kill_tree "$xk_pid"
            exec 3>&-
            tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
            xk_fail "XKeen не принял ответ установщика (изменились вопросы). Подробности отправлены в админ-панель."; return 1
        fi
        # Silence watchdog: nothing new from XKeen for 5 minutes — a download frozen by the ISP
        # or a question this installer can't recognise. Stop and show where it stuck.
        if [ "$stable" -ge "$XK_SILENT_LIMIT" ]; then
            kill_tree "$xk_pid"
            exec 3>&-
            tail -c 3000 "$XK_LOG" | strip_ansi >> "$LOG"
            xk_fail "XKeen ничего не выводит $((XK_SILENT_LIMIT / 60)) минут — зависла загрузка или вопрос без ответа. Последнее: «$(tail -c 600 "$XK_LOG" | strip_ansi | grep -v '^[[:space:]]*$' | tail -n 2 | tr '\n' ' ')». Подробности отправлены в админ-панель."; return 1
        fi
        if [ "$waited" -ge 1200 ]; then
            kill_tree "$xk_pid"
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
        if run_xkeen_install -ux && [ "v$(/opt/sbin/xray version 2>/dev/null | head -n 1 | awk '{print $2}')" = "$XRAY_VERSION" ]; then
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
    run_limited 300 "загрузка XKeen" sh /tmp/xkeen-install.sh --legacy "$XKEEN_VERSION" >> "$LOG" 2>&1 </dev/null \
        || fail xkeen "Не удалось скачать XKeen $XKEEN_VERSION (ошибка или больше 5 минут — GitHub недоступен у этого провайдера). Подробности: $LOG"

    run_xkeen_install -i

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
    run_limited 300 "установка cron" opkg install cron >> "$LOG" 2>&1 </dev/null || fail cron "Не удалось установить cron."
    cron_file="/opt/etc/crontabs/root"; cron_init="/opt/etc/init.d/S10cron"
fi
for f in /opt/var/spool/cron/crontabs/root /opt/etc/crontabs/root; do
    [ -f "$f" ] && sed -i '/cipherway-agent/d' "$f"
done
mkdir -p "$(dirname "$cron_file")"
echo "$cron_line" >> "$cron_file"
run_limited 60 "перезапуск cron" "$cron_init" restart >> "$LOG" 2>&1 </dev/null \
    || run_limited 60 "запуск cron" "$cron_init" start >> "$LOG" 2>&1 </dev/null
pidof crond >/dev/null 2>&1 || fail cron "cron не запустился ($cron_init)."
ok cron "каждые 5 минут ($cron_file)"

# --- 7. first run ----------------------------------------------------------------------

say "Первый запуск агента и Xray"
run_limited 300 "первый запуск агента" "$AGENT_DIR/agent.sh" </dev/null \
    || warn "первый запуск агента завершился с ошибкой или дольше 5 минут — cron повторит через 5 минут"
if ! pidof xray >/dev/null 2>&1; then
    run_limited 120 "запуск XKeen" xkeen -start >> "$LOG" 2>&1 </dev/null || true
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
