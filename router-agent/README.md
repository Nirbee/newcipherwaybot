# CipherWay router agent

Keenetic routers (Entware + XKeen) are installed with one command and then managed entirely
from the admin panel. Russian technician guide: [README.ru.md](README.ru.md).

- `install.sh` — served at `/api/agent/install.sh`. Preflight (Entware, arch, Netfilter
  modules, free space, token via `/api/agent/whoami`), packages, XKeen 2.0 + Xray (answers
  XKeen's interactive installer for that pinned version, with a watchdog), baseline settings
  (`routeOnly: false`, Xray error log), the agent, its cron job (in whichever crontab the
  router's crond actually reads), a first run, and an install report.
- `agent.sh` — served at `/api/agent/agent.sh`; runs from cron every 5 minutes. Fetches
  `/api/agent/config` (outbounds + the split-tunnel routing/DNS lifted from the Happ
  Xray-JSON subscription), keeps a new config only if `xray run -test` accepts it and Xray
  comes back up after the restart, self-updates when `X-Agent-Version` changes, and sends a
  heartbeat with diagnostics (XKeen ports, routeOnly, DNS probe, cron) shown in the panel.
- `agent.conf.example` — the file the installer writes to `/opt/etc/cipherway-agent/agent.conf`.

To ship an agent change to every router: bump `AGENT_VERSION` in `agent.sh` and deploy.
