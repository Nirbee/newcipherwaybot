/* Screen 11 — Тикеты: support channels config + ticket list + chat. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, dtTime } from "../api/client";
import { Field, Modal, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type TicketRow = {
  id: number;
  username: string | null;
  subject: string;
  status: "open" | "waiting" | "closed";
  is_premium: boolean;
  messages: number;
  updated_at: string | null;
};
type TicketDetail = {
  id: number;
  subject: string;
  status: string;
  is_premium: boolean;
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
  const [selId, setSelId] = useState<number | null>(null);
  const [reply, setReply] = useState("");
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
    mutationFn: () => api.post(`/api/admin/tickets/${selId}/reply`, { text: reply }),
    onSuccess: () => {
      setReply("");
      void qc.invalidateQueries({ queryKey: ["ticket", selId] });
      void qc.invalidateQueries({ queryKey: ["tickets"] });
      toast("✓");
    },
    onError: (e) => toast(e.message),
  });

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
                    {tk.messages} · {dtTime(tk.updated_at)}
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
              <div className="grid" style={{ gap: 8, flex: 1, overflowY: "auto", marginBottom: 12 }}>
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
              <div className="row">
                <input
                  className="input"
                  style={{ flex: 1 }}
                  placeholder={t.reply + "…"}
                  value={reply}
                  onChange={(e) => setReply(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && reply.trim()) sendReply.mutate();
                  }}
                />
                <button
                  className="btn primary"
                  disabled={!reply.trim() || sendReply.isPending}
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
