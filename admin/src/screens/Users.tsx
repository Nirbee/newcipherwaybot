/* Screen 02 — Клиенты: tiles + filter chips + search + sortable table + 460px drawer.
   Backend: GET /api/admin/users/clients (bot data + one cached panel snapshot),
   POST /api/admin/users/manual («+ Добавить клиента»), per-user actions /api/admin/users/{id}/…. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate } from "react-router";

import { ApiError, api, bytesFmt, dt, dtTime, getToken, money } from "../api/client";
import { Drawer, Field, Modal, Prog, Seg } from "../components/ui";
import { PanelSyncButton } from "../components/PanelSync";
import { useApp } from "../state/app";

type ClientRow = {
  id: number;
  telegram_id: number | null;
  username: string | null;
  name: string | null;
  email: string | null;
  status: string;
  is_service: boolean;
  plan_name: string | null;
  source: string | null;
  expire_at: string | null;
  expire_forever: boolean;
  grace_until: string | null;
  traffic_used_bytes: number;
  traffic_limit_bytes: number;
  device_limit: number | null;
  devices: number | null;
  online_at: string | null;
  never_connected: boolean;
  node: { name: string | null; country: string | null } | null;
  squads: string[];
  tag: string | null;
  panel_username: string | null;
  in_panel: boolean;
  created_at: string | null;
};

type ClientsResp = {
  items: ClientRow[];
  total: number;
  limit: number;
  offset: number;
  counters: Record<string, number>;
  panel: { ok: boolean; error: string | null; devices_error: string | null; fetched_at: string | null };
  can_export: boolean;
};

type PanelView = {
  username?: string;
  panel_id?: number | null;
  status?: string;
  expire_at?: string | null;
  device_limit?: number | null;
  traffic_limit_bytes?: number;
  tag?: string | null;
  online_at?: string | null;
  mismatch?: string[];
  error?: string;
} | null;

type Detail = {
  id: number;
  telegram_id: number | null;
  username: string | null;
  name: string | null;
  email: string | null;
  status: string;
  balance_minor: number;
  plan_name: string | null;
  expire_at: string | null;
  grace_until: string | null;
  traffic_used_bytes: number;
  traffic_limit_bytes: number;
  device_limit: number | null;
  created_at: string | null;
  last_seen_at: string | null;
  panel?: PanelView;
  is_service: boolean;
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
    panel_username: string | null;
    source: string;
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

type TicketRow = { id: number; user_id: number; subject: string; status: string };
type Plan = { id: number; name: string; is_trial: boolean };
type Squad = { id: number; name: string; uuid: string };

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

/* status -> [label, badge tone] */
const STATUS: Record<string, [string, string]> = {
  active: ["Активна", "ok"],
  trial: ["Пробная", "accent"],
  grace: ["Льготный период", "warn"],
  limited: ["Лимит трафика", "warn"],
  expired: ["Истекла", "bad"],
  disabled: ["Отключена", "dim"],
  blocked: ["Заблокирован", "bad"],
  none: ["Без подписки", "dim"],
};

const SOURCE: Record<string, string> = {
  bot: "бот",
  panel: "из панели",
  manual: "вручную",
  bedolaga: "Bedolaga",
  remnashop: "Remnashop",
  solobot: "SoloBot",
  "3x-ui": "3x-ui",
};

/* chip id, label, one-line explanation under the row */
const CHIPS: [string, string, string][] = [
  ["all", "Все", "Все клиенты бота, кроме служебных учёток MultiScript."],
  ["active", "Активные", "Подписка действует: оплаченная, пробная или в льготном периоде."],
  ["online", "Онлайн сейчас", "Были в сети за последние 2 минуты."],
  ["exp3", "Кончается за 3 дня", "Подписка закончится в ближайшие 3 дня: самое время напомнить про продление."],
  ["exp7", "Кончается за 7 дней", "Подписка закончится в ближайшую неделю — напомните про продление."],
  ["expired_week", "Истекли за неделю", "Срок закончился за последние 7 дней — таких проще всего вернуть."],
  ["never", "Ни разу не подключались", "Учётка в панели есть, но подключений не было ни одного — помогите настроить приложение."],
  ["gone", "Пропали больше недели", "Подключались раньше, но не появлялись больше 7 дней."],
  ["near_limit", "Близко к лимиту", "Израсходовано 80% трафика или больше."],
  ["devices_over", "Устройств больше лимита", "Подключено больше устройств, чем разрешает подписка."],
  ["new_week", "Новые за неделю", "Пришли в бота за последние 7 дней."],
  ["limited", "Упёрлись в лимит", "Трафик исчерпан — панель ограничила доступ (LIMITED)."],
  ["disabled", "Отключённые", "Подписка отключена: возврат, отзыв или вручную."],
  ["expired", "Истёкшие", "Срок подписки закончился."],
  ["top_traffic", "Топ-20 по трафику", "20 клиентов с наибольшим расходом трафика."],
  ["no_sub", "Без подписки", "Запускали бота, но ничего не покупали."],
  ["trial", "Пробные", "Сейчас на пробном периоде."],
  ["blocked", "Заблокированные", "Заблокированы администратором."],
  ["service", "Служебные", "Служебные учётки MultiScript (каскад, мониторинг): бот их не меняет, здесь они только для просмотра."],
];

/* table columns: id, title, sort key (null = not sortable) */
const COLS: [string, string, string | null][] = [
  ["client", "Клиент", "name"],
  ["status", "Статус", "status"],
  ["expire", "Срок", "expire"],
  ["traffic", "Трафик", "traffic"],
  ["online", "Онлайн", "online"],
  ["node", "Нода", "node"],
  ["devices", "Устройства", "devices"],
  ["squads", "Сквады", "squads"],
  ["tag", "Метка", "tag"],
  ["source", "Источник", "source"],
];
const GRID = "minmax(190px,2fr) 128px 112px 150px 104px minmax(110px,1fr) 92px minmax(90px,1fr) 90px 82px";
const PAGE = 50;
/* panel.error is a code, never the exception text */
const PANEL_ERR: Record<string, string> = {
  unreachable: "панель недоступна",
  timeout: "панель не ответила вовремя",
  auth: "панель отклонила токен",
  rejected: "панель вернула ошибку",
};
const DAY = 86_400_000;

function flag(cc: string | null | undefined): string {
  if (!cc || !/^[a-z]{2}$/i.test(cc)) return "";
  return String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 0x1f1a5 + c.charCodeAt(0)));
}

function ago(iso: string, now: number): string {
  const min = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60_000));
  if (min < 60) return `${min} мин назад`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.round(h / 24)} дн назад`;
}

function Badge({ status }: { status: string }) {
  const [label, tone] = STATUS[status] ?? [status, "dim"];
  return <span className={`cl-badge ${tone}`}>{label}</span>;
}

function ExpireCell({ r, now }: { r: ClientRow; now: number }) {
  if (r.status === "grace" && r.grace_until) {
    return <span className="cl-warn">льгота до {dt(r.grace_until)}</span>;
  }
  if (r.expire_forever) return <span>бессрочно</span>;
  if (!r.expire_at) return <span className="dim">—</span>;
  const left = (new Date(r.expire_at).getTime() - now) / DAY;
  const title = dt(r.expire_at);
  if (left < 0) {
    const d = Math.max(1, Math.floor(-left));
    return (
      <span className="cl-bad" title={title}>
        истекла {d} дн. назад
      </span>
    );
  }
  const days = Math.floor(left);
  const text = days < 1 ? "сегодня" : days === 1 ? "завтра" : `${days} дн.`;
  return (
    <span className={left <= 7 ? "cl-warn" : undefined} title={title}>
      {text}
      <div className="dim" style={{ fontSize: 11 }}>
        до {title}
      </div>
    </span>
  );
}

function OnlineCell({ r, now }: { r: ClientRow; now: number }) {
  if (r.online_at) {
    const fresh = now - new Date(r.online_at).getTime() <= 2 * 60_000;
    return fresh ? <span className="cl-on">● сейчас</span> : <span className="muted">{ago(r.online_at, now)}</span>;
  }
  if (r.never_connected) return <span className="dim">не было</span>;
  return <span className="dim">—</span>;
}

function QrImage({ text }: { text: string }) {
  const [src, setSrc] = useState("");
  const [full, setFull] = useState(false);
  useEffect(() => {
    QRCode.toDataURL(text, { margin: 2, width: 720, errorCorrectionLevel: "M" })
      .then(setSrc)
      .catch(() => setSrc(""));
  }, [text]);
  if (!src) return null;
  return (
    <>
      <button type="button" className="qr-box" onClick={() => setFull(true)}>
        <img src={src} alt="QR-код подписки" />
        <span className="qr-hint">⤢ Нажмите — QR на весь экран</span>
      </button>
      {full &&
        createPortal(
          <div className="qr-full" onClick={() => setFull(false)} role="dialog">
            <img src={src} alt="QR-код подписки" />
            <div className="qr-full-note">Клиент сканирует камерой телефона · нажмите, чтобы закрыть</div>
          </div>,
          document.body,
        )}
    </>
  );
}

/* ---- «+ Добавить клиента» ---- */

type Created = { user_id: number; subscription_url: string | null; panel_username: string | null; expire_at: string | null };

function AddClient({ onClose, onOpen }: { onClose: () => void; onOpen: (id: number) => void }) {
  const { toast } = useApp();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [telegram, setTelegram] = useState("");
  const [email, setEmail] = useState("");
  const [tag, setTag] = useState("");
  const [term, setTerm] = useState<"days" | "until" | "forever" | "plan">("days");
  const [days, setDays] = useState("30");
  const [until, setUntil] = useState("");
  const [planId, setPlanId] = useState("");
  const [traffic, setTraffic] = useState("");
  const [devices, setDevices] = useState("");
  const [squad, setSquad] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState<{ message: string; user_id: number } | null>(null);
  const [created, setCreated] = useState<Created | null>(null);

  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: () => api.get<{ items: Plan[] }>("/api/admin/plans"),
  });
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => api.get<{ squads: Squad[] }>("/api/admin/servers"),
  });

  const tagOk = !tag.trim() || /^[A-Za-z0-9_]{1,16}$/.test(tag.trim());
  const termOk =
    term === "forever" ||
    (term === "days" && Number(days) >= 1) ||
    (term === "until" && !!until) ||
    (term === "plan" && !!planId && Number(days) >= 1);
  const canSave = !!name.trim() && tagOk && termOk && !busy;

  async function save() {
    if (busy) return; // one request at a time: a double click must not create two clients
    setBusy(true);
    setConflict(null);
    try {
      const body: Record<string, unknown> = {
        name: name.trim(),
        telegram: telegram.trim() || null,
        email: email.trim() || null,
        tag: tag.trim() || null,
        term,
        internal_squads: squad ? [squad] : [],
      };
      if (term === "days" || term === "plan") body.days = Number(days);
      if (term === "until") body.until = until;
      if (term === "plan") body.plan_id = Number(planId);
      if (traffic.trim() !== "") body.traffic_gb = Number(traffic);
      if (devices.trim() !== "") body.device_limit = Number(devices);
      const res = await api.post<Created>("/api/admin/users/manual", body);
      setCreated(res);
      void qc.invalidateQueries({ queryKey: ["users"] });
      void qc.invalidateQueries({ queryKey: ["clients"] });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        try {
          setConflict(JSON.parse(e.detail) as { message: string; user_id: number });
        } catch {
          toast(e.message);
        }
      } else {
        toast((e as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <Modal title="Клиент добавлен" onClose={onClose}>
        <div className="grid" style={{ gap: 12 }}>
          <div className="dim" style={{ fontSize: 13 }}>
            {created.panel_username && <span className="mono">{created.panel_username}</span>}
            {created.expire_at && <> · действует до {dt(created.expire_at)}</>}
          </div>
          {created.subscription_url ? (
            <>
              <div className="site-url">{created.subscription_url}</div>
              <button
                className="btn secondary"
                onClick={() => {
                  void navigator.clipboard.writeText(created.subscription_url ?? "");
                  toast("Ссылка скопирована");
                }}
              >
                Скопировать ссылку подписки
              </button>
              <QrImage text={created.subscription_url} />
            </>
          ) : (
            <span className="dim">Панель не вернула ссылку подписки — откройте карточку позже.</span>
          )}
          <div className="row" style={{ gap: 8, justifyContent: "flex-end", flexWrap: "wrap" }}>
            <button className="btn secondary" onClick={onClose}>
              Готово
            </button>
            <button className="btn primary" onClick={() => onOpen(created.user_id)}>
              Открыть карточку
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="Добавить клиента" onClose={onClose} wide>
      <div className="grid" style={{ gap: 12 }}>
        <div className="dim" style={{ fontSize: 12.5, lineHeight: 1.45 }}>
          Сразу заводит клиента в боте, подписку и учётку в панели. Telegram и e-mail необязательны — например, для
          клиентов за наличные и родственников.
        </div>
        <Field label="Имя *">
          <input className="input" value={name} maxLength={128} autoFocus onChange={(e) => setName(e.target.value)} />
        </Field>
        <div className="cl-form2">
          <Field label="Telegram — ID или @username">
            <input className="input" value={telegram} placeholder="необязательно" onChange={(e) => setTelegram(e.target.value)} />
          </Field>
          <Field label="E-mail">
            <input className="input" type="email" value={email} placeholder="необязательно" onChange={(e) => setEmail(e.target.value)} />
          </Field>
        </div>
        <Field label="Метка в панели (A-Z, 0-9, _ — до 16)">
          <input
            className="input mono"
            value={tag}
            maxLength={16}
            placeholder="например NL_017"
            style={tagOk ? undefined : { borderColor: "var(--bad)" }}
            onChange={(e) => setTag(e.target.value.toUpperCase())}
          />
        </Field>
        <Field label="Срок">
          <Seg
            value={term}
            options={[
              { id: "days" as const, label: "N дней" },
              { id: "until" as const, label: "До даты" },
              { id: "forever" as const, label: "Бессрочно" },
              { id: "plan" as const, label: "По тарифу" },
            ]}
            onChange={setTerm}
          />
        </Field>
        {(term === "days" || term === "plan") && (
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {term === "plan" && (
              <select className="input" value={planId} style={{ flex: "1 1 200px" }} onChange={(e) => setPlanId(e.target.value)}>
                <option value="">Выберите тариф…</option>
                {(plans.data?.items ?? [])
                  .filter((p) => !p.is_trial)
                  .map((p) => (
                    <option key={p.id} value={String(p.id)}>
                      {p.name}
                    </option>
                  ))}
              </select>
            )}
            <input className="input num" type="number" min={1} max={3650} value={days} style={{ width: 90 }} onChange={(e) => setDays(e.target.value)} />
            <span className="dim">дн.</span>
            {[7, 30, 90, 365].map((n) => (
              <button key={n} className="cap-pill" style={{ cursor: "pointer" }} onClick={() => setDays(String(n))}>
                {n}
              </button>
            ))}
          </div>
        )}
        {term === "until" && (
          <input className="input" type="date" value={until} style={{ width: 170 }} onChange={(e) => setUntil(e.target.value)} />
        )}
        <div className="cl-form2">
          <Field label={term === "plan" ? "Трафик, ГБ (пусто — как в тарифе)" : "Трафик, ГБ (0 или пусто — безлимит)"}>
            <input className="input num" type="number" min={0} value={traffic} onChange={(e) => setTraffic(e.target.value)} />
          </Field>
          <Field label={term === "plan" ? "Устройств (пусто — как в тарифе)" : "Устройств (пусто — по умолчанию, 0 — без лимита)"}>
            <input className="input num" type="number" min={0} max={100} value={devices} onChange={(e) => setDevices(e.target.value)} />
          </Field>
        </div>
        <Field label="Сквад">
          <select className="input" value={squad} onChange={(e) => setSquad(e.target.value)}>
            <option value="">{term === "plan" ? "Как в тарифе" : "По умолчанию — как у обычной покупки"}</option>
            {(servers.data?.squads ?? []).map((s) => (
              <option key={s.uuid} value={s.uuid}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        {conflict && (
          <div className="site-warn" style={{ marginBottom: 0 }}>
            {conflict.message}.{" "}
            <button className="btn secondary sm" onClick={() => onOpen(conflict.user_id)}>
              Открыть карточку
            </button>
          </div>
        )}
        <div className="row" style={{ gap: 8, justifyContent: "flex-end" }}>
          <button className="btn secondary" onClick={onClose}>
            Отмена
          </button>
          <button className="btn primary" disabled={!canSave} onClick={() => void save()}>
            {busy ? "Создаю…" : "Добавить"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/* ---- the screen ---- */

export default function Users() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [qDebounced, setQDebounced] = useState("");
  const [chip, setChip] = useState("all");
  const [sort, setSort] = useState<{ key: string; desc: boolean }>({ key: "created", desc: true });
  const [offset, setOffset] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [adding, setAdding] = useState(false);
  const [selId, setSelId] = useState<number | null>(null);
  const [tab, setTab] = useState<"overview" | "finance" | "sub" | "tickets" | "actions">("overview");
  const [hwidInput, setHwidInput] = useState("");
  const [balAmount, setBalAmount] = useState("");
  const nav = useNavigate();
  const [extDays, setExtDays] = useState(""); // custom "+N days"
  const [extUntil, setExtUntil] = useState(""); // absolute expiry date (YYYY-MM-DD)
  const [grantPlan, setGrantPlan] = useState(""); // tariff to hand out (not just days)
  const [grantDays, setGrantDays] = useState("30");
  const [msgText, setMsgText] = useState(""); // DM to the customer
  const [discount, setDiscount] = useState("");
  const [trafficGb, setTrafficGb] = useState("");
  const [tagInput, setTagInput] = useState("");

  useEffect(() => {
    const h = setTimeout(() => setQDebounced(q), 300);
    return () => clearTimeout(h);
  }, [q]);
  useEffect(() => setOffset(0), [qDebounced, chip, sort]);
  useEffect(() => {
    const h = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(h);
  }, []);

  const params = `q=${encodeURIComponent(qDebounced)}&chip=${chip}&sort=${sort.key}&desc=${sort.desc}`;
  const listKey = ["clients", qDebounced, chip, sort.key, sort.desc, offset];
  const list = useQuery({
    queryKey: listKey,
    queryFn: () => api.get<ClientsResp>(`/api/admin/users/clients?${params}&limit=${PAGE}&offset=${offset}`),
    placeholderData: (prev) => prev,
  });
  const detail = useQuery({
    queryKey: ["user", selId],
    queryFn: () => api.get<Detail>(`/api/admin/users/${selId}`),
    enabled: selId !== null,
  });
  // Tariffs for the "выдать подписку" picker (shares the cache with the Тарифы screen).
  const plans = useQuery({
    queryKey: ["plans"],
    queryFn: () => api.get<{ items: Plan[] }>("/api/admin/plans"),
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
    void qc.invalidateQueries({ queryKey: ["clients"] });
    void qc.invalidateQueries({ queryKey: ["user", selId] });
  }

  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => api.post(`/api/admin/users/${selId}${path}`, body),
    onSuccess: (_data, variables) => {
      invalidate();
      // Balance changes surface their own toast (with an Undo action) via changeBalance.
      if (variables.path !== "/balance") toast("✓");
    },
    onError: (e) => toast(e.message),
  });

  async function refresh() {
    try {
      const data = await api.get<ClientsResp>(`/api/admin/users/clients?${params}&limit=${PAGE}&offset=${offset}&refresh=true`);
      qc.setQueryData(listKey, data);
      setNow(Date.now());
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function exportCsv() {
    try {
      const res = await fetch(`/api/admin/users/clients/export?${params}`, {
        headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      });
      if (!res.ok) throw new Error(res.status === 403 ? "Скачивать может только владелец или разработчик" : `Ошибка ${res.status}`);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(await res.blob());
      a.download = `clients-${new Date().toISOString().slice(0, 10)}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(t.exportCsv + " ✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

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
      void qc.invalidateQueries({ queryKey: ["clients"] });
      toast("✕");
    } catch (e) {
      // Staff accounts return 400 — surface the message.
      toast((e as Error).message);
    }
  }

  function openUser(id: number) {
    setSelId(id);
    setTab("overview");
    setTrafficGb("");
    setTagInput("");
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setSelId(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const data = list.data;
  const c = data?.counters ?? {};
  const d = detail.data;
  const hint = CHIPS.find(([id]) => id === chip)?.[2];
  const fetchedAt = data?.panel.fetched_at ? new Date(data.panel.fetched_at).getTime() : null;
  const updated =
    fetchedAt === null ? "панель не ответила" : now - fetchedAt < 60_000 ? "обновлено только что" : `обновлено ${ago(data!.panel.fetched_at!, now)}`;

  function sortBy(key: string | null) {
    if (!key) return;
    setSort((s) => (s.key === key ? { key, desc: !s.desc } : { key, desc: key !== "name" && key !== "status" && key !== "node" }));
  }

  const tiles: [string, string, number | undefined, string, string | undefined][] = [
    ["all", "Всего клиентов", c.all, "", undefined],
    ["active", "Активных", c.active, c.all !== undefined ? `из ${c.all}` : "", undefined],
    ["online", "Онлайн сейчас", c.online, "за последние 2 минуты", undefined],
    ["exp7", "Кончаются за 7 дней", c.exp7, "напомните про продление", "warn"],
    ["never", "Ни разу не подключались", c.never, "помогите настроить", undefined],
  ];

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.users}</h1>
        <div className="actions">
          <span className="dim cl-updated" title={data?.panel.fetched_at ? dtTime(data.panel.fetched_at) : undefined}>
            {data ? updated : ""}
          </span>
          <button className="btn secondary" onClick={() => void refresh()} title="Перечитать данные панели">
            ⟳ Обновить
          </button>
          <PanelSyncButton onDone={() => invalidate()} onOpenUser={(id) => openUser(id)} />
          {data?.can_export && (
            <button className="btn secondary" onClick={() => void exportCsv()}>
              Скачать CSV
            </button>
          )}
          <button className="btn primary" onClick={() => setAdding(true)}>
            + Добавить клиента
          </button>
        </div>
      </div>

      <div className="kpis cl-tiles">
        {tiles.map(([id, label, value, note, tone]) => (
          <div
            key={id}
            className={`kpi link${tone ? ` cl-tile-${tone}` : ""}${chip === id ? " cl-tile-on" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => setChip(id)}
            onKeyDown={(e) => e.key === "Enter" && setChip(id)}
          >
            <div className="caps">{label}</div>
            <div className="val">{value ?? <span className="sk" style={{ width: 60 }} />}</div>
            {note && <div className="note">{note}</div>}
          </div>
        ))}
      </div>

      {data && !data.panel.ok && (
        <div className="site-warn">
          ⚠️ Панель Remnawave: {PANEL_ERR[data.panel.error ?? ""] ?? "ошибка запроса"} —{" "}
          {data.panel.fetched_at ? `показаны данные панели на ${dtTime(data.panel.fetched_at)}` : "столбцы панели пустые"}, остальное — из бота.
        </div>
      )}
      {data?.panel.ok && data.panel.devices_error && (
        <div className="site-warn">⚠️ Список устройств из панели не получен — столбец «Устройства» показывает только лимит.</div>
      )}

      <div className="cl-chips">
        {CHIPS.filter(([id]) => id !== "service" || (c.service ?? 0) > 0).map(([id, label]) => (
          <button key={id} className={`cl-chip${chip === id ? " on" : ""}`} onClick={() => setChip(id)}>
            {label}
            <span className="cnt">{c[id] ?? "·"}</span>
          </button>
        ))}
      </div>
      {hint && <div className="cl-hint">{hint}</div>}

      <input
        className="input"
        style={{ width: "100%", marginBottom: 12 }}
        placeholder="Имя, метка, e-mail, @username, Telegram ID, sub_…, id в боте"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />

      <div className="cl-scroll">
        <div className="tbl cl-tbl">
          <div className="tr head" style={{ gridTemplateColumns: GRID }}>
            {COLS.map(([id, title, key]) => (
              <span key={id} className={key ? "cl-sort" : undefined} onClick={() => sortBy(key)}>
                {title}
                {key && sort.key === key ? (sort.desc ? " ↓" : " ↑") : ""}
              </span>
            ))}
          </div>
          {(data?.items ?? []).map((r) => {
            const over = r.devices !== null && !!r.device_limit && r.devices > r.device_limit;
            return (
              <div key={r.id} className="tr click" style={{ gridTemplateColumns: GRID }} onClick={() => openUser(r.id)}>
                <span style={{ minWidth: 0 }}>
                  <div className="cl-ellipsis" style={{ fontWeight: 550 }}>
                    {r.name ?? r.panel_username ?? (r.username ? `@${r.username}` : `id${r.id}`)}
                  </div>
                  <div className="dim cl-ellipsis" style={{ fontSize: 11.5 }}>
                    {r.username ? `@${r.username}` : r.telegram_id ? `TG ${r.telegram_id}` : r.email ?? `id${r.id}`}
                    {r.panel_username && r.panel_username !== r.name ? ` · ${r.panel_username}` : ""}
                  </div>
                </span>
                <span>
                  <Badge status={r.status} />
                </span>
                <ExpireCell r={r} now={now} />
                <span className="mono" style={{ fontSize: 12 }}>
                  {bytesFmt(r.traffic_used_bytes)} / {r.traffic_limit_bytes ? bytesFmt(r.traffic_limit_bytes) : "безлимит"}
                  {r.traffic_limit_bytes > 0 && (
                    <div className="cl-bar">
                      <Prog pct={(r.traffic_used_bytes / r.traffic_limit_bytes) * 100} />
                    </div>
                  )}
                </span>
                <OnlineCell r={r} now={now} />
                <span className="cl-ellipsis">{r.node?.name ? `${flag(r.node.country)} ${r.node.name}` : <span className="dim">—</span>}</span>
                <span className={over ? "cl-warn" : undefined}>
                  {r.devices ?? "—"} / {r.device_limit || "∞"}
                </span>
                <span className="cl-ellipsis dim" style={{ fontSize: 12 }} title={r.squads.join(", ")}>
                  {r.squads.join(", ") || "—"}
                </span>
                <span>{r.tag ? <span className="cap-pill">{r.tag}</span> : <span className="dim">—</span>}</span>
                <span className="dim" style={{ fontSize: 11.5 }}>
                  {r.source ? SOURCE[r.source] ?? r.source : "—"}
                </span>
              </div>
            );
          })}
          {data && data.items.length === 0 && <div className="tr dim">{list.isFetching ? t.loading : "Никого не нашлось"}</div>}
          {!data && <div className="tr dim">{t.loading}</div>}
        </div>
      </div>

      {data && data.total > PAGE && (
        <div className="row" style={{ gap: 8, marginTop: 12, justifyContent: "flex-end", alignItems: "center" }}>
          <span className="dim" style={{ fontSize: 12.5 }}>
            {offset + 1}–{Math.min(offset + PAGE, data.total)} из {data.total}
          </span>
          <button className="btn secondary sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            ← Назад
          </button>
          <button className="btn secondary sm" disabled={offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>
            Дальше →
          </button>
        </div>
      )}

      {adding && (
        <AddClient
          onClose={() => setAdding(false)}
          onOpen={(id) => {
            setAdding(false);
            openUser(id);
          }}
        />
      )}

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
                    <Badge status={d.status} />
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
                    {d.panel?.tag && <span className="cap-pill accent">{d.panel.tag}</span>}
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

              {d.is_service && (
                <div className="site-warn" style={{ marginBottom: 0 }}>
                  🔒 Служебная учётка MultiScript (каскад или мониторинг). Бот её не меняет — все действия отключены.
                </div>
              )}

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
                  <b>{d.subscription?.device_limit || "∞"}</b>
                  <span className="dim">лимит HWID</span>
                </div>
              </div>

              {/* quick actions — the things done most often, without hunting through tabs */}
              {!d.is_service && (
                <div className="ud-section">
                  <div className="ud-title">Быстрые действия</div>
                  {d.subscription ? (
                    <div className="grid" style={{ gap: 10 }}>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <span className="dim cl-qlabel">Продлить</span>
                        {[7, 30, 90, 365].map((n) => (
                          <button
                            key={n}
                            className="btn secondary sm"
                            disabled={act.isPending}
                            onClick={() => act.mutate({ path: "/extend", body: { days: n } })}
                          >
                            +{n}
                          </button>
                        ))}
                        <button
                          className="btn secondary sm"
                          disabled={act.isPending}
                          onClick={async () => {
                            if (await confirm("Сделать подписку бессрочной?")) act.mutate({ path: "/extend", body: { forever: true } });
                          }}
                        >
                          ∞ Бессрочно
                        </button>
                      </div>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <span className="dim cl-qlabel">До даты</span>
                        <input className="input" type="date" value={extUntil} style={{ width: 150 }} onChange={(e) => setExtUntil(e.target.value)} />
                        <button
                          className="btn secondary sm"
                          disabled={!extUntil || act.isPending}
                          onClick={() => {
                            act.mutate({ path: "/extend", body: { until: extUntil } });
                            setExtUntil("");
                          }}
                        >
                          Установить
                        </button>
                      </div>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <span className="dim cl-qlabel">Трафик, ГБ</span>
                        <input
                          className="input num"
                          type="number"
                          min={0}
                          value={trafficGb}
                          style={{ width: 90 }}
                          placeholder={d.traffic_limit_bytes ? String(Math.round(d.traffic_limit_bytes / 1024 ** 3)) : "∞"}
                          onChange={(e) => setTrafficGb(e.target.value)}
                        />
                        <button
                          className="btn secondary sm"
                          disabled={trafficGb === "" || act.isPending}
                          title="0 — безлимит"
                          onClick={() => {
                            act.mutate({ path: "/limits", body: { traffic_gb: Number(trafficGb) } });
                            setTrafficGb("");
                          }}
                        >
                          Сохранить
                        </button>
                        <span className="dim" style={{ fontSize: 11.5 }}>
                          0 — безлимит
                        </span>
                      </div>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <span className="dim cl-qlabel">Метка</span>
                        <input
                          className="input mono"
                          value={tagInput}
                          maxLength={16}
                          style={{ width: 130 }}
                          placeholder={d.panel?.tag ?? "A-Z 0-9 _"}
                          onChange={(e) => setTagInput(e.target.value.toUpperCase())}
                        />
                        <button
                          className="btn secondary sm"
                          disabled={!/^[A-Z0-9_]{1,16}$/.test(tagInput) || act.isPending}
                          onClick={() => {
                            act.mutate({ path: "/limits", body: { tag: tagInput } });
                            setTagInput("");
                          }}
                        >
                          Сохранить
                        </button>
                        {d.panel?.tag && (
                          <button className="btn secondary sm" onClick={() => act.mutate({ path: "/limits", body: { tag: "" } })}>
                            Снять
                          </button>
                        )}
                      </div>
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        <button className="btn secondary sm" onClick={() => act.mutate({ path: "/reset-traffic" })}>
                          {t.resetTraffic}
                        </button>
                        <button
                          className="btn secondary sm"
                          onClick={async () => {
                            if (await confirm(t.resetDevicesConfirm)) act.mutate({ path: "/reset-devices" });
                          }}
                        >
                          {t.resetDevices}
                        </button>
                        {d.subscription.subscription_url && (
                          <button
                            className="btn secondary sm"
                            onClick={() => {
                              void navigator.clipboard.writeText(d.subscription?.subscription_url ?? "");
                              toast("Ссылка подписки скопирована");
                            }}
                          >
                            Скопировать ссылку
                          </button>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="dim" style={{ fontSize: 13, marginBottom: 10 }}>
                      Подписки нет — выдайте по тарифу:
                    </div>
                  )}
                  <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: d.subscription ? 10 : 0 }}>
                    <select className="input" value={grantPlan} style={{ flex: "1 1 160px" }} onChange={(e) => setGrantPlan(e.target.value)}>
                      <option value="">Выдать по тарифу…</option>
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
                      style={{ width: 76 }}
                      onChange={(e) => setGrantDays(e.target.value)}
                    />
                    <span className="dim">дн.</span>
                    <button
                      className="btn primary sm"
                      disabled={!grantPlan || !Number(grantDays) || act.isPending}
                      onClick={() => act.mutate({ path: "/grant", body: { plan_id: Number(grantPlan), days: Number(grantDays) } })}
                    >
                      Выдать
                    </button>
                  </div>
                  <div className="row" style={{ gap: 6, flexWrap: "wrap", marginTop: 10 }}>
                    {d.telegram_id && (
                      <button className="btn secondary sm" onClick={() => setTab("actions")}>
                        ✉ Сообщение
                      </button>
                    )}
                    {d.status !== "blocked" ? (
                      <button
                        className="btn danger sm"
                        onClick={async () => {
                          if (await confirm(t.blockConfirm)) act.mutate({ path: "/block" });
                        }}
                      >
                        {t.block}
                      </button>
                    ) : (
                      <button className="btn secondary sm" onClick={() => act.mutate({ path: "/unblock" })}>
                        {t.unblock}
                      </button>
                    )}
                  </div>
                </div>
              )}

              <Seg
                value={tab}
                options={
                  d.is_service
                    ? [
                        { id: "overview" as const, label: "Обзор" },
                        { id: "finance" as const, label: "Финансы" },
                      ]
                    : [
                        { id: "overview" as const, label: "Обзор" },
                        { id: "finance" as const, label: "Финансы" },
                        { id: "sub" as const, label: "Подписка" },
                        { id: "tickets" as const, label: "Тикеты" },
                        { id: "actions" as const, label: "Действия" },
                      ]
                }
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
                      {d.grace_until && d.status === "active" && (
                        <div>
                          <span className="muted">Льготный период до</span>
                          <span>{dt(d.grace_until)}</span>
                        </div>
                      )}
                      <div>
                        <span className="muted">Трафик</span>
                        <span>
                          {bytesFmt(d.traffic_used_bytes)} / {d.traffic_limit_bytes ? bytesFmt(d.traffic_limit_bytes) : "∞"}
                        </span>
                      </div>
                      {d.email && (
                        <div>
                          <span className="muted">E-mail</span>
                          <span>{d.email}</span>
                        </div>
                      )}
                      {d.subscription && (
                        <div>
                          <span className="muted">Источник</span>
                          <span>{SOURCE[d.subscription.source] ?? d.subscription.source}</span>
                        </div>
                      )}
                    </div>
                    {d.traffic_limit_bytes > 0 && (
                      <div style={{ marginTop: 8 }}>
                        <Prog pct={(d.traffic_used_bytes / d.traffic_limit_bytes) * 100} />
                      </div>
                    )}
                  </div>

                  {d.panel && (
                    <div className={`ud-section${d.panel.mismatch?.length || d.panel.error ? " ud-warn" : ""}`}>
                      <div className="ud-title">В панели Remnawave</div>
                      {d.panel.error ? (
                        <div style={{ color: "var(--warn)", fontSize: 13 }}>⚠️ {d.panel.error}</div>
                      ) : (
                        <>
                          <div className="kv">
                            <div>
                              <span className="muted">Имя в панели</span>
                              <span className="mono">
                                {d.panel.username}
                                {d.panel.panel_id != null ? ` · №${d.panel.panel_id}` : ""}
                              </span>
                            </div>
                            <div>
                              <span className="muted">Статус</span>
                              <span
                                style={{
                                  color: d.panel.status === "ACTIVE" ? "var(--good-ink)" : "var(--bad-ink)",
                                  fontWeight: 600,
                                }}
                              >
                                {d.panel.status}
                              </span>
                            </div>
                            <div>
                              <span className="muted">До</span>
                              <span>{d.panel.expire_at ? dt(d.panel.expire_at) : "—"}</span>
                            </div>
                            <div>
                              <span className="muted">Был онлайн</span>
                              <span>{d.panel.online_at ? dtTime(d.panel.online_at) : "—"}</span>
                            </div>
                          </div>
                          {!d.is_service && d.panel.mismatch && d.panel.mismatch.length > 0 && (
                            <div style={{ marginTop: 8, fontSize: 12.5, color: "var(--warn)", lineHeight: 1.45 }}>
                              {d.panel.mismatch.map((m) => (
                                <div key={m}>⚠️ Расходится: {m}</div>
                              ))}
                              <button className="btn secondary sm" style={{ marginTop: 8 }} onClick={() => act.mutate({ path: "/sync" })}>
                                ⟳ Привести панель к боту
                              </button>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  )}

                  {!d.is_service && (
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
                          <button className="btn primary sm" disabled={!(Number(hwidInput) >= 1) || act.isPending} onClick={saveHwid}>
                            Сохранить
                          </button>
                          <button
                            className="btn secondary sm"
                            disabled={act.isPending}
                            title="Без ограничения числа устройств"
                            onClick={() => act.mutate({ path: "/limits", body: { device_limit: 0 } })}
                          >
                            Без лимита
                          </button>
                        </div>
                      ) : (
                        <span className="dim">Нет подписки</span>
                      )}
                    </div>
                  )}

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
                                color: tx.status !== "completed" ? "var(--dim)" : sign === "+" ? "var(--good-ink)" : undefined,
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

              {tab === "sub" && !d.is_service && (
                <div className="grid" style={{ gap: 12 }}>
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
                      <button className="btn secondary sm" onClick={() => act.mutate({ path: "/trial", body: { available: !d.is_trial_available } })}>
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

              {tab === "actions" && !d.is_service && (
                <div className="grid" style={{ gap: 12 }}>
                  <div className="ud-section">
                    <div className="ud-title">Написать клиенту в бот</div>
                    {d.telegram_id ? (
                      <>
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
                      </>
                    ) : (
                      <span className="dim">У клиента нет Telegram — написать через бот нельзя.</span>
                    )}
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
