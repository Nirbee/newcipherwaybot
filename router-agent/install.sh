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

INSTALLER_VERSION="1"
XKEEN_VERSION="2.0"
AGENT_DIR="/opt/etc/cipherway-agent"
CONFDIR="/opt/etc/xray/configs"
LOG="/opt/var/log/cipherway-install.log"
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
    body="$(jq -n \
        --arg installer_version "$INSTALLER_VERSION" \
        --arg xkeen_version "$XKEEN_VERSION" \
        --arg steps "$STEPS" \
        --arg failed "$FAILED" \
        --arg arch "$(opkg print-architecture 2>/dev/null | awk '/aarch64|mips|arm/ {print $2}' | tr '\n' ' ')" \
        --arg opt_device "$(mount | awk '$3 == "/opt" {print $1}')" \
        --arg log_tail "$(tail -n 40 "$LOG" 2>/dev/null)" \
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
    200) ok token "роутер «$(jq -r '.label // "?"' /tmp/cw-whoami.json)», клиент $(jq -r '.client // "?"' /tmp/cw-whoami.json)" ;;
    401|403) fail token "Сервер не принял токен (HTTP $code). Скопируйте команду из админки заново или выпустите новый токен." ;;
    *) fail token "Сервер $API_BASE недоступен (HTTP $code). Проверьте интернет на роутере." ;;
esac

# --- 3. XKeen + Xray -------------------------------------------------------------------

if command -v xkeen >/dev/null 2>&1 && [ -x /opt/sbin/xray ] && [ -f /opt/etc/init.d/S05xkeen ]; then
    say "XKeen уже установлен — пропускаю установку"
    ok xkeen "уже был"
else
    say "Установка XKeen $XKEEN_VERSION и Xray (несколько минут)"
    cd /tmp || true
    curl -fsSL -m 120 https://raw.githubusercontent.com/jameszeroX/XKeen/main/install.sh \
        -o /tmp/xkeen-install.sh 2>>"$LOG" \
        || fail xkeen "Не удалось скачать установщик XKeen с GitHub."
    sh /tmp/xkeen-install.sh --legacy "$XKEEN_VERSION" >> "$LOG" 2>&1 </dev/null \
        || fail xkeen "Установщик XKeen завершился с ошибкой. Подробности: $LOG"

    # Answers for `xkeen -i` (XKeen 2.0), in prompt order:
    #   [only if Entware is in internal memory] continue anyway -> 1
    #   proxy core -> 1 (Xray); Xray release -> auto (autoinstall_mode=true, no prompt)
    #   GeoSite -> 0, GeoIP -> 0 (our routing rules use plain domain/IP lists, no geo files)
    #   GeoIPSET -> no prompt without a TTY (installs RU-subnet exclusion)
    #   geofile auto-update -> 0; autostart on boot -> 1
    answers="/tmp/xkeen-answers.txt"
    : > "$answers"
    if mount | awk '$3 == "/opt" {print $1}' | grep -q '^/dev/ubi'; then
        echo 1 >> "$answers"
    fi
    printf '1\n0\n0\n0\n1\n' >> "$answers"

    autoinstall_mode=true xkeen -i < "$answers" >> "$LOG" 2>&1 &
    xk_pid=$!
    waited=0
    while kill -0 "$xk_pid" 2>/dev/null; do
        sleep 5
        waited=$((waited + 5))
        if [ "$waited" -ge 900 ]; then
            kill "$xk_pid" 2>/dev/null
            fail xkeen "Установка XKeen зависла (больше 15 минут) — вероятно, изменились вопросы установщика. Установите XKeen вручную: xkeen -i, затем запустите эту команду снова."
        fi
    done

    if [ ! -x /opt/sbin/xray ] || [ ! -f /opt/etc/init.d/S05xkeen ]; then
        fail xkeen "XKeen установился не полностью (нет /opt/sbin/xray или S05xkeen). Подробности: $LOG"
    fi
    ok xkeen "XKeen $XKEEN_VERSION и Xray установлены"
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
