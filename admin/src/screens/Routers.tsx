/* Screen — Роутеры: control-plane fleet (RouterDevice). List + create + host assignment +
   eligible-hosts allowlist + token rotate/revoke/delete. See src/web/routes/admin/routers.py. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api, dtTime } from "../api/client";
import { Drawer, Field, Modal, Seg, SecretInput, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Mode = "auto" | "force";
type Status = "pending" | "online" | "offline" | "revoked";

type ProbeResult = {
  name: string;
  ok: boolean;
  ip: string;
  secs: string;
  big: string;
  error: string;
};
type VpnVerdict = {
  state: "ok" | "direct" | "slow" | "fail" | "unknown" | "none" | "expired";
  text: string;
  exit_ip?: string;
  direct_ip?: string;
  routed?: ProbeResult | null;
  candidates?: ProbeResult[];
  servers: ProbeResult[];
} | null;
type ConfigInfo = {
  source: "subscription" | "panel" | "none";
  servers: string[];
  warning: string | null;
  tags?: Record<string, string>;
  split_rules?: number;
} | null;

type RouterDevice = {
  id: number;
  label: string;
  subscription_id: number;
  subscription_label: string | null;
  awaiting_claim: boolean;
  mode: Mode;
  status: Status;
  is_online: boolean;
  primary_host_uuid: string | null;
  backup_host_uuid: string | null;
  config_etag: string | null;
  last_seen_at: string | null;
  xray_version: string | null;
  active_outbound: string | null;
  external_ip: string | null;
  last_error: string | null;
  vpn: VpnVerdict;
  note: string | null;
  created_at: string;
  /** Agent v7 journal: Xray restarts / watchdog stops over the last 24 h. */
  restarts_24h?: number;
};
type PulseCell = {
  median_1h: number | null;
  median_24h: number | null;
  loss_1h: number | null;
  loss_24h: number | null;
  samples_24h: number;
};
type GamesInfo = {
  enabled: boolean;
  supported: boolean;
  paths: { id: string; title: string }[];
  rows: {
    target: string;
    title: string;
    games: string;
    best: string | null;
    cells: Record<string, PulseCell>;
  }[];
  verdict: string;
  last_at: number | null;
};
type AgentEvent = { ts: number; kind: string; note: string };
type RestartsInfo = {
  day: number;
  week: number;
  supported: boolean;
  events: AgentEvent[];
};
type PulseTarget = {
  id: string;
  title: string;
  url: string;
  games: string;
  resolve_ip?: string;
};
type AvailableHost = { uuid: string; remark: string; network: string; is_disabled: boolean };
type SubSummary = {
  id: number;
  status: string;
  is_trial: boolean;
  expire_at: string | null;
  plan_name: string | null;
  device_limit: number | null;
} | null;
type RouterDetail = RouterDevice & {
  install_report: Record<string, unknown> | null;
  diagnostics: Record<string, string> | null;
  available_hosts: AvailableHost[];
  subscription: SubSummary;
  claim_url: string | null;
  install_code: string | null;
  config_info: ConfigInfo;
  recommended_xray: string;
  autotune: { note: string; at: string | null } | null;
  games?: GamesInfo;
  restarts?: RestartsInfo;
};

function QrImage({ text }: { text: string }) {
  const [src, setSrc] = useState("");
  const [full, setFull] = useState(false);
  useEffect(() => {
    // Rendered large once; CSS scales it down — stays sharp when opened full-screen.
    QRCode.toDataURL(text, { margin: 2, width: 720, errorCorrectionLevel: "M" })
      .then(setSrc)
      .catch(() => setSrc(""));
  }, [text]);
  if (!src) return null;
  return (
    <>
      <button type="button" className="qr-box" onClick={() => setFull(true)}>
        <img src={src} alt="QR-код для клиента" />
        <span className="qr-hint">⤢ Нажмите — QR на весь экран</span>
      </button>
      {/* Portal: outside the Field's <label> (a click there re-fires the button and reopens
          it) and outside the drawer's transformed box (which would clip position:fixed). */}
      {full &&
        createPortal(
          <div className="qr-full" onClick={() => setFull(false)} role="dialog">
            <img src={src} alt="QR-код для клиента" />
            <div className="qr-full-note">
              Клиент сканирует камерой телефона · нажмите, чтобы закрыть
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

/** Short install command: the 6-char code replaces the 43-char token the technician used to
 *  retype from the phone (typos broke installs). Falls back to the long form without a code. */
function installCommand(token: string, code?: string | null): string {
  const base = window.location.origin;
  if (code) return `opkg update && opkg install curl && curl -fsSL ${base}/i/${code} | sh`;
  return (
    `opkg update && opkg install curl && curl -fsSL ${base}/api/agent/install.sh ` +
    `-o /tmp/cw-install.sh && sh /tmp/cw-install.sh ${token} ${base}`
  );
}

function InstallBlock({
  code,
  token,
  onCopy,
}: {
  code: string | null;
  token: string | null;
  onCopy: (text: string) => void;
}) {
  const cmd = installCommand(token ?? "", code);
  return (
    <div className="install-block">
      {/* No own onClick: it sits inside Field's <label>, so a tap already activates the copy
          button below — a handler here would copy twice. */}
      <code className="install-cmd">{cmd}</code>
      <button className="btn primary install-copy" onClick={() => onCopy(cmd)}>
        📋 Скопировать команду
      </button>
      <div className="dim install-note">
        Вставьте целиком в SSH-подключение к роутеру.
        {code && " Команда действует 24 часа — потом выпустите новую в карточке роутера."}
      </div>
    </div>
  );
}


const VPN_COLOR: Record<string, string> = {
  ok: "var(--good-ink)",
  slow: "var(--warn)",
  unknown: "var(--warn)",
  direct: "var(--bad-ink)",
  fail: "var(--bad-ink)",
  none: "var(--muted)",
  expired: "var(--muted)",
};

function VpnBadge({ vpn }: { vpn: VpnVerdict }) {
  if (!vpn || vpn.state === "none") return null;
  const label =
    vpn.state === "ok"
      ? "VPN ✓"
      : vpn.state === "slow"
        ? "VPN медленно"
        : vpn.state === "unknown"
          ? "VPN не проверен"
          : vpn.state === "expired"
            ? "VPN выкл. — не оплачено"
            : "VPN не работает";
  return (
    <div className="vpn-badge" style={{ color: VPN_COLOR[vpn.state] }} title={vpn.text}>
      {label}
    </div>
  );
}

function VpnCheck({ vpn, info }: { vpn: VpnVerdict; info: ConfigInfo }) {
  const names = info?.tags ?? {};
  return (
    <div className="grid" style={{ gap: 8, fontSize: 13 }}>
      {vpn ? (
        <div className="vpn-verdict" style={{ borderColor: VPN_COLOR[vpn.state] }}>
          <b style={{ color: VPN_COLOR[vpn.state] }}>{vpn.text}</b>
        </div>
      ) : (
        <span className="dim" style={{ fontSize: 12.5 }}>
          Проверки ещё нет — агент обновится до версии 3 и пришлёт её в течение ~10 минут.
        </span>
      )}
      {info?.warning && (
        <div style={{ color: "var(--warn)", fontSize: 12.5 }}>⚠️ {info.warning}</div>
      )}
      {vpn?.routed && (
        <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
          <span>{vpn.routed.ok ? "✅" : "❌"} Устройства в сети (путь клиента)</span>
          <span className="mono" style={{ fontSize: 11.5, textAlign: "right", wordBreak: "break-all" }}>
            {vpn.routed.ok
              ? `${vpn.routed.ip} · ${Number(vpn.routed.secs || 0).toFixed(2)} с`
              : vpn.routed.error || "нет ответа"}
          </span>
        </div>
      )}
      {vpn?.servers.map((s) => (
        <div key={s.name} className="row" style={{ justifyContent: "space-between", gap: 10 }}>
          <span>
            {s.ok ? "✅" : "❌"} {names[s.name] ?? s.name}
          </span>
          <span className="mono" style={{ fontSize: 11.5, textAlign: "right", wordBreak: "break-all" }}>
            {s.ok
              ? `${s.ip} · ${Number(s.secs || 0).toFixed(2)} с${s.big && s.big !== "ok" ? ` · ⚠ ${s.big}` : ""}`
              : s.error || "нет ответа"}
          </span>
        </div>
      ))}
      {vpn?.candidates && vpn.candidates.length > 0 && (
        <details className="vpn-cands">
          <summary className="dim">
            Другие серверы для авто-выбора ({vpn.candidates.filter((c) => c.ok).length} из{" "}
            {vpn.candidates.length} проходят)
          </summary>
          {vpn.candidates.map((c) => (
            <div key={c.name} className="row dim" style={{ justifyContent: "space-between", gap: 10 }}>
              <span>
                {c.ok ? "✅" : "❌"} {names[c.name] ?? c.name}
              </span>
              <span className="mono" style={{ fontSize: 11.5, textAlign: "right", wordBreak: "break-all" }}>
                {c.ok ? `${Number(c.secs || 0).toFixed(2)} с` : c.error || "нет ответа"}
              </span>
            </div>
          ))}
        </details>
      )}
      {info && info.source !== "none" && info.split_rules !== undefined && (
        <div className="row dim" style={{ justifyContent: "space-between", fontSize: 12 }}>
          <span>Сплит-туннель (российское — напрямую)</span>
          <span className="mono">
            {info.split_rules > 0 ? `${info.split_rules} правил` : "нет — всё через VPN"}
          </span>
        </div>
      )}
      {vpn?.direct_ip && (
        <div className="row dim" style={{ justifyContent: "space-between", fontSize: 12 }}>
          <span>Без VPN (провайдер роутера)</span>
          <span className="mono">{vpn.direct_ip}</span>
        </div>
      )}
    </div>
  );
}

function DiagRow({ label, value, warn }: { label: string; value: string; warn?: string | null }) {
  return (
    <div className="grid" style={{ gap: 2 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 12 }}>
        <span className="muted">{label}</span>
        <span className="mono" style={{ fontSize: 12, textAlign: "right", wordBreak: "break-all" }}>
          {value || "—"}
        </span>
      </div>
      {warn && <div style={{ color: "var(--warn, #e0a800)", fontSize: 11.5 }}>⚠️ {warn}</div>}
    </div>
  );
}

const NEED_V7 = "Нужен агент v7 — обновится сам в течение 5 минут.";
/** A restart in the list gets flagged above this many per 24 h. */
const RESTARTS_WARN = 5;

function evTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function PulseValue({ cell, best }: { cell: PulseCell | undefined; best: boolean }) {
  if (!cell) return <span className="dim">—</span>;
  const ms = cell.median_1h ?? cell.median_24h;
  const loss = cell.loss_24h ?? 0;
  return (
    <span className="pulse-val">
      {ms == null ? (
        <span style={{ color: "var(--bad-ink)" }}>нет связи</span>
      ) : (
        <span
          title={cell.median_1h == null ? "За последний час замеров нет — медиана за 24 ч" : undefined}
          style={{ color: best ? "var(--good-ink)" : undefined, fontWeight: best ? 600 : undefined }}
        >
          {cell.median_1h == null ? "~" : ""}
          {ms} мс{best ? " ✓" : ""}
        </span>
      )}
      {ms != null && loss > 0 && (
        <span className="dim pulse-loss" style={{ color: loss >= 0.2 ? "var(--warn)" : undefined }}>
          потери {Math.round(loss * 100)} %
        </span>
      )}
    </span>
  );
}

function GamesBlock({
  games,
  onToggle,
}: {
  games: GamesInfo | undefined;
  onToggle: (on: boolean) => void;
}) {
  const enabled = games?.enabled ?? false;
  const paths = games?.paths ?? [];
  const cols = `minmax(110px, 1.3fr) repeat(${Math.max(paths.length, 1)}, minmax(70px, 1fr))`;
  return (
    <div className="grid" style={{ gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
        <span className="caps">Игры и пинг</span>
        <span className="row" style={{ gap: 8, fontSize: 12.5 }}>
          <span className="dim">Игровой пульс</span>
          <Toggle on={enabled} onChange={onToggle} />
        </span>
      </div>
      {!enabled && (
        <span className="dim" style={{ fontSize: 12.5, lineHeight: 1.45 }}>
          Пульс выключен. Включённый роутер раз в 10 минут сам замеряет задержку до игровых
          регионов — напрямую и через каждый сервер. Трафик клиентов не смотрим: только
          свои короткие замеры.
        </span>
      )}
      {enabled && games && !games.supported && (
        <span className="dim" style={{ fontSize: 12.5 }}>
          {NEED_V7}
        </span>
      )}
      {enabled && games?.supported && paths.length === 0 && (
        <span className="dim" style={{ fontSize: 12.5 }}>
          Ждём первых замеров — до 15 минут после включения.
        </span>
      )}
      {paths.length > 0 && games && (
        <>
          <div className="pulse-tbl">
            <div className="pulse-row head" style={{ gridTemplateColumns: cols }}>
              <span>Регион</span>
              {paths.map((p) => (
                <span key={p.id}>{p.title}</span>
              ))}
            </div>
            {games.rows.map((r) => (
              <div key={r.target} className="pulse-row" style={{ gridTemplateColumns: cols }}>
                <span className="pulse-target" title={r.games || undefined}>
                  {r.title}
                </span>
                {paths.map((p) => (
                  <span key={p.id} className="pulse-cell" data-label={p.title}>
                    <PulseValue cell={r.cells[p.id]} best={r.best === p.id} />
                  </span>
                ))}
              </div>
            ))}
          </div>
          <div className="dim" style={{ fontSize: 11.5 }}>
            медиана за 1 ч · потери за 24 ч под числом · ✓ лучший путь
            {games.last_at ? ` · последний замер ${evTime(games.last_at)}` : ""}
          </div>
          {games.verdict && (
            <div className="vpn-verdict" style={{ fontSize: 13 }}>
              Вывод: {games.verdict}.
            </div>
          )}
        </>
      )}
    </div>
  );
}

const EVENT_TEXT: Record<string, string> = {
  config_applied: "конфиг применён — Xray перезапущен",
  rollback: "новый конфиг не запустился — вернули прежний",
  watchdog_stop: "сторож выключил VPN — нет связи с сервером и интернетом",
  watchdog_resume: "сторож включил VPN обратно",
  self_update: "агент обновился",
  xray_restart_failed: "не удалось перезапустить Xray",
};
const EVENT_BAD = new Set(["rollback", "watchdog_stop", "xray_restart_failed"]);

function RestartsBlock({ restarts }: { restarts: RestartsInfo | undefined }) {
  return (
    <div className="grid" style={{ gap: 8 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 10 }}>
        <span className="caps">Перезапуски VPN</span>
        {restarts?.supported && (
          <span
            className="mono"
            style={{
              fontSize: 12,
              color: restarts.day > RESTARTS_WARN ? "var(--warn)" : undefined,
            }}
          >
            за 24 ч: {restarts.day} · за 7 дн: {restarts.week}
          </span>
        )}
      </div>
      {!restarts?.supported ? (
        <span className="dim" style={{ fontSize: 12.5 }}>
          {NEED_V7}
        </span>
      ) : restarts.events.length === 0 ? (
        <span className="dim" style={{ fontSize: 12.5 }}>
          За 7 дней перезапусков не было.
        </span>
      ) : (
        <div className="grid ev-list" style={{ gap: 3 }}>
          {restarts.events.map((e) => (
            <div key={`${e.ts}-${e.kind}`} className="ev-row">
              <span className="mono dim">{evTime(e.ts)}</span>
              <span style={{ color: EVENT_BAD.has(e.kind) ? "var(--warn)" : undefined }}>
                {EVENT_TEXT[e.kind] ?? e.kind}
                {e.kind === "self_update" && e.note && <span className="dim"> ({e.note})</span>}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

type EligibleHost = {
  uuid: string;
  remark: string;
  network: string;
  security: string;
  is_disabled: boolean;
  eligible: boolean;
};
type CreateResp = {
  ok: boolean;
  id: number;
  token: string;
  install_code: string | null;
  primary_host_uuid: string | null;
  backup_host_uuid: string | null;
  warning: string | null;
  claim_url: string | null;
  subscription: SubSummary;
};
type CustomerHit = {
  id: number;
  username: string | null;
  name: string | null;
  subscription: SubSummary;
};
type RouterPlan = {
  id: number;
  name: string;
  device_limit: number | null;
  durations: { days: number; price_minor: number | null }[];
};

function rub(minor: number | null | undefined): string {
  if (minor == null) return "—";
  return `${(minor / 100).toLocaleString("ru-RU")} ₽`;
}

function hostLabel(hosts: EligibleHost[] | AvailableHost[], uuid: string | null): string {
  if (!uuid) return "—";
  const h = hosts.find((x) => x.uuid === uuid);
  return h ? `${h.remark} (${h.network})` : uuid.slice(0, 8);
}

export default function Routers() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();

  const devices = useQuery({
    queryKey: ["routers"],
    queryFn: () => api.get<{ items: RouterDevice[] }>("/api/admin/routers"),
  });
  const eligible = useQuery({
    queryKey: ["routers", "eligible-hosts"],
    queryFn: () => api.get<{ items: EligibleHost[] }>("/api/admin/routers/config/eligible-hosts"),
  });

  const [createOpen, setCreateOpen] = useState(false);
  const [customerMode, setCustomerMode] = useState<"qr" | "existing">("qr");
  const [searchQ, setSearchQ] = useState("");
  const [searchResults, setSearchResults] = useState<CustomerHit[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<CustomerHit | null>(null);
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState<Mode>("auto");
  const [primaryHost, setPrimaryHost] = useState("");
  const [note, setNote] = useState("");
  const [creating, setCreating] = useState(false);

  const [paidFor, setPaidFor] = useState<number | null>(null);
  const [paidPlanId, setPaidPlanId] = useState<number | "">("");
  const [paidDays, setPaidDays] = useState<number | "">("");
  const [paying, setPaying] = useState(false);
  const routerPlans = useQuery({
    queryKey: ["routers", "plans"],
    queryFn: () => api.get<{ items: RouterPlan[] }>("/api/admin/routers/plans"),
    enabled: paidFor !== null,
  });

  const [tokenModal, setTokenModal] = useState<{
    label: string;
    token: string;
    code: string | null;
    warning: string | null;
    claimUrl: string | null;
  } | null>(null);
  const [hostsOpen, setHostsOpen] = useState(false);
  const [hostsChecked, setHostsChecked] = useState<Record<string, boolean>>({});
  const [pulseOpen, setPulseOpen] = useState(false);
  const [pulseDraft, setPulseDraft] = useState<PulseTarget[]>([]);
  const [pulseSaving, setPulseSaving] = useState(false);
  const pulseTargets = useQuery({
    queryKey: ["routers", "pulse-targets"],
    queryFn: () =>
      api.get<{ items: PulseTarget[]; defaults: PulseTarget[]; max: number }>(
        "/api/admin/routers/config/pulse-targets",
      ),
    enabled: pulseOpen,
  });
  useEffect(() => {
    if (pulseOpen && pulseTargets.data) setPulseDraft(pulseTargets.data.items.map((x) => ({ ...x })));
  }, [pulseOpen, pulseTargets.data]);

  const [openId, setOpenId] = useState<number | null>(null);
  const detail = useQuery({
    queryKey: ["routers", openId],
    queryFn: () => api.get<RouterDetail>(`/api/admin/routers/${openId}`),
    enabled: openId !== null,
  });

  function resetCreate() {
    setCustomerMode("qr");
    setSearchQ("");
    setSearchResults(null);
    setPicked(null);
    setLabel("");
    setMode("auto");
    setPrimaryHost("");
    setNote("");
  }

  async function searchCustomers() {
    if (!searchQ.trim()) return;
    setSearching(true);
    try {
      const r = await api.get<{ items: CustomerHit[] }>(
        `/api/admin/routers/customers?q=${encodeURIComponent(searchQ.trim())}`,
      );
      setSearchResults(r.items);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSearching(false);
    }
  }

  async function create() {
    if (customerMode === "existing" && !picked) {
      toast(t.routersPickCustomer);
      return;
    }
    if (!label.trim()) {
      toast(t.routersFillRequired);
      return;
    }
    if (mode === "force" && !primaryHost) {
      toast(t.routersForceNeedsHost);
      return;
    }
    const finalLabel = label.trim();
    setCreating(true);
    try {
      const r = await api.post<CreateResp>("/api/admin/routers", {
        label: finalLabel,
        user_id: customerMode === "existing" ? picked?.id : undefined,
        mode,
        primary_host_uuid: mode === "force" ? primaryHost : undefined,
        note: note.trim() || undefined,
      });
      setCreateOpen(false);
      resetCreate();
      void qc.invalidateQueries({ queryKey: ["routers"] });
      setTokenModal({
        label: finalLabel,
        token: r.token,
        code: r.install_code,
        warning: r.warning,
        claimUrl: r.claim_url,
      });
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setCreating(false);
    }
  }

  function openPaid(deviceId: number) {
    setPaidPlanId("");
    setPaidDays("");
    setPaidFor(deviceId);
  }

  async function confirmPaid() {
    if (paidFor === null || !paidPlanId || !paidDays) return;
    setPaying(true);
    try {
      await api.post(`/api/admin/routers/${paidFor}/paid`, {
        plan_id: paidPlanId,
        days: paidDays,
      });
      void qc.invalidateQueries({ queryKey: ["routers"] });
      void qc.invalidateQueries({ queryKey: ["routers", paidFor] });
      setPaidFor(null);
      toast(t.routersPaidDone);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setPaying(false);
    }
  }

  function subLine(s: SubSummary): string {
    if (!s || !["active", "trial", "limited"].includes(s.status)) return t.routersSubInactive;
    const until = s.expire_at ? new Date(s.expire_at).toLocaleDateString("ru-RU") : "—";
    return `${s.is_trial ? t.routersSubTrial : t.routersSubActive} ${until}`;
  }

  async function rotate(d: RouterDevice) {
    if (!(await confirm(t.routersRotateConfirm))) return;
    try {
      const r = await api.post<{ token: string; install_code: string | null }>(
        `/api/admin/routers/${d.id}/rotate`,
      );
      void qc.invalidateQueries({ queryKey: ["routers", d.id] });
      setTokenModal({
        label: d.label,
        token: r.token,
        code: r.install_code,
        warning: null,
        claimUrl: null,
      });
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function revoke(d: RouterDevice) {
    if (!(await confirm(t.routersRevokeConfirm))) return;
    try {
      await api.post(`/api/admin/routers/${d.id}/revoke`);
      void qc.invalidateQueries({ queryKey: ["routers"] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function removeDevice(d: RouterDevice) {
    if (!(await confirm(t.routersDeleteConfirm))) return;
    try {
      await api.del(`/api/admin/routers/${d.id}`);
      void qc.invalidateQueries({ queryKey: ["routers"] });
      toast("✕ " + d.label);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function patchDevice(id: number, body: Record<string, unknown>) {
    try {
      await api.patch(`/api/admin/routers/${id}`, body);
      void qc.invalidateQueries({ queryKey: ["routers"] });
      void qc.invalidateQueries({ queryKey: ["routers", id] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  function openHostsModal() {
    const checked: Record<string, boolean> = {};
    for (const h of eligible.data?.items ?? []) checked[h.uuid] = h.eligible;
    setHostsChecked(checked);
    setHostsOpen(true);
  }

  async function saveEligibleHosts() {
    const host_uuids = Object.entries(hostsChecked)
      .filter(([, v]) => v)
      .map(([k]) => k);
    try {
      await api.put("/api/admin/routers/config/eligible-hosts", { host_uuids });
      setHostsOpen(false);
      void qc.invalidateQueries({ queryKey: ["routers", "eligible-hosts"] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function setPulse(id: number, enabled: boolean) {
    try {
      await api.put(`/api/admin/routers/${id}/pulse`, { enabled });
      void qc.invalidateQueries({ queryKey: ["routers", id] });
      toast(enabled ? "Пульс включён — роутер подхватит за 5 минут" : "Пульс выключен");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  function editPulseTarget(i: number, patch: Partial<PulseTarget>) {
    setPulseDraft((list) => list.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  }

  async function savePulseTargets() {
    setPulseSaving(true);
    try {
      await api.put("/api/admin/routers/config/pulse-targets", {
        items: pulseDraft.map((x) => ({ ...x, resolve_ip: (x.resolve_ip ?? "").trim() || undefined })),
      });
      setPulseOpen(false);
      void qc.invalidateQueries({ queryKey: ["routers"] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setPulseSaving(false);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t.copied);
    } catch {
      toast("Не удалось скопировать — выделите текст вручную");
    }
  }

  const hostOptions = eligible.data?.items ?? [];
  const cols = "1.2fr 1.1fr 0.8fr 0.7fr 1.3fr 1fr auto";

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.routersTitle}</h1>
        <div className="actions">
          <button className="btn secondary" onClick={openHostsModal}>
            {t.routersEligibleHosts}
          </button>
          <button className="btn secondary" onClick={() => setPulseOpen(true)}>
            🎮 Игровой пульс
          </button>
          <button
            className="btn primary"
            onClick={() => {
              resetCreate();
              setCreateOpen(true);
            }}
          >
            {t.routersCreate}
          </button>
        </div>
      </div>

      <div className="tbl rt-tbl">
        <div className="tr head" style={{ gridTemplateColumns: cols }}>
          <span>{t.routersLabel}</span>
          <span>{t.routersClient}</span>
          <span>{t.colStatus}</span>
          <span>{t.routersMode}</span>
          <span>{t.routersHosts}</span>
          <span>{t.routersLastSeen}</span>
          <span />
        </div>
        {(devices.data?.items ?? []).map((d) => (
          <div
            key={d.id}
            className="tr"
            style={{ gridTemplateColumns: cols, cursor: "pointer" }}
            onClick={() => setOpenId(d.id)}
          >
            <span className="rt-label">
              <b style={{ fontWeight: 500 }}>{d.label}</b>
              {d.note && (
                <div className="dim" style={{ fontSize: 11.5 }}>
                  {d.note}
                </div>
              )}
            </span>
            <span className="mono muted rt-client">
              {d.awaiting_claim ? t.routersAwaitingQr : (d.subscription_label ?? `#${d.subscription_id}`)}
            </span>
            <span
              className={`rt-status st ${d.is_online ? "on" : d.status === "pending" ? "mid" : "off"}`}
            >
              {d.is_online && <span className="status-dot" />}
              {d.status === "pending"
                ? t.routersStPending
                : d.status === "revoked"
                  ? t.routersStRevoked
                  : d.is_online
                    ? t.routersStOnline
                    : t.routersStOffline}
              {d.status !== "revoked" && <VpnBadge vpn={d.vpn} />}
              {(d.restarts_24h ?? 0) > RESTARTS_WARN && (
                <div
                  className="vpn-badge"
                  style={{ color: "var(--warn)" }}
                  title="Перезапусков VPN за 24 ч — в эти моменты у клиента рвутся соединения (игры, звонки)"
                >
                  ⚠ перезапусков за 24 ч: {d.restarts_24h}
                </div>
              )}
            </span>
            <span className="cap-pill rt-mode">
              {d.mode === "auto" ? t.routersModeAuto : t.routersModeForce}
            </span>
            <span className="mono rt-hosts" style={{ fontSize: 11.5 }}>
              {hostLabel(hostOptions, d.primary_host_uuid)}
              {d.backup_host_uuid && (
                <div className="dim">+ {hostLabel(hostOptions, d.backup_host_uuid)}</div>
              )}
              {!d.primary_host_uuid && (
                <div className="dim" style={{ color: "var(--warn, #e0a800)" }}>
                  {t.routersNoHost}
                </div>
              )}
            </span>
            <span className="mono muted rt-seen">
              {d.last_seen_at ? dtTime(d.last_seen_at) : "—"}
            </span>
            <span className="row rt-actions" style={{ gap: 4 }} onClick={(e) => e.stopPropagation()}>
              <button className="btn secondary sm" onClick={() => void rotate(d)}>
                {t.routersRotate}
              </button>
              {d.status !== "revoked" && (
                <button className="btn secondary sm" onClick={() => void revoke(d)}>
                  {t.routersRevoke}
                </button>
              )}
              <button className="btn danger sm" onClick={() => void removeDevice(d)}>
                ✕
              </button>
            </span>
          </div>
        ))}
        {devices.data && devices.data.items.length === 0 && (
          <div className="tr dim">— · {t.routersCreate} →</div>
        )}
      </div>

      {createOpen && (
        <Modal title={t.routersCreate} onClose={() => setCreateOpen(false)}>
          <div className="grid" style={{ gap: 12 }}>
            <Seg
              value={customerMode}
              options={[
                { id: "qr" as const, label: t.routersCustomerQr },
                { id: "existing" as const, label: t.routersCustomerExisting },
              ]}
              onChange={(v) => {
                setCustomerMode(v);
                setPicked(null);
                setSearchResults(null);
              }}
            />
            {customerMode === "qr" ? (
              <div className="dim" style={{ fontSize: 12.5 }}>
                {t.routersQrHint}
              </div>
            ) : picked ? (
              <div className="card" style={{ padding: 10 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span>
                    <b>{picked.name ?? `@${picked.username}`}</b>
                    {picked.name && picked.username && (
                      <span className="dim"> @{picked.username}</span>
                    )}
                    <div className="dim" style={{ fontSize: 11.5 }}>
                      {subLine(picked.subscription)}
                    </div>
                  </span>
                  <button className="btn secondary sm" onClick={() => setPicked(null)}>
                    {t.routersChangeCustomer}
                  </button>
                </div>
              </div>
            ) : (
              <Field label={t.routersSearchPh}>
                <div className="row">
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    value={searchQ}
                    placeholder="@username"
                    onChange={(e) => setSearchQ(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && void searchCustomers()}
                  />
                  <button
                    className="btn secondary sm"
                    disabled={searching}
                    onClick={() => void searchCustomers()}
                  >
                    {t.routersSearchBtn}
                  </button>
                </div>
                <div className="dim" style={{ fontSize: 11.5, marginTop: 6 }}>
                  {t.routersExistingHint}
                </div>
                {searchResults && searchResults.length === 0 && (
                  <div style={{ color: "var(--warn, #e0a800)", fontSize: 12, marginTop: 6 }}>
                    {t.routersNotFound}
                  </div>
                )}
                {searchResults && searchResults.length > 0 && (
                  <div className="grid" style={{ gap: 2, marginTop: 6 }}>
                    {searchResults.map((u) => (
                      <button
                        key={u.id}
                        type="button"
                        className="btn secondary sm"
                        style={{ justifyContent: "space-between", textAlign: "left" }}
                        onClick={() => setPicked(u)}
                      >
                        <span>
                          {u.name ?? `@${u.username}`}
                          {u.name && u.username && <span className="dim"> @{u.username}</span>}
                        </span>
                        <span className="dim">{subLine(u.subscription)}</span>
                      </button>
                    ))}
                  </div>
                )}
              </Field>
            )}

            <Field label={t.routersLabel}>
              <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} />
            </Field>
            <Field label={t.routersMode}>
              <select className="input" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                <option value="auto">{t.routersModeAuto}</option>
                <option value="force">{t.routersModeForce}</option>
              </select>
            </Field>
            {mode === "force" && (
              <Field label={t.routersPickHost}>
                <select
                  className="input"
                  value={primaryHost}
                  onChange={(e) => setPrimaryHost(e.target.value)}
                >
                  <option value="">{t.routersPickHostPh}</option>
                  {hostOptions.map((h) => (
                    <option key={h.uuid} value={h.uuid} disabled={h.is_disabled}>
                      {h.remark} ({h.network}){h.is_disabled ? ` — ${t.routersHostDisabled}` : ""}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <Field label={t.routersNote}>
              <input className="input" value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setCreateOpen(false)}>
                {t.cancel}
              </button>
              <button className="btn primary" disabled={creating} onClick={() => void create()}>
                {t.create}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {tokenModal && (
        <Modal title={t.routersTokenTitle} onClose={() => setTokenModal(null)}>
          <div className="grid" style={{ gap: 12 }}>
            {tokenModal.claimUrl && (
              <Field label={t.routersClaimTitle}>
                <div className="grid" style={{ gap: 8 }}>
                  <QrImage text={tokenModal.claimUrl} />
                  <div className="dim" style={{ fontSize: 12.5 }}>
                    {t.routersClaimHint}
                  </div>
                  <div className="row">
                    <input
                      className="input mono"
                      readOnly
                      style={{ flex: 1, fontSize: 11.5 }}
                      value={tokenModal.claimUrl}
                    />
                    <button
                      className="btn secondary sm"
                      onClick={() => void copy(tokenModal.claimUrl ?? "")}
                    >
                      {t.copy}
                    </button>
                  </div>
                </div>
              </Field>
            )}
            <div className="dim" style={{ fontSize: 12.5 }}>
              {t.routersTokenHint}
            </div>
            <Field label={t.routersInstallCmd}>
              <InstallBlock
                code={tokenModal.code}
                token={tokenModal.token}
                onCopy={(text) => void copy(text)}
              />
            </Field>
            <Field label={t.routersToken}>
              <div className="row">
                <SecretInput value={tokenModal.token} onChange={() => {}} className="input mono" />
                <button className="btn secondary sm" onClick={() => void copy(tokenModal.token)}>
                  {t.copy}
                </button>
              </div>
            </Field>
            {tokenModal.warning && (
              <div style={{ color: "var(--warn, #e0a800)", fontSize: 12.5 }}>
                ⚠️ {tokenModal.warning}
              </div>
            )}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setTokenModal(null)}>
                {t.close}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {paidFor !== null && (
        <Modal title={t.routersPaidTitle} onClose={() => setPaidFor(null)}>
          <div className="grid" style={{ gap: 12 }}>
            {routerPlans.data && routerPlans.data.items.length === 0 && (
              <div style={{ color: "var(--warn, #e0a800)", fontSize: 12.5 }}>
                {t.routersNoRouterPlans}
              </div>
            )}
            <Field label={t.routersPaidPlan}>
              <select
                className="input"
                value={paidPlanId}
                onChange={(e) => {
                  const id = e.target.value ? Number(e.target.value) : "";
                  setPaidPlanId(id);
                  const p = routerPlans.data?.items.find((x) => x.id === id);
                  setPaidDays(p?.durations[0]?.days ?? "");
                }}
              >
                <option value="">—</option>
                {(routerPlans.data?.items ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.device_limit ? ` · ${p.device_limit} ${t.routersDevicesShort}` : ""}
                  </option>
                ))}
              </select>
            </Field>
            {paidPlanId !== "" && (
              <Field label={t.routersPaidDuration}>
                <select
                  className="input"
                  value={paidDays}
                  onChange={(e) => setPaidDays(e.target.value ? Number(e.target.value) : "")}
                >
                  {(routerPlans.data?.items.find((p) => p.id === paidPlanId)?.durations ?? []).map(
                    (d) => (
                      <option key={d.days} value={d.days}>
                        {d.days} {t.routersPaidDays} · {rub(d.price_minor)}
                      </option>
                    ),
                  )}
                </select>
              </Field>
            )}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setPaidFor(null)}>
                {t.cancel}
              </button>
              <button
                className="btn primary"
                disabled={paying || !paidPlanId || !paidDays}
                onClick={() => void confirmPaid()}
              >
                {t.routersPaidConfirm}
                {paidPlanId && paidDays
                  ? ` · ${rub(
                      routerPlans.data?.items
                        .find((p) => p.id === paidPlanId)
                        ?.durations.find((d) => d.days === paidDays)?.price_minor,
                    )}`
                  : ""}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {hostsOpen && (
        <Modal title={t.routersEligibleHosts} onClose={() => setHostsOpen(false)}>
          <div className="grid" style={{ gap: 4 }}>
            <div className="dim" style={{ fontSize: 12.5, marginBottom: 8 }}>
              {t.routersEligibleHint}
            </div>
            {hostOptions.map((h) => (
              <label key={h.uuid} className="row" style={{ gap: 8, fontSize: 13, padding: "4px 0" }}>
                <input
                  type="checkbox"
                  checked={hostsChecked[h.uuid] ?? false}
                  onChange={(e) =>
                    setHostsChecked((s) => ({ ...s, [h.uuid]: e.target.checked }))
                  }
                  disabled={h.is_disabled}
                />
                <span className="mono">{h.remark}</span>
                <span className="dim">
                  {h.network} · {h.security}
                </span>
                {h.is_disabled && <span className="dim">({t.routersHostDisabled})</span>}
              </label>
            ))}
            {hostOptions.length === 0 && <span className="dim">—</span>}
            <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
              <button className="btn secondary" onClick={() => setHostsOpen(false)}>
                {t.cancel}
              </button>
              <button className="btn primary" onClick={() => void saveEligibleHosts()}>
                {t.save}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {pulseOpen && (
        <Modal title="Игровой пульс: цели замеров" wide onClose={() => setPulseOpen(false)}>
          <div className="grid" style={{ gap: 10 }}>
            <div className="dim" style={{ fontSize: 12.5, lineHeight: 1.45 }}>
              Роутеры с включённым пульсом (переключатель в карточке роутера) раз в 10 минут
              замеряют время ответа этих адресов по уже открытому соединению — напрямую и через
              каждый сервер. Цель должна держать keep-alive (иначе замер считается потерей).
              До {pulseTargets.data?.max ?? 4} целей. Адрес — публичный https (порт 443) в том же городе, что игровые
              серверы, и не CDN: CDN покажет ближайший к роутеру узел, а не игру. «Фиксированный
              IP» — если имя ведёт на разные узлы (роутер подставит этот адрес).
            </div>
            {pulseDraft.map((x, i) => (
              <div key={i} className="card pulse-edit">
                <div className="row" style={{ gap: 8 }}>
                  <input
                    className="input mono"
                    style={{ width: 120 }}
                    placeholder="id"
                    value={x.id}
                    onChange={(e) => editPulseTarget(i, { id: e.target.value })}
                  />
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    placeholder="Название (PUBG · Европа)"
                    value={x.title}
                    onChange={(e) => editPulseTarget(i, { title: e.target.value })}
                  />
                  <button
                    className="btn danger sm"
                    onClick={() => setPulseDraft((list) => list.filter((_, j) => j !== i))}
                  >
                    ✕
                  </button>
                </div>
                <input
                  className="input mono"
                  placeholder="https://…"
                  value={x.url}
                  onChange={(e) => editPulseTarget(i, { url: e.target.value })}
                />
                <div className="row" style={{ gap: 8 }}>
                  <input
                    className="input mono"
                    style={{ width: 160 }}
                    placeholder="фикс. IP (необяз.)"
                    value={x.resolve_ip ?? ""}
                    onChange={(e) => editPulseTarget(i, { resolve_ip: e.target.value })}
                  />
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    placeholder="Игры (подсказка в таблице)"
                    value={x.games}
                    onChange={(e) => editPulseTarget(i, { games: e.target.value })}
                  />
                </div>
              </div>
            ))}
            {pulseTargets.isLoading && <span className="dim">…</span>}
            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <button
                className="btn secondary sm"
                disabled={pulseDraft.length >= (pulseTargets.data?.max ?? 4)}
                onClick={() =>
                  setPulseDraft((list) => [...list, { id: "", title: "", url: "", games: "" }])
                }
              >
                + Добавить цель
              </button>
              <button
                className="btn secondary sm"
                disabled={!pulseTargets.data}
                onClick={() =>
                  setPulseDraft((pulseTargets.data?.defaults ?? []).map((x) => ({ ...x })))
                }
              >
                Вернуть стандартные
              </button>
            </div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setPulseOpen(false)}>
                {t.cancel}
              </button>
              <button
                className="btn primary"
                disabled={pulseSaving || !pulseTargets.data}
                onClick={() => void savePulseTargets()}
              >
                {t.save}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {openId !== null && (
        <Drawer onClose={() => setOpenId(null)}>
          {detail.data && (
            <div className="grid" style={{ gap: 14 }}>
              {/* Technicians rename routers here (address, family name…) so they don't lose
                  track of which one is which; saved on Enter or when the field loses focus. */}
              <label className="rt-title" title="Нажмите, чтобы переименовать">
                <input
                  key={detail.data.id}
                  className="rt-title-input"
                  defaultValue={detail.data.label}
                  maxLength={128}
                  aria-label={t.routersLabel}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                    if (e.key === "Escape") {
                      e.currentTarget.value = detail.data?.label ?? "";
                      e.currentTarget.blur();
                    }
                  }}
                  onBlur={(e) => {
                    const value = e.currentTarget.value.trim();
                    if (!value) {
                      e.currentTarget.value = detail.data?.label ?? "";
                      return;
                    }
                    if (value !== detail.data?.label) void patchDevice(detail.data!.id, { label: value });
                  }}
                />
                <span className="rt-title-pen" aria-hidden>
                  ✎
                </span>
              </label>
              <Field label={t.routersSub}>
                <div className="grid" style={{ gap: 6, fontSize: 13 }}>
                  <div>{subLine(detail.data.subscription)}</div>
                  {detail.data.subscription?.plan_name && (
                    <div className="dim" style={{ fontSize: 12 }}>
                      {detail.data.subscription.plan_name}
                      {detail.data.subscription.device_limit
                        ? ` · ${detail.data.subscription.device_limit} ${t.routersDevicesShort}`
                        : ""}
                    </div>
                  )}
                  <button
                    className="btn primary sm"
                    style={{ justifySelf: "start" }}
                    onClick={() => openPaid(detail.data!.id)}
                  >
                    {t.routersPaidBtn}
                  </button>
                </div>
              </Field>
              <Field label="Проверка VPN">
                <VpnCheck vpn={detail.data.vpn} info={detail.data.config_info} />
                {detail.data.mode === "auto" && (
                  <div className="dim" style={{ fontSize: 12, marginTop: 8, lineHeight: 1.45 }}>
                    🤖 Авто-выбор:{" "}
                    {detail.data.autotune
                      ? `${detail.data.autotune.note}${detail.data.autotune.at ? ` · ${dtTime(detail.data.autotune.at)}` : ""}`
                      : "ждёт первой проверки с роутера (агент версии 5)"}
                    <div>
                      Основной используется всегда, резервный — только если основной не отвечает.
                      Сервер меняется сам, если не проходит 2 проверки подряд.
                    </div>
                  </div>
                )}
                {detail.data.mode === "force" && (
                  <div className="dim" style={{ fontSize: 12, marginTop: 8 }}>
                    Вручную: основной используется всегда, резервный — только если основной не
                    отвечает. Сервер сам не меняется.
                  </div>
                )}
              </Field>
              <GamesBlock
                games={detail.data.games}
                onToggle={(on) => void setPulse(detail.data!.id, on)}
              />
              <RestartsBlock restarts={detail.data.restarts} />
              {detail.data.claim_url && (
                <Field label={t.routersClaimTitle}>
                  <div className="grid" style={{ gap: 8 }}>
                    <QrImage text={detail.data.claim_url} />
                    <div className="dim" style={{ fontSize: 12.5 }}>
                      {t.routersClaimHint}
                    </div>
                  </div>
                </Field>
              )}
              {(detail.data.install_code || !detail.data.last_seen_at) && (
                <Field label={t.routersInstallCmd}>
                  {detail.data.install_code ? (
                    <InstallBlock
                      code={detail.data.install_code}
                      token={null}
                      onCopy={(text) => void copy(text)}
                    />
                  ) : (
                    <div className="grid" style={{ gap: 8 }}>
                      <span className="dim" style={{ fontSize: 12.5 }}>
                        Код установки истёк (действует 24 часа после выдачи). Выпустите новый — старый
                        токен на роутере перестанет работать, поэтому делайте это только для
                        установки или переустановки.
                      </span>
                      <button
                        className="btn secondary"
                        style={{ justifySelf: "start" }}
                        onClick={() => void rotate(detail.data!)}
                      >
                        🔑 Новый код установки
                      </button>
                    </div>
                  )}
                </Field>
              )}
              <Field label={t.routersNote}>
                <input
                  className="input"
                  defaultValue={detail.data.note ?? ""}
                  onBlur={(e) => {
                    if (e.currentTarget.value !== (detail.data?.note ?? "")) {
                      void patchDevice(detail.data!.id, { note: e.currentTarget.value });
                    }
                  }}
                />
              </Field>
              <Field label={t.routersMode}>
                <select
                  className="input"
                  value={detail.data.mode}
                  onChange={(e) => void patchDevice(detail.data!.id, { mode: e.target.value })}
                >
                  <option value="auto">{t.routersModeAuto}</option>
                  <option value="force">{t.routersModeForce}</option>
                </select>
              </Field>
              <Field label={t.routersPrimaryHost}>
                <select
                  className="input"
                  value={detail.data.primary_host_uuid ?? ""}
                  onChange={(e) =>
                    void patchDevice(detail.data!.id, {
                      primary_host_uuid: e.target.value || null,
                    })
                  }
                >
                  <option value="">{t.routersPickHostPh}</option>
                  {detail.data.available_hosts.map((h) => (
                    <option key={h.uuid} value={h.uuid}>
                      {h.remark} ({h.network})
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t.routersBackupHost}>
                <select
                  className="input"
                  value={detail.data.backup_host_uuid ?? ""}
                  onChange={(e) =>
                    void patchDevice(detail.data!.id, {
                      backup_host_uuid: e.target.value || null,
                    })
                  }
                >
                  <option value="">{t.routersPickHostPh}</option>
                  {detail.data.available_hosts.map((h) => (
                    <option key={h.uuid} value={h.uuid}>
                      {h.remark} ({h.network})
                    </option>
                  ))}
                </select>
              </Field>
              <div className="grid" style={{ gap: 6, fontSize: 13 }}>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{t.routersXrayVer}</span>
                  <span className="mono">{detail.data.xray_version ?? "—"}</span>
                </div>
                {detail.data.xray_version &&
                  !detail.data.xray_version.startsWith(detail.data.recommended_xray.replace(/^v/, "")) && (
                    <div style={{ color: "var(--warn)", fontSize: 12 }}>
                      ⚠️ На нодах Xray {detail.data.recommended_xray.replace(/^v/, "")} — на роутере другая
                      версия. Повторный запуск команды установки заменит Xray на версию нод.
                    </div>
                  )}
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{t.routersActiveOutbound}</span>
                  {/* The agent doesn't report which candidate the router's leastPing balancer
                      picked, so show the servers it balances between (primary + backup). */}
                  <span className="mono" style={{ textAlign: "right" }}>
                    {detail.data.active_outbound ??
                      (detail.data.config_info?.servers.length
                        ? detail.data.config_info.servers.join(" / ")
                        : null) ??
                      (detail.data.primary_host_uuid
                        ? hostLabel(hostOptions, detail.data.primary_host_uuid) +
                          (detail.data.backup_host_uuid
                            ? ` / резерв: ${hostLabel(hostOptions, detail.data.backup_host_uuid)}`
                            : "")
                        : "не назначен — без VPN")}
                  </span>
                </div>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{t.routersExternalIp}</span>
                  <span className="mono">{detail.data.external_ip ?? "—"}</span>
                </div>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{t.routersConfigEtag}</span>
                  <span className="mono" style={{ fontSize: 11 }}>
                    {detail.data.config_etag ? detail.data.config_etag.slice(0, 12) : "—"}
                  </span>
                </div>
              </div>
              {detail.data.last_error && (
                <div style={{ color: "var(--warn, #e0a800)", fontSize: 12.5 }}>
                  ⚠️ {detail.data.last_error}
                </div>
              )}
              <Field label={t.routersDiagnostics}>
                {detail.data.diagnostics ? (
                  <div className="grid" style={{ gap: 6, fontSize: 13 }}>
                    {(() => {
                      const d = detail.data.diagnostics;
                      // Spoofing = no address in common. Same set in another order (or a CDN
                      // rotating part of its pool) is normal and must not raise a warning.
                      const routerIps = new Set((d.dns_router ?? "").split(/\s+/).filter(Boolean));
                      const dnsMismatch =
                        routerIps.size > 0 &&
                        d.dns_1111 &&
                        !d.dns_1111.split(/\s+/).some((ip) => routerIps.has(ip))
                          ? t.routersDiagDnsMismatch
                          : null;
                      return (
                        <>
                          <DiagRow label={t.routersDiagAgent} value={d.agent_version} />
                          <DiagRow label={t.routersDiagXkeen} value={d.xkeen_version} />
                          <DiagRow label={t.routersDiagRouter} value={d.router} />
                          <DiagRow
                            label={t.routersDiagRouteOnly}
                            value={d.route_only}
                            warn={d.route_only?.includes("true") ? "routeOnly=true" : null}
                          />
                          <DiagRow
                            label={t.routersDiagPorts}
                            value={d.ports_proxied || t.routersDiagPortsAll}
                          />
                          <DiagRow label={t.routersDiagPortsExcluded} value={d.ports_excluded} />
                          <DiagRow label={t.routersDiagCron} value={d.cron} />
                          <DiagRow
                            label={`${t.routersDiagDns} (${d.dns_probe ?? ""})`}
                            value={d.dns_router}
                            warn={dnsMismatch}
                          />
                          <DiagRow label={t.routersDiagDns1111} value={d.dns_1111} />
                          <DiagRow label={t.routersDiagFree} value={d.opt_free} />
                          <DiagRow label={t.routersDiagFiles} value={d.confdir_files} />
                          <DiagRow label={t.routersDiagXrayTest} value={d.xray_test} />
                          {d.xray_errors && (
                            <div className="grid" style={{ gap: 4 }}>
                              <span className="muted">Журнал ошибок Xray</span>
                              <pre className="mono xray-errors">{d.xray_errors}</pre>
                            </div>
                          )}
                        </>
                      );
                    })()}
                  </div>
                ) : (
                  <span className="dim" style={{ fontSize: 12.5 }}>
                    {t.routersDiagNone}
                  </span>
                )}
              </Field>
              {!detail.data.install_code && detail.data.last_seen_at && (
                <Field label="Переустановка">
                  <button className="btn secondary sm" onClick={() => void rotate(detail.data!)}>
                    🔑 Новая команда установки
                  </button>
                </Field>
              )}
              {detail.data.install_report && (
                <Field label={t.routersInstallReport}>
                  <pre
                    className="mono"
                    style={{
                      fontSize: 11,
                      whiteSpace: "pre-wrap",
                      background: "var(--panel-2, rgba(0,0,0,.15))",
                      padding: 8,
                      borderRadius: 6,
                      maxHeight: 200,
                      overflow: "auto",
                    }}
                  >
                    {JSON.stringify(detail.data.install_report, null, 2)}
                  </pre>
                </Field>
              )}
            </div>
          )}
        </Drawer>
      )}
    </>
  );
}
