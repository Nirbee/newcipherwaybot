/* Screen 02 — Пользователи: search + segment filter + table + 460px drawer. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";

import { api, bytesFmt, dt, dtTime, money } from "../api/client";
import { Drawer, Prog, Seg } from "../components/ui";
import { useApp } from "../state/app";

type Row = {
  id: number;
  telegram_id: number | null;
  username: string | null;
  name: string | null;
  status: string;
  balance_minor: number;
  plan_name: string | null;
  expire_at: string | null;
  traffic_used_bytes: number;
  traffic_limit_bytes: number;
  device_limit: number | null;
  created_at: string | null;
  last_seen_at: string | null;
};

type Detail = Row & {
  is_trial_available: boolean;
  referral_code: string;
  referral_invited: number;
  referral_earned_minor: number;
  personal_discount_pct: number;
  subscription: {
    short_id: string;
    status: string;
    subscription_url: string | null;
    device_limit: number | null;
  } | null;
  transactions: {
    id: number;
    type: string;
    status: string;
    amount_minor: number;
    gateway: string | null;
    created_at: string | null;
  }[];
};

type Counters = { all: number; active: number; trial: number; expired: number; blocked: number };
type TicketRow = { id: number; user_id: number; subject: string; status: string };

// label + sign as seen from the customer's balance/wallet
const TX_LABEL: Record<string, [string, string]> = {
  deposit: ["Пополнение баланса", "+"],
  gift: ["Начисление от администратора", "+"],
  referral_reward: ["Реферальное вознаграждение", "+"],
  refund: ["Возврат", "+"],
  withdrawal: ["Списание", "−"],
  subscription_payment: ["Оплата подписки", ""],
};
const TX_STATUS: Record<string, string> = {
  pending: "ожидает оплаты",
  canceled: "отменена",
  failed: "не прошла",
  refunded: "возвращена",
};

const ST_GLYPH: Record<string, [string, string]> = {
  active: ["●", "on"],
  trial: ["◐", "mid"],
  expired: ["○", "off"],
  blocked: ["✕", "off"],
  none: ["—", "off"],
};

function StatusCell({ status, t }: { status: string; t: Record<string, string> }) {
  const [g, cls] = ST_GLYPH[status] ?? ["—", "off"];
  const label =
    status === "active"
      ? t.activeOne
      : status === "trial"
        ? t.trial
        : status === "expired"
          ? t.expiredOne
          : status === "blocked"
            ? t.blocked
            : t.none;
  return (
    <span className={`st ${cls}`}>
      {g} {label}
    </span>
  );
}

export default function Users() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [filter, setFilter] = useState<"all" | "active" | "trial" | "expired" | "blocked">("all");
  const [selId, setSelId] = useState<number | null>(null);
  const [tab, setTab] = useState<"overview" | "finance" | "sub" | "tickets" | "actions">("overview");
  const [hwidInput, setHwidInput] = useState("");
  const [balAmount, setBalAmount] = useState("");
  const nav = useNavigate();
  const [extDays, setExtDays] = useState(""); // custom "+N days"
  const [extUntil, setExtUntil] = useState(""); // absolute expiry date (YYYY-MM-DD)
  const [grantPlan, setGrantPlan] = useState("");
  const [msgText, setMsgText] = useState(""); // DM to the customer
  const [discount, setDiscount] = useState(""); // tariff to hand out (not just days)
  const [grantDays, setGrantDays] = useState("30");

  useEffect(() => {
    const h = setTimeout(() => setQDebounced(q), 300);
    return () => clearTimeout(h);
  }, [q]);

  const counters = useQuery({
    queryKey: ["users", "counters"],
    queryFn: () => api.get<Counters>("/api/admin/users/counters"),
  });
  const list = useQuery({
    queryKey: ["users", qDebounced, filter],
    queryFn: () =>
      api.get<{ items: Row[]; total: number }>(
        `/api/admin/users?q=${encodeURIComponent(qDebounced)}&status=${filter}&limit=100`,
      ),
  });
  const detail = useQuery({
    queryKey: ["user", selId],
    queryFn: () => api.get<Detail>(`/api/admin/users/${selId}`),
    enabled: selId !== null,
  });
  // Tariffs for the "выдать подписку" picker (shares the cache with the Тарифы screen).
  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: () =>
      api.get<{ items: { id: number; name: string; is_trial: boolean }[] }>("/api/admin/plans"),
    enabled: selId !== null,
  });

  const ticketsQ = useQuery({
    queryKey: ["tickets"],
    queryFn: () => api.get<{ items: TicketRow[] }>("/api/admin/tickets"),
    enabled: selId !== null && tab === "tickets",
  });
  const userTickets = (ticketsQ.data?.items ?? []).filter((x) => x.user_id === selId);

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ["users"] });
    void qc.invalidateQueries({ queryKey: ["user", selId] });
  }

  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) =>
      api.post(`/api/admin/users/${selId}${path}`, body),
    onSuccess: (_data, variables) => {
      invalidate();
      // Balance changes surface their own toast (with an Undo action) via adjustBalance.
      if (variables.path !== "/balance") toast("✓");
    },
    onError: (e) => toast(e.message),
  });

  async function changeBalance(sign: 1 | -1) {
    const rub = Number(balAmount);
    if (!(rub > 0)) return;
    const amount_minor = Math.round(rub * 100) * sign;
    if (sign < 0 && !(await confirm(`Списать ${money(-amount_minor)} с баланса клиента?`))) return;
    try {
      await act.mutateAsync({ path: "/balance", body: { amount_minor } });
      setBalAmount("");
      toast(sign > 0 ? `Пополнено на ${money(amount_minor)}` : `Списано ${money(-amount_minor)}`, {
        label: t.undo,
        onClick: () => act.mutate({ path: "/balance", body: { amount_minor: -amount_minor } }),
      });
    } catch {
      /* act.onError already surfaced the message */
    }
  }

  function saveHwid() {
    const value = Number(hwidInput);
    if (!(value >= 1)) return;
    act.mutate({ path: "/hwid", body: { value } });
    setHwidInput("");
  }

  async function deleteUser() {
    if (!(await confirm(t.deleteUserConfirm))) return;
    try {
      await api.del(`/api/admin/users/${selId}`);
      setSelId(null);
      void qc.invalidateQueries({ queryKey: ["users"] });
      toast("✕");
    } catch (e) {
      // Staff accounts return 400 — surface the message.
      toast((e as Error).message);
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setSelId(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const c = counters.data;
  const d = detail.data;
  const cols = "2fr 1fr 1fr 1.4fr 0.8fr 1fr 0.9fr";

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.users}</h1>
        <div className="actions">
          <button
            className="btn secondary"
            onClick={() => {
              void (async () => {
                try {
                  const data = await api.get<{ items: Row[] }>(
                    `/api/admin/users?q=${encodeURIComponent(qDebounced)}&status=${filter}&limit=200`,
                  );
                  const head = ["id", "telegram_id", "username", "name", "status", "balance_minor", "plan_name", "created_at"];
                  const cell = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
                  const lines = [head.join(",")].concat(
                    data.items.map((r) =>
                      head.map((k) => cell((r as unknown as Record<string, unknown>)[k])).join(","),
                    ),
                  );
                  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = `users-${new Date().toISOString().slice(0, 10)}.csv`;
                  a.click();
                  URL.revokeObjectURL(a.href);
                  toast(t.exportCsv + " ✓");
                } catch (e) {
                  toast(String(e));
                }
              })();
            }}
          >
            {t.exportCsv}
          </button>
        </div>
      </div>

      <div className="row" style={{ marginBottom: 14, flexWrap: "wrap" }}>
        <input
          className="input"
          style={{ flex: "1 1 260px" }}
          placeholder={t.userSearchPh}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Seg
          value={filter}
          options={[
            { id: "all" as const, label: t.all, count: c?.all },
            { id: "active" as const, label: t.active, count: c?.active },
            { id: "trial" as const, label: t.trial, count: c?.trial },
            { id: "expired" as const, label: t.expired, count: c?.expired },
            { id: "blocked" as const, label: t.blocked, count: c?.blocked },
          ]}
          onChange={setFilter}
        />
      </div>

      <div className="tbl">
        <div className="tr head" style={{ gridTemplateColumns: cols }}>
          <span>{t.colUser}</span>
          <span>Telegram ID</span>
          <span>{t.colStatus}</span>
          <span>{t.colSub}</span>
          <span>{t.colBalance}</span>
          <span>{t.colTraffic}</span>
          <span>{t.colActivity}</span>
        </div>
        {(list.data?.items ?? []).map((u) => (
          <div
            key={u.id}
            className="tr click"
            style={{ gridTemplateColumns: cols }}
            onClick={() => {
              setSelId(u.id);
              setTab("overview");
            }}
          >
            <span className="row" style={{ gap: 10, minWidth: 0 }}>
              <span className="avatar-sq" style={{ flex: "0 0 auto" }}>
                {(u.name ?? u.username ?? "?").slice(0, 2).toUpperCase()}
              </span>
              <span style={{ minWidth: 0 }}>
                <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {u.username ? `@${u.username}` : `id${u.id}`}
                </div>
                <div className="dim" style={{ fontSize: 11.5 }}>
                  {u.name ?? "—"}
                </div>
              </span>
            </span>
            <span className="mono muted">{u.telegram_id ?? "—"}</span>
            <StatusCell status={u.status} t={t as unknown as Record<string, string>} />
            <span>
              {u.plan_name ?? "—"}
              {u.expire_at && (
                <div className="dim" style={{ fontSize: 11.5 }}>
                  {t.till} {dt(u.expire_at)}
                </div>
              )}
            </span>
            <span className="mono">{money(u.balance_minor)}</span>
            <span className="mono muted">
              {bytesFmt(u.traffic_used_bytes)} / {u.traffic_limit_bytes ? bytesFmt(u.traffic_limit_bytes) : "∞"}
            </span>
            <span className="dim" style={{ fontSize: 12 }}>
              {dtTime(u.last_seen_at)}
            </span>
          </div>
        ))}
        {list.data && list.data.items.length === 0 && (
          <div className="tr dim">{list.isFetching ? t.loading : "—"}</div>
        )}
      </div>

      {selId !== null && (
        <Drawer onClose={() => setSelId(null)}>
          {d ? (
            <div className="grid" style={{ gap: 14 }}>
              {/* header */}
              <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                <div className="avatar-sq" style={{ width: 46, height: 46, fontSize: 15, borderRadius: 12 }}>
                  {(d.name ?? d.username ?? "?").slice(0, 2).toUpperCase()}
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 17, fontWeight: 650, overflow: "hidden", textOverflow: "ellipsis" }}>
                    {d.username ? `@${d.username}` : d.name ?? `id${d.id}`}
                  </div>
                  <div className="row" style={{ gap: 8, marginTop: 2, flexWrap: "wrap" }}>
                    <StatusCell status={d.status} t={t as unknown as Record<string, string>} />
                    {d.telegram_id && (
                      <button
                        className="cap-pill"
                        style={{ cursor: "pointer" }}
                        title="Скопировать Telegram ID"
                        onClick={() => {
                          void navigator.clipboard.writeText(String(d.telegram_id));
                          toast(t.copied);
                        }}
                      >
                        TG {d.telegram_id}
                      </button>
                    )}
                    {d.name && d.username && <span className="dim" style={{ fontSize: 12 }}>{d.name}</span>}
                  </div>
                </div>
                {d.username && (
                  <a
                    className="icon-btn"
                    href={`https://t.me/${d.username}`}
                    target="_blank"
                    rel="noreferrer"
                    title="Открыть в Telegram"
                    style={{ textDecoration: "none" }}
                  >
                    ↗
                  </a>
                )}
                <button className="icon-btn" onClick={() => setSelId(null)} aria-label="Закрыть">
                  ✕
                </button>
              </div>

              {/* summary tiles */}
              <div className="ud-stats">
                <div>
                  <span className="dim">Подписка</span>
                  <b>{d.plan_name ?? "нет"}</b>
                  <span className="dim">{d.expire_at ? `до ${dt(d.expire_at)}` : "—"}</span>
                </div>
                <div>
                  <span className="dim">Баланс</span>
                  <b>{money(d.balance_minor)}</b>
                  <span className="dim">{d.personal_discount_pct ? `скидка ${d.personal_discount_pct}%` : " "}</span>
                </div>
                <div>
                  <span className="dim">Устройства</span>
                  <b>{d.subscription?.device_limit ?? "—"}</b>
                  <span className="dim">лимит HWID</span>
                </div>
              </div>

              <Seg
                value={tab}
                options={[
                  { id: "overview" as const, label: "Обзор" },
                  { id: "finance" as const, label: "Финансы" },
                  { id: "sub" as const, label: "Подписка" },
                  { id: "tickets" as const, label: "Тикеты" },
                  { id: "actions" as const, label: "Действия" },
                ]}
                onChange={setTab}
              />

              {tab === "overview" && (
                <div className="grid" style={{ gap: 12 }}>
                  <div className="ud-section">
                    <div className="ud-title">Подписка</div>
                    <div className="kv">
                      <div>
                        <span className="muted">Тариф</span>
                        <span>{d.plan_name ?? "—"}</span>
                      </div>
                      <div>
                        <span className="muted">Действует до</span>
                        <span>{d.expire_at ? dt(d.expire_at) : "—"}</span>
                      </div>
                      <div>
                        <span className="muted">Трафик</span>
                        <span>
                          {bytesFmt(d.traffic_used_bytes)} /{" "}
                          {d.traffic_limit_bytes ? bytesFmt(d.traffic_limit_bytes) : "∞"}
                        </span>
                      </div>
                    </div>
                    {d.traffic_limit_bytes > 0 && (
                      <div style={{ marginTop: 8 }}>
                        <Prog pct={(d.traffic_used_bytes / d.traffic_limit_bytes) * 100} />
                      </div>
                    )}
                  </div>

                  <div className="ud-section">
                    <div className="ud-title">Устройства (HWID)</div>
                    {d.subscription ? (
                      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                        <button
                          className="btn secondary sm"
                          disabled={act.isPending}
                          onClick={() => act.mutate({ path: "/hwid", body: { delta: -1 } })}
                        >
                          −1
                        </button>
                        <input
                          className="input num"
                          type="number"
                          min={1}
                          max={100}
                          style={{ width: 80 }}
                          value={hwidInput}
                          placeholder={String(d.subscription.device_limit ?? "")}
                          onChange={(e) => setHwidInput(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && Number(hwidInput) >= 1) saveHwid();
                          }}
                        />
                        <button
                          className="btn secondary sm"
                          disabled={act.isPending}
                          onClick={() => act.mutate({ path: "/hwid", body: { delta: 1 } })}
                        >
                          +1
                        </button>
                        <button
                          className="btn primary sm"
                          disabled={!(Number(hwidInput) >= 1) || act.isPending}
                          onClick={saveHwid}
                        >
                          Сохранить
                        </button>
                      </div>
                    ) : (
                      <span className="dim">Нет подписки</span>
                    )}
                  </div>

                  <div className="ud-section">
                    <div className="ud-title">Аккаунт</div>
                    <div className="kv">
                      <div>
                        <span className="muted">Регистрация</span>
                        <span>{dt(d.created_at)}</span>
                      </div>
                      <div>
                        <span className="muted">Последняя активность</span>
                        <span>{dtTime(d.last_seen_at)}</span>
                      </div>
                      <div>
                        <span className="muted">Пробный период</span>
                        <span>{d.is_trial_available ? "доступен" : "использован"}</span>
                      </div>
                      <div>
                        <span className="muted">Пригласил друзей</span>
                        <span>{d.referral_invited}</span>
                      </div>
                      <div>
                        <span className="muted">Заработал на рефералах</span>
                        <span>{money(d.referral_earned_minor)}</span>
                      </div>
                    </div>
                    <button
                      className="btn secondary sm"
                      style={{ marginTop: 10 }}
                      onClick={() => {
                        void navigator.clipboard.writeText(`ref_${d.referral_code}`);
                        toast(t.copied);
                      }}
                    >
                      Скопировать реф-код
                    </button>
                  </div>
                </div>
              )}

              {tab === "finance" && (
                <div className="grid" style={{ gap: 12 }}>
                  <div className="ud-section">
                    <div className="ud-title">Баланс</div>
                    <div className="hero" style={{ fontSize: 30, marginBottom: 12 }}>
                      {money(d.balance_minor)}
                    </div>
                    <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                      <input
                        className="input num"
                        type="number"
                        min={1}
                        placeholder="Сумма, ₽"
                        value={balAmount}
                        style={{ width: 130 }}
                        onChange={(e) => setBalAmount(e.target.value)}
                      />
                      <button
                        className="btn primary sm"
                        disabled={!(Number(balAmount) > 0) || act.isPending}
                        onClick={() => void changeBalance(1)}
                      >
                        + Пополнить
                      </button>
                      <button
                        className="btn danger sm"
                        disabled={!(Number(balAmount) > 0) || act.isPending}
                        onClick={() => void changeBalance(-1)}
                      >
                        − Списать
                      </button>
                    </div>
                    <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                      {[100, 500, 1000].map((v) => (
                        <button key={v} className="cap-pill" style={{ cursor: "pointer" }} onClick={() => setBalAmount(String(v))}>
                          {v} ₽
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="ud-section">
                    <div className="ud-title">Последние операции</div>
                    <div className="grid" style={{ gap: 0 }}>
                      {d.transactions.map((tx) => {
                        const [label, sign] = TX_LABEL[tx.type] ?? [tx.type, ""];
                        return (
                          <div
                            key={tx.id}
                            className="row"
                            style={{
                              justifyContent: "space-between",
                              padding: "8px 0",
                              borderBottom: "1px solid var(--border)",
                              fontSize: 13,
                            }}
                          >
                            <span style={{ minWidth: 0 }}>
                              <div>{label}</div>
                              <div className="dim" style={{ fontSize: 11.5 }}>
                                {dtTime(tx.created_at)}
                                {tx.status !== "completed" ? ` · ${TX_STATUS[tx.status] ?? tx.status}` : ""}
                              </div>
                            </span>
                            <b
                              style={{
                                fontWeight: 600,
                                color:
                                  tx.status !== "completed"
                                    ? "var(--dim)"
                                    : sign === "+"
                                      ? "var(--good-ink)"
                                      : undefined,
                              }}
                            >
                              {sign}
                              {money(tx.amount_minor)}
                            </b>
                          </div>
                        );
                      })}
                      {d.transactions.length === 0 && <span className="dim">Операций пока не было</span>}
                    </div>
                  </div>
                </div>
              )}

              {tab === "sub" && (
                <div className="grid" style={{ gap: 12 }}>
                  <div className="ud-section">
                    <div className="ud-title">Выдать подписку по тарифу</div>
                    <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                      <select
                        className="input"
                        value={grantPlan}
                        style={{ flex: "1 1 170px" }}
                        onChange={(e) => setGrantPlan(e.target.value)}
                      >
                        <option value="">Выберите тариф…</option>
                        {(plans.data?.items ?? [])
                          .filter((p) => !p.is_trial)
                          .map((p) => (
                            <option key={p.id} value={String(p.id)}>
                              {p.name}
                            </option>
                          ))}
                      </select>
                      <input
                        className="input num"
                        type="number"
                        min={1}
                        max={3650}
                        value={grantDays}
                        style={{ width: 80 }}
                        onChange={(e) => setGrantDays(e.target.value)}
                      />
                      <span className="dim">дн.</span>
                      <button
                        className="btn primary sm"
                        disabled={!grantPlan || !Number(grantDays) || act.isPending}
                        onClick={() =>
                          act.mutate({
                            path: "/grant",
                            body: { plan_id: Number(grantPlan), days: Number(grantDays) },
                          })
                        }
                      >
                        Выдать
                      </button>
                    </div>
                  </div>

                  <div className="ud-section">
                    <div className="ud-title">Срок подписки</div>
                    <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                      {[7, 30, 90].map((n) => (
                        <button
                          key={`+${n}`}
                          className="btn secondary sm"
                          disabled={act.isPending}
                          onClick={() => act.mutate({ path: "/extend", body: { days: n } })}
                        >
                          +{n} дн.
                        </button>
                      ))}
                      {[7, 30, 90].map((n) => (
                        <button
                          key={`-${n}`}
                          className="btn danger sm"
                          disabled={!d.expire_at || act.isPending}
                          title={!d.expire_at ? t.noExpiry : undefined}
                          onClick={() => {
                            // The absolute-date path of /extend handles a past date correctly
                            // (the relative `days` path is for real renewals only).
                            const target = new Date(d.expire_at!);
                            target.setUTCDate(target.getUTCDate() - n);
                            act.mutate({
                              path: "/extend",
                              body: { until: target.toISOString().slice(0, 10) },
                            });
                          }}
                        >
                          −{n}
                        </button>
                      ))}
                    </div>
                    <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
                      <input
                        className="input num"
                        type="number"
                        min={1}
                        max={3650}
                        placeholder="дней"
                        value={extDays}
                        style={{ width: 90 }}
                        onChange={(e) => setExtDays(e.target.value)}
                      />
                      <button
                        className="btn secondary sm"
                        disabled={!extDays || Number(extDays) < 1}
                        onClick={() => {
                          act.mutate({ path: "/extend", body: { days: Number(extDays) } });
                          setExtDays("");
                        }}
                      >
                        Продлить
                      </button>
                      <input
                        className="input"
                        type="date"
                        value={extUntil}
                        style={{ width: 150 }}
                        onChange={(e) => setExtUntil(e.target.value)}
                      />
                      <button
                        className="btn secondary sm"
                        disabled={!extUntil}
                        onClick={() => {
                          act.mutate({ path: "/extend", body: { until: extUntil } });
                          setExtUntil("");
                        }}
                      >
                        До даты
                      </button>
                    </div>
                  </div>

                  <div className="ud-section">
                    <div className="ud-title">Условия клиента</div>
                    <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                      <input
                        className="input num"
                        type="number"
                        min={0}
                        max={100}
                        placeholder="%"
                        value={discount}
                        style={{ width: 70 }}
                        onChange={(e) => setDiscount(e.target.value)}
                      />
                      <button
                        className="btn secondary sm"
                        disabled={discount === "" || act.isPending}
                        onClick={() => act.mutate({ path: "/discount", body: { percent: Number(discount) } })}
                      >
                        Задать скидку
                      </button>
                      <button
                        className="btn secondary sm"
                        onClick={() =>
                          act.mutate({ path: "/trial", body: { available: !d.is_trial_available } })
                        }
                      >
                        {d.is_trial_available ? "Забрать пробный период" : "Вернуть пробный период"}
                      </button>
                      <button className="btn secondary sm" onClick={() => act.mutate({ path: "/sync" })}>
                        ⟳ Синхронизировать с панелью
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {tab === "tickets" && (
                <div className="ud-section">
                  <div className="ud-title">Обращения в поддержку</div>
                  {userTickets.length ? (
                    <div className="grid" style={{ gap: 0 }}>
                      {userTickets.map((tk) => (
                        <button
                          key={tk.id}
                          className="row"
                          style={{
                            justifyContent: "space-between",
                            padding: "9px 0",
                            border: 0,
                            borderBottom: "1px solid var(--border)",
                            background: "none",
                            color: "var(--text)",
                            cursor: "pointer",
                            textAlign: "left",
                            font: "inherit",
                            fontSize: 13,
                          }}
                          onClick={() => {
                            sessionStorage.setItem("open_ticket", String(tk.id));
                            nav("/tickets");
                          }}
                        >
                          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
                            <span className="dim">#{tk.id}</span> {tk.subject}
                          </span>
                          <span className="dim" style={{ fontSize: 12, flex: "0 0 auto" }}>
                            {tk.status === "closed" ? "закрыт" : tk.status === "waiting" ? "ждёт клиента" : "открыт"} →
                          </span>
                        </button>
                      ))}
                    </div>
                  ) : (
                    <span className="dim">Обращений не было</span>
                  )}
                </div>
              )}

              {tab === "actions" && (
                <div className="grid" style={{ gap: 12 }}>
                  <div className="ud-section">
                    <div className="ud-title">Написать клиенту в бот</div>
                    <textarea
                      className="input"
                      rows={3}
                      placeholder="Текст сообщения…"
                      value={msgText}
                      style={{ width: "100%", resize: "vertical" }}
                      onChange={(e) => setMsgText(e.target.value)}
                    />
                    <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
                      <button
                        className="btn primary sm"
                        disabled={!msgText.trim() || act.isPending}
                        onClick={() => {
                          act.mutate({ path: "/message", body: { text: msgText } });
                          setMsgText("");
                        }}
                      >
                        Отправить
                      </button>
                    </div>
                  </div>
                  <div className="ud-section">
                    <div className="ud-title">Обслуживание</div>
                    <div className="grid" style={{ gap: 8 }}>
                      <button className="btn secondary" onClick={() => act.mutate({ path: "/reset-traffic" })}>
                        {t.resetTraffic}
                      </button>
                      <button
                        className="btn secondary"
                        onClick={async () => {
                          if (await confirm(t.resetDevicesConfirm)) act.mutate({ path: "/reset-devices" });
                        }}
                      >
                        {t.resetDevices}
                      </button>
                    </div>
                  </div>
                  <div className="ud-section" style={{ borderColor: "rgba(208,59,59,.35)" }}>
                    <div className="ud-title" style={{ color: "var(--bad-ink)" }}>
                      Опасная зона
                    </div>
                    <div className="grid" style={{ gap: 8 }}>
                      {d.status !== "blocked" ? (
                        <button
                          className="btn danger"
                          onClick={async () => {
                            if (await confirm(t.blockConfirm)) act.mutate({ path: "/block" });
                          }}
                        >
                          {t.block}
                        </button>
                      ) : (
                        <button className="btn secondary" onClick={() => act.mutate({ path: "/unblock" })}>
                          {t.unblock}
                        </button>
                      )}
                      <button className="btn danger" onClick={() => void deleteUser()}>
                        {t.deleteUser}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="grid" style={{ gap: 12 }}>
              <span className="sk" style={{ width: "60%", height: 22 }} />
              <span className="sk" style={{ width: "100%", height: 70 }} />
              <span className="sk" style={{ width: "100%", height: 160 }} />
            </div>
          )}
        </Drawer>
      )}
    </>
  );
}
