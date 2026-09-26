/* Screen — Статистика: growth and product health for a chosen period — new users and
   subscription purchases per day (two separate charts, never one dual-axis), user and
   subscription breakdowns, purchase mix and the plans customers actually run. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { ColumnChart, RankBars, StackedBar } from "../components/charts";
import { Seg, Stat } from "../components/ui";
import { useApp } from "../state/app";

type Period = "7" | "30" | "90";

type Stats = {
  days: number;
  users: {
    total: number;
    new_today: number;
    new_week: number;
    new_month: number;
    new_period: number;
    with_sub: number;
    without_sub: number;
    with_trial: number;
    blocked: number;
    bot_blocked: number;
    chart: { date: string; count: number }[];
  };
  subs: {
    active: number;
    disabled: number;
    expired: number;
    trial: number;
    unlimited_traffic: number;
    limited_traffic: number;
    expired_period: number;
    top_plans: { name: string; category: string; count: number }[];
  };
  sales: {
    chart: { date: string; count: number }[];
    total: number;
    kinds: { new: number; renew: number; change: number };
  };
};

const PERIOD_LABEL: Record<Period, string> = { "7": "7 дней", "30": "30 дней", "90": "90 дней" };
const CATEGORY: Record<string, string> = { app: "приложение", router: "роутер", premium: "премиум" };

const n = (v: number) => Math.round(v).toLocaleString("ru-RU");

function readPeriod(): Period {
  try {
    const v = localStorage.getItem("stats_period");
    if (v === "7" || v === "30" || v === "90") return v;
  } catch {
    /* storage unavailable — default below */
  }
  return "30";
}

export default function StatsScreen() {
  const { t } = useApp();
  const [period, setPeriodRaw] = useState<Period>(readPeriod);
  function setPeriod(p: Period) {
    setPeriodRaw(p);
    try {
      localStorage.setItem("stats_period", p);
    } catch {
      /* not critical */
    }
  }
  const q = useQuery({
    queryKey: ["stats", period],
    queryFn: () => api.get<Stats>(`/api/admin/stats?days=${period}`),
    refetchInterval: 60_000,
    placeholderData: (prev) => prev,
  });
  const d = q.data;
  const u = d?.users;
  const s = d?.subs;
  const sales = d?.sales;
  const days = Number(period);
  const word = PERIOD_LABEL[period];

  return (
    <div style={{ opacity: q.isFetching && q.isPlaceholderData ? 0.6 : 1, transition: "opacity .2s" }}>
      <div className="page-head">
        <h1 className="h1">{t.statsTitle}</h1>
      </div>

      <div className="filters">
        <Seg
          value={period}
          options={(["7", "30", "90"] as Period[]).map((p) => ({ id: p, label: PERIOD_LABEL[p] }))}
          onChange={setPeriod}
        />
      </div>

      <div className="grid2">
        <div className="card">
          <div className="card-head">
            <span className="card-title">Новые пользователи</span>
            <span className="dim" style={{ fontSize: 12 }}>
              {u ? `${n(u.new_period)} за ${word} · ≈${(u.new_period / days).toFixed(1).replace(".", ",")} в день` : ""}
            </span>
          </div>
          {u ? (
            <ColumnChart
              points={u.chart.map((x) => ({ date: x.date, value: x.count }))}
              format={(v) => `${n(v)} чел.`}
            />
          ) : (
            <span className="sk" style={{ width: "100%", height: 170 }} />
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <span className="card-title">Покупки подписок</span>
            <span className="dim" style={{ fontSize: 12 }}>
              {sales ? `${n(sales.total)} за ${word}` : ""}
            </span>
          </div>
          {sales ? (
            <ColumnChart
              points={sales.chart.map((x) => ({ date: x.date, value: x.count }))}
              format={(v) => `${n(v)} покуп.`}
            />
          ) : (
            <span className="sk" style={{ width: "100%", height: 170 }} />
          )}
        </div>
      </div>

      <div className="caps" style={{ margin: "8px 2px 10px" }}>
        {t.statsUsers}
      </div>
      <div className="kpis">
        <Stat icon="👥" label={t.statsTotal} value={u ? u.total : null} />
        <Stat
          icon="✨"
          label={t.statsNew}
          value={u ? u.new_today : null}
          note={u ? `неделя ${n(u.new_week)} · месяц ${n(u.new_month)}` : undefined}
          spark={u?.chart.map((x) => x.count)}
        />
        <Stat
          icon="✅"
          label={t.statsWithSub}
          value={u ? u.with_sub : null}
          note={u && u.total ? `${Math.round((u.with_sub / u.total) * 100)}% базы` : undefined}
        />
        <Stat icon="🎁" label={t.statsWithTrial} value={u ? u.with_trial : null} />
        <Stat icon="⛔" label={t.statsBlocked} value={u ? u.blocked : null} />
        <Stat icon="🔕" label={t.statsBotBlocked} value={u ? u.bot_blocked : null} note="остановили бота" />
      </div>

      <div className="caps" style={{ margin: "8px 2px 10px" }}>
        {t.statsSubs}
      </div>
      <div className="kpis">
        <Stat icon="🟢" label={t.active} value={s ? s.active : null} />
        <Stat icon="🎁" label={t.trial} value={s ? s.trial : null} />
        <Stat icon="⌛" label={t.expired} value={s ? s.expired : null} note={s ? `за ${word}: ${n(s.expired_period)}` : undefined} />
        <Stat icon="⏸" label={t.statsDisabled} value={s ? s.disabled : null} />
        <Stat icon="∞" label={t.statsUnlimited} value={s ? s.unlimited_traffic : null} />
        <Stat icon="📦" label={t.statsLimited} value={s ? s.limited_traffic : null} />
      </div>

      <div className="grid2">
        <div className="card">
          <div className="card-head">
            <span className="card-title">Из чего состоят покупки</span>
            <span className="dim" style={{ fontSize: 12 }}>
              {word}
            </span>
          </div>
          {sales ? (
            <StackedBar
              parts={[
                { key: "new", label: "Новые подписки", value: sales.kinds.new },
                { key: "renew", label: "Продления", value: sales.kinds.renew },
                { key: "change", label: "Смена тарифа", value: sales.kinds.change },
              ]}
              format={(v) => n(v)}
              empty="Покупок за период не было"
            />
          ) : (
            <span className="sk" style={{ width: "100%" }} />
          )}
        </div>
        <div className="card">
          <div className="card-head">
            <span className="card-title">База пользователей</span>
            <span className="dim" style={{ fontSize: 12 }}>
              сейчас
            </span>
          </div>
          {u ? (
            <StackedBar
              parts={[
                { key: "paid", label: "Платная подписка", value: Math.max(0, u.with_sub - u.with_trial) },
                { key: "trial", label: "Пробный период", value: u.with_trial },
                { key: "none", label: "Без подписки", value: u.without_sub },
              ]}
              format={(v) => n(v)}
            />
          ) : (
            <span className="sk" style={{ width: "100%" }} />
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <span className="card-title">Популярные тарифы</span>
          <span className="dim" style={{ fontSize: 12 }}>
            активные платные подписки
          </span>
        </div>
        {s ? (
          <RankBars
            rows={s.top_plans.map((p) => ({
              label: p.name,
              value: p.count,
              hint: CATEGORY[p.category] ?? p.category,
            }))}
            format={(v) => n(v)}
            empty="Активных платных подписок пока нет"
          />
        ) : (
          <span className="sk" style={{ width: "100%" }} />
        )}
      </div>
    </div>
  );
}
