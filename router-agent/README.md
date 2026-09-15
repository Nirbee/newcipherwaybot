# CipherWay router agent (stage 6)

A small POSIX-sh script that runs on a customer's Keenetic router (Entware + XKeen) and keeps
its Xray config in sync with the bot. Polls `GET /api/agent/config`, writes the returned
outbounds/routing fragment into XKeen's config directory, restarts Xray when it changes, and
reports back via `POST /api/agent/heartbeat` so the admin panel shows the device as online.

There is **no automated installer yet** (that's stage 7 — deliberately deferred until we've done
a few of these by hand and know the rough edges). This is the manual install procedure.

## Prerequisites on the router

- Keenetic with Entware and XKeen already installed and working (i.e. `xkeen -status` runs and
  the router already gets internet through Xray some other way — this agent only manages the
  *outbounds* config, not the XKeen install itself).
- `curl` and `jq` from Entware:
  ```
  opkg update && opkg install curl jq
  ```

## Install

1. Copy `agent.sh` to `/opt/etc/cipherway-agent/agent.sh` on the router and `chmod +x` it.
2. Copy `agent.conf.example` to `/opt/etc/cipherway-agent/agent.conf`.
3. In the admin panel: **Роутеры → создать** (or **rotate** on an existing device), pick the
   customer's subscription, and copy the token shown — it's shown once. Paste it into
   `agent.conf` as `TOKEN`.
4. Check where XKeen keeps its own config fragments on *this* router — the directory usually has
   a handful of `NN_*.json` files already:
   ```
   ls /opt/etc/xray/configs/
   ```
   Set `CONFDIR` in `agent.conf` to that path (adjust if this install uses a different path —
   we've seen it vary between XKeen versions).
5. Run it once by hand and watch the log:
   ```
   /opt/etc/cipherway-agent/agent.sh
   tail -n 50 /opt/var/log/cipherway-agent.log
   ```
   Expect a line like `config updated -> /opt/etc/xray/configs/10_cipherway.json (etag=...),
   restarting xray`. Confirm the router's internet still works and is going through the proxy
   (check the device's **status** flips to *online* with a `last_seen_at` in the admin panel
   within a few minutes).
6. Add the cron job (every 5 minutes — matches the server's rate limit):
   ```
   echo '*/5 * * * * /opt/etc/cipherway-agent/agent.sh >> /opt/var/log/cipherway-agent.cron.log 2>&1' >> /opt/etc/crontabs/root
   /opt/etc/init.d/S10cron restart
   ```

## What it does NOT do

- Doesn't install or configure XKeen/Xray itself, or set up inbounds/DNS/log — that's assumed
  already working on the router before this agent is installed.
- Doesn't touch the config if the panel is briefly unreachable (503) — old config stays applied
  rather than the router losing its proxy over a transient blip.
- Doesn't uninstall itself on revoke — a revoked token just gets a 401/403 forever; remove the
  cron line and delete `/opt/etc/cipherway-agent/` by hand if decommissioning a device.

## Troubleshooting

- `tail -f /opt/var/log/cipherway-agent.log` — the agent's own log.
- `curl -s -H "Authorization: Bearer $TOKEN" https://cabinet.cipherway.net.ru/api/agent/config`
  — fetch the config by hand to see exactly what the server would send.
- 401/403 in the log — token wrong or the device was revoked in the admin panel.
- Config applies but no internet — usually `CONFDIR`/`OUTFILE` isn't actually where XKeen reads
  from, or the restart command didn't fire; check `xkeen -status` after a manual run.
