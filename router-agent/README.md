# CipherWay router agent

Keenetic routers (Entware + XKeen) are installed with one command and then managed entirely
from the admin panel. Russian technician guide: [README.ru.md](README.ru.md).

- `install.sh` — served at `/api/agent/install.sh`. Preflight (Entware, arch, Netfilter
  modules, free space, token via `/api/agent/whoami`), packages, XKeen 2.0 + Xray (answers
  XKeen's interactive installer for that pinned version, with a watchdog), baseline settings
  (`routeOnly: false`, Xray error log), the agent, its cron job (in whichever crontab the
  router's crond actually reads), a first run, and an install report.
- `agent.sh` — served at `/api/agent/agent.sh`; runs from cron every minute (a light
  "has the config changed?" check; the full cycle still runs every 5 minutes). Fetches
  `/api/agent/config` (outbounds + the split-tunnel routing/DNS lifted from the Happ
  Xray-JSON subscription), keeps a new config only if `xray run -test` accepts it and Xray
  comes back up after the restart, self-updates when `X-Agent-Version` changes, and sends a
  heartbeat with diagnostics (XKeen ports, routeOnly, DNS probe, cron) shown in the panel.
  v7 adds a journal of its own actions (config applied, watchdog stop/resume, self-update,
  rollback) and an opt-in «game pulse» (`/api/agent/pulse`, off by default): response time
  over an already-open connection (the second of two HEADs in one curl) to a few fixed
  game-region addresses, direct and through each server's test port.
  The agent doesn't capture clients' traffic or read conntrack/DNS; the only place their
  activity can show up is the 15-line Xray error-log tail sent for diagnostics (failed
  connections may name a site; LAN addresses are masked) — see «Что собирает агент» in
  [README.ru.md](README.ru.md).
- `agent.conf.example` — the file the installer writes to `/opt/etc/cipherway-agent/agent.conf`.

To ship an agent change to every router: bump `AGENT_VERSION` in `agent.sh` and deploy.

## Agent versions only go forward

Never roll the server back to an agent below v8: v7 has no light check while the cron line is already `*/1`, so it would run a full cycle every minute. Fix forward (v9, v10…). As a last resort v8 puts cron back to `*/5` before installing a version below 8. A new agent goes to a few routers first: admin «Роутеры → 🧪 Агент v8» (ids, or `*` for all).
