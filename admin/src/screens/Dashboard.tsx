/* Screen 01 — Обзор: the business at a glance for a chosen period — revenue (hero + daily
   columns + split by product and payment method), key stat tiles with trends, acquisition
   sources, the router fleet, support load, system health and the audit feed. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router";

import { api, bytesFmt, dtTime, money } from "../api/client";
import { ColumnChart, RankBars, StackedBar, useCountUp } from "../components/charts";
import { Delta, Seg, Stat, pctChange } from "../components/ui";
import { useApp } from "../state/app";

type Period = "7" | "30" | "90";

type Dash = {
  days: number;
  revenue_today_minor: number;
  revenue_yesterday_minor: number;
  revenue: {
    series: { date: string; amount_minor: number }[];
    current_minor: number;
    previous_minor: number;
    orders: number;
    payers: number;
    avg_check_minor: number;
    by_product: { app: number; router: number; premium: number; topup: number };
    by_method: { name: string; amount_minor: number; count: number }[];
  };
  active_subscriptions: number;
  paid_active_subscriptions: number;
  expiring_7d: number;
  expired_period: number;
  total_users: number;
  new_users_24h: number;
  new_trials_24h: number;
  new_users: { series: number[]; current: number; previous: number };
  trial_conversion: { used: number; converted: number; pct: number };
  online_now: number;
  routers: { total: number; online: number; offline: number; pending: number };
  tickets: { open: number; premium_open: number };
  events: { id: number; at: string | null; actor: string; action: string; entity: string | null }[];
  sources: { total: number; campaigns: number; referrals: number; organic: number };
};

type SystemInfo = {
  redis: string;
  db_size_bytes: number | null;
  maintenance_mode: boolean;
  backup_enabled: boolean;
  backup_time: string;
  panel: { status: string; version?: string; detail?: string };
};

const PERIOD_LABEL: Record<Period, string> = { "7": "7 дней", "30": "30 дней", "90": "90 дней" };

const METHOD_NAMES: Record<string, string> = {
  manual: "Вручную / наличные",
  telegram_stars: "Telegram Stars",
  balance: "С баланса",
};

const VERBS: Record<string, [string, string]> = {
  create: ["+", "создание"],
  patch: ["✎", "изменение"],
  delete: ["✕", "удаление"],
  block: ["⛔", "блокировка"],
  save: ["✓", "сохранение"],
  sync: ["⟳", "синхронизация"],
  login: ["→", "вход"],
  paid: ["₽", "оплата"],
  reply: ["💬", "ответ"],
};

function eventGlyph(action: string): [string, string] {
  const suffix = action.split(".").pop() ?? "";
  return VERBS[suffix] ?? ["•", action];
}

function readPeriod(): Period {
  try {
    const v = localStorage.getItem("dash_period");
    if (v === "7" || v === "30" || v === "90") return v;
  } catch {
    /* storage unavailable — default below */
  }
  return "30";
}

function Dot({ ok }: { ok: boolean | null }) {
  return <span className={`status-dot ${ok === null ? "" : ok ? "ok" : "err"}`} />;
}

export default function Dashboard() {
  const { t, toast } = useApp();
  const nav = useNavigate();
  const qc = useQueryClient();
  const [syncing, setSyncing] = useState(false);
  const [period, setPeriodRaw] = useState<Period>(readPeriod);

  function setPeriod(p: Period) {
    setPeriodRaw(p);
    try {
      localStorage.setItem("dash_period", p);
    } catch {
      /* not critical */
    }
  }

  const dash = useQuery({
    queryKey: ["dashboard", period],
    queryFn: () => api.get<Dash>(`/api/admin/dashboard?days=${period}`),
    refetchInterval: 30_000,
    placeholderData: (prev) => prev,
  });
  const sys = useQuery({
    queryKey: ["system"],
    queryFn: () => api.get<SystemInfo>("/api/admin/dashboard/system"),
    refetchInterval: 60_000,
  });

  const backup = useMutation({
    mutationFn: () => api.post("/api/admin/maintenance/backup"),
    onSuccess: () => toast(t.quickBackup + " ✓"),
    onError: (e) => toast(e.message),
  });

  async function syncPanel() {
    setSyncing(true);
    try {
      const res = await api.post<{ synced: number }>("/api/admin/servers/sync");
      toast(`${t.syncRemnawave}: ${res.synced} ${t.nodes}`);
      void qc.invalidateQueries({ queryKey: ["system"] });
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSyncing(false);
    }
  }

  const d = dash.data;
  const s = sys.data;
  const rev = d?.revenue;
  const heroValue = useCountUp(rev?.current_minor ?? 0, 900);
  const now = new Date();
  const periodWord = PERIOD_LABEL[period];

  return (
    <div style={{ opacity: dash.isFetching && dash.isPlaceholderData ? 0.6 : 1, transition: "opacity .2s" }}>
      <div className="page-head">
        <div>
          <h1 className="h1">{t.overview}</h1>
          <div className="dim sub" style={{ fontSize: 12.5 }}>
            {now.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" })} ·
            обновлено{" "}
            {now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}
          </div>
        </div>
        <div className="actions">
          <button className="btn secondary" onClick={() => backup.mutate()}>
            {t.backupNow}
          </button>
          <button className="btn primary" onClick={() => nav("/broadcasts")}>
            {t.createBroadcast}
          </button>
        </div>
      </div>

      <div className="filters">
        <Seg
          value={period}
          options={(["7", "30", "90"] as Period[]).map((p) => ({ id: p, label: PERIOD_LABEL[p] }))}
          onChange={setPeriod}
        />
      </div>

      {/* Revenue hero */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row" style={{ alignItems: "flex-start", flexWrap: "wrap", gap: 24 }}>
          <div style={{ minWidth: 220 }}>
            <div className="caps">Выручка за {periodWord}</div>
            <div className="hero" style={{ margin: "8px 0 6px" }}>
              {rev ? money(Math.round(heroValue / 100) * 100) : <span className="sk" style={{ width: 180 }} />}
            </div>
            {rev && (
              <Delta
                pct={pctChange(rev.current_minor, rev.previous_minor)}
                label="к прошлому периоду"
              />
            )}
          </div>
          <div
            style={{
              flex: 1,
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
              gap: 14,
              minWidth: 260,
            }}
          >
            {(
              [
                ["Сегодня", d ? money(d.revenue_today_minor) : null],
                ["Вчера", d ? money(d.revenue_yesterday_minor) : null],
                ["Оплат", rev ? rev.orders.toLocaleString("ru-RU") : null],
                ["Средний чек", rev ? money(rev.avg_check_minor) : null],
                ["Платящих клиентов", rev ? rev.payers.toLocaleString("ru-RU") : null],
              ] as [string, string | null][]
            ).map(([label, value]) => (
              <div key={label}>
                <div className="dim" style={{ fontSize: 12 }}>
                  {label}
                </div>
                <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>
                  {value ?? <span className="sk" />}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div style={{ marginTop: 22 }}>
          {rev && (
            <ColumnChart
              points={rev.series.map((x) => ({ date: x.date, value: x.amount_minor / 100 }))}
              format={(v) => money(Math.round(v * 100))}
              unit="₽"
            />
          )}
        </div>
      </div>

      {/* Stat tiles */}
      <div className="kpis">
        <Stat
          icon="✅"
          label="Активные подписки"
          value={d ? d.active_subscriptions : null}
          note={d ? `платных ${d.paid_active_subscriptions.toLocaleString("ru-RU")}` : undefined}
          onClick={() => nav("/users")}
        />
        <Stat
          icon="👥"
          label={`Новые пользователи · ${periodWord}`}
          value={d ? d.new_users.current : null}
          delta={d ? pctChange(d.new_users.current, d.new_users.previous) : null}
          note={d ? `за 24 ч: ${d.new_users_24h}` : undefined}
          spark={d?.new_users.series}
        />
        <Stat
          icon="🎯"
          label="Конверсия триала"
          value={d ? d.trial_conversion.pct : null}
          format={(v) => `${v.toFixed(1).replace(".", ",")}%`}
          note={d ? `${d.trial_conversion.converted} из ${d.trial_conversion.used}` : undefined}
        />
        <Stat
          icon="🟢"
          label="Онлайн сейчас"
          value={d ? d.online_now : null}
          note="по данным Remnawave"
        />
        <Stat
          icon="⏳"
          label="Истекают за 7 дней"
          value={d ? d.expiring_7d : null}
          note="платные, ещё не продлены"
          onClick={() => nav("/reminders")}
        />
        <Stat
          icon="📉"
          label={`Истекли · ${periodWord}`}
          value={d ? d.expired_period : null}
          note="кандидаты на возврат"
        />
      </div>

      <div className="grid2">
        <div className="card">
          <div className="card-head">
            <span className="card-title">Выручка по направлениям</span>
            <span className="dim" style={{ fontSize: 12 }}>
              {periodWord}
            </span>
          </div>
          {rev ? (
            <StackedBar
              parts={[
                { key: "app", label: "Приложение (VPN)", value: rev.by_product.app },
                { key: "router", label: "Роутеры", value: rev.by_product.router },
                { key: "premium", label: "Премиум-серверы", value: rev.by_product.premium },
                { key: "topup", label: "Пополнения баланса", value: rev.by_product.topup },
              ]}
              format={(v) => money(v)}
            />
          ) : (
            <span className="sk" style={{ width: "100%" }} />
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <span className="card-title">Способы оплаты</span>
            <span className="dim" style={{ fontSize: 12 }}>
              {periodWord}
            </span>
          </div>
          {rev ? (
            <RankBars
              rows={rev.by_method.map((m) => ({
                label: METHOD_NAMES[m.name] ?? m.name,
                value: m.amount_minor,
                hint: `${m.count} опл.`,
              }))}
              format={(v) => money(v)}
              empty="Оплат за период не было"
            />
          ) : (
            <span className="sk" style={{ width: "100%" }} />
          )}
        </div>
      </div>

      <div className="cols">
        <div className="main-col">
          <div className="card">
            <div className="card-head">
              <span className="card-title">Откуда приходят пользователи</span>
              <span className="dim" style={{ fontSize: 12 }}>
                {d ? `${d.sources.total.toLocaleString("ru-RU")} новых · ${periodWord}` : ""}
              </span>
            </div>
            {d ? (
              <StackedBar
                parts={[
                  { key: "organic", label: "Сами пришли", value: d.sources.organic },
                  { key: "referrals", label: "По приглашению", value: d.sources.referrals },
                  { key: "campaigns", label: "Из рекламы (UTM)", value: d.sources.campaigns },
                ]}
                format={(v) => v.toLocaleString("ru-RU")}
              />
            ) : (
              <span className="sk" style={{ width: "100%" }} />
            )}
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">{t.lastEvents}</span>
            </div>
            <div className="grid" style={{ gap: 2 }}>
              {(d?.events ?? []).slice(0, 12).map((e) => {
                const [glyph, verb] = eventGlyph(e.action);
                return (
                  <div
                    key={e.id}
                    className="row"
                    style={{ fontSize: 13, padding: "6px 0", borderBottom: "1px solid var(--border)" }}
                  >
                    <span
                      style={{
                        width: 26,
                        height: 26,
                        borderRadius: 8,
                        background: "var(--pill)",
                        display: "grid",
                        placeItems: "center",
                        flex: "0 0 auto",
                        fontSize: 12,
                      }}
                    >
                      {glyph}
                    </span>
                    <span style={{ minWidth: 0, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      <b style={{ fontWeight: 500 }}>{e.actor}</b>{" "}
                      <span className="muted">
                        {verb} · {e.action}
                        {e.entity ? ` · ${e.entity}` : ""}
                      </span>
                    </span>
                    <span className="dim" style={{ fontSize: 11.5, flex: "0 0 auto" }}>
                      {dtTime(e.at)}
                    </span>
                  </div>
                );
              })}
              {d && d.events.length === 0 && <span className="dim">—</span>}
            </div>
          </div>
        </div>

        <div className="side-col">
          <div className="card link" onClick={() => nav("/routers")}>
            <div className="card-head">
              <span className="card-title">📡 Роутеры</span>
              <span className="dim" style={{ fontSize: 12 }}>
                {d ? `всего ${d.routers.total}` : ""}
              </span>
            </div>
            <div className="kv">
              <div>
                <span>
                  <Dot ok={true} />
                  Онлайн
                </span>
                <b>{d?.routers.online ?? "…"}</b>
              </div>
              <div>
                <span>
                  <Dot ok={false} />
                  Оффлайн
                </span>
                <b>{d?.routers.offline ?? "…"}</b>
              </div>
              <div>
                <span>
                  <Dot ok={null} />
                  Ждут установки
                </span>
                <b>{d?.routers.pending ?? "…"}</b>
              </div>
            </div>
          </div>

          <div className="card link" onClick={() => nav("/tickets")}>
            <div className="card-head">
              <span className="card-title">🎫 Поддержка</span>
            </div>
            <div className="kv">
              <div>
                <span className="muted">Открытые обращения</span>
                <b>{d?.tickets.open ?? "…"}</b>
              </div>
              <div>
                <span className="muted">💎 Из них премиум</span>
                <b>{d?.tickets.premium_open ?? "…"}</b>
              </div>
            </div>
          </div>

          <div className="card">
            <div className="card-head">
              <span className="card-title">{t.system}</span>
            </div>
            <div className="kv">
              <div>
                <span className="muted">
                  <Dot ok={s ? s.panel.status === "ok" : null} />
                  Remnawave
                </span>
                <span>{s ? (s.panel.status === "ok" ? (s.panel.version ?? "OK") : "ошибка") : "…"}</span>
              </div>
              <div>
                <span className="muted">
                  <Dot ok={s ? s.redis === "ok" : null} />
                  Redis
                </span>
                <span>{s ? (s.redis === "ok" ? "OK" : "ошибка") : "…"}</span>
              </div>
              <div>
                <span className="muted">{t.dbSize}</span>
                <span>{s?.db_size_bytes ? bytesFmt(s.db_size_bytes) : "—"}</span>
              </div>
              <div>
                <span className="muted">{t.autoBackup}</span>
                <span>{s ? (s.backup_enabled ? `✓ ${s.backup_time}` : "выключен") : "…"}</span>
              </div>
              <div>
                <span className="muted">{t.maintenanceMode}</span>
                <span className={`st ${s?.maintenance_mode ? "mid" : "off"}`}>
                  {s?.maintenance_mode ? t.on : t.off}
                </span>
              </div>
            </div>
            <hr className="sep" />
            <div className="grid" style={{ gap: 8 }}>
              <button className="btn secondary" onClick={syncPanel} disabled={syncing}>
                {syncing ? <span className="spin">⟳</span> : "⟳"} {t.syncRemnawave}
              </button>
              <button className="btn secondary" onClick={() => nav("/promos")}>
                {t.createPromo}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
