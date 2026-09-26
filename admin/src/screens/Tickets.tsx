/* Screen 11 — Тикеты: support channels config + ticket list + chat. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, dtTime, getToken } from "../api/client";
import { Field, Modal, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type TicketRow = {
  id: number;
  username: string | null;
  subject: string;
  status: "open" | "waiting" | "closed";
  is_premium: boolean;
  priority: number;
  effective_priority: number;
  waiting_minutes: number | null;
  messages: number;
  updated_at: string | null;
};
type TicketDetail = {
  id: number;
  subject: string;
  status: string;
  is_premium: boolean;
  priority: number;
  offers: {
    id: number;
    name: string;
    is_active: boolean;
    durations: { days: number; price_minor: number | null }[];
  }[];
  user: { username: string | null };
  messages: {
    id: number;
    author: "user" | "admin";
    text: string;
    attachment_url: string | null;
    attachment_kind: "photo" | "document" | null;
    at: string | null;
  }[];
};
type Channels = { mode: string; redirect_username: string };
type Squad = { id: number; name: string; uuid: string };
type OfferDraft = {
  name: string;
  description: string;
  device_limit: number;
  traffic_limit_gb: number;
  durations: { days: number; price_minor: number }[];
  internal_squads: string[];
};

const PRIORITIES: { id: number; label: string; color: string }[] = [
  { id: 0, label: "Низкий", color: "var(--dim)" },
  { id: 1, label: "Обычный", color: "var(--muted)" },
  { id: 2, label: "Высокий", color: "var(--warn)" },
  { id: 3, label: "Срочный", color: "var(--bad-ink)" },
];

function waitLabel(min: number | null): string | null {
  if (min === null) return null;
  if (min < 60) return `ждёт ${min} мин`;
  const h = Math.floor(min / 60);
  if (h < 24) return `ждёт ${h} ч`;
  return `ждёт ${Math.floor(h / 24)} дн`;
}

const PERIOD_LADDER = [30, 90, 180, 360];

function nextDuration(ds: OfferDraft["durations"]): OfferDraft["durations"][number] {
  const last = ds[ds.length - 1];
  if (!last) return { days: 30, price_minor: 0 };
  const days = PERIOD_LADDER.find((d) => d > last.days) ?? last.days * 2;
  const perDay = last.price_minor / Math.max(last.days, 1);
  return { days, price_minor: Math.round((perDay * days) / 100) * 100 };
}

function rub(minor: number | null): string {
  return minor == null ? "—" : `${(minor / 100).toLocaleString("ru-RU")} ₽`;
}

const ST: Record<string, [string, string]> = {
  open: ["●", "on"],
  waiting: ["◐", "mid"],
  closed: ["○", "off"],
};

export default function Tickets() {
  const { t, toast } = useApp();
  const qc = useQueryClient();
  const [selId, setSelId] = useState<number | null>(() => {
    const pending = sessionStorage.getItem("open_ticket");
    sessionStorage.removeItem("open_ticket");
    return pending ? Number(pending) : null;
  });
  const [reply, setReply] = useState("");
  const [attach, setAttach] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const [redirect, setRedirect] = useState<string | null>(null);
  const [offer, setOffer] = useState<OfferDraft | null>(null);
  const [sendingOffer, setSendingOffer] = useState(false);
  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => api.get<{ squads: Squad[] }>("/api/admin/servers"),
    enabled: offer !== null,
  });

  const channels = useQuery({
    queryKey: ["support-channels"],
    queryFn: () => api.get<Channels>("/api/admin/support-channels"),
  });
  const tickets = useQuery({
    queryKey: ["tickets"],
    queryFn: () => api.get<{ items: TicketRow[]; open_count: number }>("/api/admin/tickets"),
    refetchInterval: 30_000,
  });
  const detail = useQuery({
    queryKey: ["ticket", selId],
    queryFn: () => api.get<TicketDetail>(`/api/admin/tickets/${selId}`),
    enabled: selId !== null,
  });

  const sendReply = useMutation({
    mutationFn: () =>
      api.post(`/api/admin/tickets/${selId}/reply`, {
        text: reply,
        attachment_url: attach ?? undefined,
      }),
    onSuccess: () => {
      setReply("");
      setAttach(null);
      void qc.invalidateQueries({ queryKey: ["ticket", selId] });
      void qc.invalidateQueries({ queryKey: ["tickets"] });
      toast("✓");
    },
    onError: (e) => toast(e.message),
  });

  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [detail.data?.messages.length, selId]);

  async function uploadShot(f: File) {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", f);
      const res = await fetch("/api/admin/upload", {
        method: "POST",
        headers: { Authorization: `Bearer ${getToken()}` },
        body: form,
      });
      if (!res.ok) throw new Error("Не удалось загрузить картинку (jpg, png, webp до 20 МБ)");
      const data = (await res.json()) as { url: string; kind: string };
      if (data.kind !== "photo") throw new Error("Можно прикрепить только картинку");
      setAttach(data.url);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  async function setPriority(priority: number) {
    await api.patch(`/api/admin/tickets/${selId}/priority`, { priority });
    void qc.invalidateQueries({ queryKey: ["ticket", selId] });
    void qc.invalidateQueries({ queryKey: ["tickets"] });
  }

  async function setStatus(status: string) {
    await api.patch(`/api/admin/tickets/${selId}/status`, { status });
    void qc.invalidateQueries({ queryKey: ["ticket", selId] });
    void qc.invalidateQueries({ queryKey: ["tickets"] });
  }

  const ch = channels.data;
  const mode = ch?.mode ?? "tickets";

  async function saveChannels() {
    try {
      await api.patch("/api/admin/support-channels", {
        mode,
        redirect_username: redirect ?? ch?.redirect_username ?? "",
      });
      void qc.invalidateQueries({ queryKey: ["support-channels"] });
      toast(t.saved);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function setMode(m: string) {
    await api.patch("/api/admin/support-channels", { mode: m });
    void qc.invalidateQueries({ queryKey: ["support-channels"] });
    toast(t.saved);
  }

  const d = detail.data;

  function openOffer() {
    setOffer({
      name: "",
      description: "",
      device_limit: 3,
      traffic_limit_gb: 0,
      durations: [{ days: 30, price_minor: 0 }],
      internal_squads: [],
    });
  }

  async function sendOffer() {
    if (!offer || selId === null) return;
    const durations = offer.durations.filter((x) => x.price_minor > 0);
    if (!offer.name.trim() || durations.length === 0) {
      toast(t.premiumOfferNeedPrice);
      return;
    }
    setSendingOffer(true);
    try {
      await api.post(`/api/admin/tickets/${selId}/premium-offer`, {
        ...offer,
        name: offer.name.trim(),
        durations,
      });
      setOffer(null);
      void qc.invalidateQueries({ queryKey: ["ticket", selId] });
      void qc.invalidateQueries({ queryKey: ["tickets"] });
      toast(t.premiumOfferSent);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSendingOffer(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.tickets}</h1>
        <div className="actions">
          <button className="btn secondary" onClick={saveChannels}>
            {t.saveChannels}
          </button>
        </div>
      </div>

      {/* support channels */}
      <div className="kpis" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))" }}>
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="caps">01 · {t.redirectAcc}</span>
            <Toggle on={mode === "redirect"} onChange={() => void setMode(mode === "redirect" ? "tickets" : "redirect")} />
          </div>
          <input
            className="input"
            style={{ width: "100%", marginTop: 10 }}
            placeholder="@support_account"
            value={redirect ?? ch?.redirect_username ?? ""}
            onChange={(e) => setRedirect(e.target.value)}
          />
        </div>
        <div className="card">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span className="caps">02 · {t.miniappChat}</span>
            <Toggle on={mode === "miniapp"} onChange={() => void setMode(mode === "miniapp" ? "tickets" : "miniapp")} />
          </div>
          <div className="dim" style={{ fontSize: 12, marginTop: 12 }}>
            Тикеты в мини-аппе + этот раздел
          </div>
        </div>
      </div>

      <div className="cols">
        {/* ticket list */}
        <div className="card" style={{ flex: "1 1 300px", padding: 0, overflow: "hidden" }}>
          {(tickets.data?.items ?? []).map((tk) => {
            const [g, cls] = ST[tk.status] ?? ["○", "off"];
            return (
              <div
                key={tk.id}
                className="tr click"
                style={{ gridTemplateColumns: "auto 1fr auto" }}
                onClick={() => setSelId(tk.id)}
              >
                <span className={`st ${cls}`}>{g}</span>
                <span style={{ minWidth: 0 }}>
                  <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <span className="mono dim">#{tk.id}</span>{" "}
                    {tk.is_premium && <span className="cap-pill">💎 {t.premiumBadge}</span>}{" "}
                    {tk.username ? `@${tk.username}` : "—"} · {tk.subject}
                  </div>
                  <div className="dim" style={{ fontSize: 11.5 }}>
                    {tk.status !== "closed" && tk.effective_priority >= 2 && (
                      <b style={{ color: PRIORITIES[tk.effective_priority].color, fontWeight: 600 }}>
                        {PRIORITIES[tk.effective_priority].label} ·{" "}
                      </b>
                    )}
                    {waitLabel(tk.waiting_minutes) && (
                      <span
                        style={{
                          color:
                            (tk.waiting_minutes ?? 0) >= 720
                              ? "var(--bad-ink)"
                              : (tk.waiting_minutes ?? 0) >= 240
                                ? "var(--warn)"
                                : undefined,
                        }}
                      >
                        {waitLabel(tk.waiting_minutes)} ·{" "}
                      </span>
                    )}
                    {tk.messages} сообщ. · {dtTime(tk.updated_at)}
                  </div>
                </span>
              </div>
            );
          })}
          {tickets.data && tickets.data.items.length === 0 && (
            <div className="tr dim">—</div>
          )}
        </div>

        {/* chat */}
        <div className="card" style={{ flex: "2 1 400px", display: "flex", flexDirection: "column", minHeight: 420 }}>
          {d ? (
            <>
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
                <span>
                  <b className="mono">#{d.id}</b> ·{" "}
                  {d.is_premium && <span className="cap-pill">💎 {t.premiumBadge}</span>}{" "}
                  {d.subject}{" "}
                  <span className={`st ${ST[d.status]?.[1] ?? "off"}`}>
                    {d.status === "open" ? t.openSt : d.status === "waiting" ? t.waitSt : t.closedSt}
                  </span>
                </span>
                <span className="row" style={{ gap: 6 }}>
                  <button className="btn primary sm" onClick={openOffer}>
                    {t.premiumOffer}
                  </button>
                  {d.status !== "closed" ? (
                    <button className="btn secondary sm" onClick={() => void setStatus("closed")}>
                      {t.closeTicket}
                    </button>
                  ) : (
                    <button className="btn secondary sm" onClick={() => void setStatus("open")}>
                      {t.reopenTicket}
                    </button>
                  )}
                </span>
              </div>
              {d.offers.length > 0 && (
                <div className="dim" style={{ fontSize: 12, marginBottom: 10 }}>
                  {t.premiumOffers}:{" "}
                  {d.offers
                    .map(
                      (o) =>
                        `${o.name} (${o.durations
                          .map((x) => `${x.days} дн. — ${rub(x.price_minor)}`)
                          .join(", ")})${o.is_active ? "" : " ✕"}`,
                    )
                    .join(" · ")}
                </div>
              )}
              <div
                className="row"
                style={{ gap: 6, marginBottom: 10, flexWrap: "wrap", fontSize: 12 }}
              >
                <span className="dim">Приоритет:</span>
                {PRIORITIES.map((pr) => (
                  <button
                    key={pr.id}
                    className="cap-pill"
                    onClick={() => void setPriority(pr.id)}
                    style={{
                      cursor: "pointer",
                      color: d.priority === pr.id ? "var(--text)" : pr.color,
                      borderColor: d.priority === pr.id ? pr.color : undefined,
                      background: d.priority === pr.id ? "var(--hover)" : undefined,
                    }}
                  >
                    {pr.label}
                  </button>
                ))}
              </div>
              <div
                ref={threadRef}
                className="grid"
                style={{ gap: 8, flex: 1, overflowY: "auto", marginBottom: 12, maxHeight: 520 }}
              >
                {d.messages.map((m) => (
                  <div
                    key={m.id}
                    style={{
                      maxWidth: "78%",
                      alignSelf: m.author === "admin" ? "flex-end" : "flex-start",
                      background: m.author === "admin" ? "var(--text)" : "var(--panel2)",
                      color: m.author === "admin" ? "var(--inv)" : "var(--text)",
                      border: m.author === "admin" ? "0" : "1px solid var(--border)",
                      borderRadius: 6,
                      padding: "8px 12px",
                      fontSize: 13,
                    }}
                  >
                    {m.attachment_url && m.attachment_kind === "photo" ? (
                      <img
                        src={m.attachment_url}
                        alt="screenshot"
                        style={{ maxWidth: "100%", maxHeight: 260, borderRadius: 4, cursor: "zoom-in", display: "block" }}
                        onClick={() => window.open(m.attachment_url!, "_blank")}
                      />
                    ) : m.attachment_url ? (
                      <a
                        href={m.attachment_url}
                        target="_blank"
                        rel="noreferrer"
                        style={{ color: "inherit", textDecoration: "underline" }}
                      >
                        📎 {t.attachment}
                      </a>
                    ) : null}
                    {m.text && (
                      <div style={{ whiteSpace: "pre-wrap", marginTop: m.attachment_url ? 6 : 0 }}>
                        {m.text}
                      </div>
                    )}
                    <div
                      className="mono"
                      style={{ fontSize: 9.5, opacity: 0.6, marginTop: 4, textAlign: "right" }}
                    >
                      {dtTime(m.at)}
                    </div>
                  </div>
                ))}
              </div>
              {attach && (
                <div className="row" style={{ marginBottom: 8, gap: 8 }}>
                  <img
                    src={attach}
                    alt="вложение"
                    style={{ height: 64, borderRadius: 8, border: "1px solid var(--border2)" }}
                  />
                  <button className="btn secondary sm" onClick={() => setAttach(null)}>
                    Убрать
                  </button>
                </div>
              )}
              <div className="row">
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  style={{ display: "none" }}
                  onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) void uploadShot(f);
                    e.target.value = "";
                  }}
                />
                <button
                  className="icon-btn"
                  title="Прикрепить скриншот"
                  disabled={uploading}
                  onClick={() => fileRef.current?.click()}
                >
                  {uploading ? <span className="spin">⟳</span> : "📎"}
                </button>
                <input
                  className="input"
                  style={{ flex: 1 }}
                  placeholder={t.reply + "…"}
                  value={reply}
                  onChange={(e) => setReply(e.target.value)}
                  onPaste={(e) => {
                    const f = Array.from(e.clipboardData.files).find((x) => x.type.startsWith("image/"));
                    if (f) {
                      e.preventDefault();
                      void uploadShot(f);
                    }
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (reply.trim() || attach)) sendReply.mutate();
                  }}
                />
                <button
                  className="btn primary"
                  disabled={(!reply.trim() && !attach) || sendReply.isPending || uploading}
                  onClick={() => sendReply.mutate()}
                >
                  {t.reply}
                </button>
              </div>
            </>
          ) : (
            <span className="dim">← {t.tickets}</span>
          )}
        </div>
      </div>

      {offer && (
        <Modal title={t.premiumOfferTitle} onClose={() => setOffer(null)}>
          <div className="grid" style={{ gap: 12 }}>
            <Field label={t.premiumOfferName}>
              <input
                className="input"
                placeholder={t.premiumOfferNamePh}
                value={offer.name}
                onChange={(e) => setOffer({ ...offer, name: e.target.value })}
              />
            </Field>
            <Field label={t.premiumOfferDesc}>
              <input
                className="input"
                value={offer.description}
                onChange={(e) => setOffer({ ...offer, description: e.target.value })}
              />
            </Field>
            <div className="row" style={{ gap: 10 }}>
              <Field label="Трафик ГБ (0=∞)">
                <input
                  className="input num"
                  type="number"
                  value={offer.traffic_limit_gb}
                  onChange={(e) =>
                    setOffer({ ...offer, traffic_limit_gb: Number(e.target.value) || 0 })
                  }
                />
              </Field>
              <Field label={t.devices}>
                <input
                  className="input num"
                  type="number"
                  value={offer.device_limit}
                  onChange={(e) => setOffer({ ...offer, device_limit: Number(e.target.value) || 0 })}
                />
              </Field>
            </div>
            <div className="grid" style={{ gap: 4 }}>
              <span className="caps">{t.premiumOfferSquads}</span>
              <span className="dim" style={{ fontSize: 11.5 }}>
                {t.premiumOfferSquadsHint}
              </span>
              {(servers.data?.squads ?? []).map((sq) => {
                const on = offer.internal_squads.includes(sq.uuid);
                return (
                  <label key={sq.uuid} className="row" style={{ gap: 8, fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        setOffer({
                          ...offer,
                          internal_squads: on
                            ? offer.internal_squads.filter((u) => u !== sq.uuid)
                            : [...offer.internal_squads, sq.uuid],
                        })
                      }
                    />
                    {sq.name}
                  </label>
                );
              })}
            </div>
            <span className="caps">{t.periods}</span>
            {offer.durations.map((dur, i) => (
              <div key={i} className="row">
                <input
                  className="input num"
                  style={{ width: 90 }}
                  type="number"
                  value={dur.days}
                  onChange={(e) =>
                    setOffer({
                      ...offer,
                      durations: offer.durations.map((x, j) =>
                        j === i ? { ...x, days: Number(e.target.value) || 1 } : x,
                      ),
                    })
                  }
                />
                <span className="dim">{t.days}</span>
                <input
                  className="input num"
                  style={{ width: 110 }}
                  type="number"
                  value={dur.price_minor / 100}
                  onChange={(e) =>
                    setOffer({
                      ...offer,
                      durations: offer.durations.map((x, j) =>
                        j === i ? { ...x, price_minor: Math.round(Number(e.target.value) * 100) } : x,
                      ),
                    })
                  }
                />
                <span className="dim">₽</span>
                <button
                  className="btn danger sm"
                  onClick={() =>
                    setOffer({ ...offer, durations: offer.durations.filter((_, j) => j !== i) })
                  }
                >
                  ✕
                </button>
              </div>
            ))}
            <button
              className="btn secondary sm"
              onClick={() =>
                setOffer({ ...offer, durations: [...offer.durations, nextDuration(offer.durations)] })
              }
            >
              + {t.periods}
            </button>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setOffer(null)}>
                {t.cancel}
              </button>
              <button className="btn primary" disabled={sendingOffer} onClick={() => void sendOffer()}>
                {t.premiumOfferSend}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
