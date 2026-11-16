/* Screen 16. Уведомления: editable lifecycle message templates (purchase, renewal, refund).
   Tab «Письма» — the parallel e-mail set for people without Telegram (subject + text). */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { Seg, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Note = {
  event: string;
  title: string;
  text: string;
  enabled: boolean;
  placeholders: string[];
};

type Draft = { text: string; enabled: boolean };

type Letter = {
  event: string;
  title: string;
  subject: string;
  text: string;
  enabled: boolean;
  placeholders: string[];
  is_default: boolean;
};

type LetterDraft = { subject: string; text: string; enabled: boolean };

export default function Notifications() {
  const { t } = useApp();
  const [tab, setTab] = useState<"tg" | "email">("tg");
  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="h1">{t.notifications}</h1>
          <div className="caps sub">{tab === "tg" ? t.notificationsSub : t.emailNotificationsSub}</div>
        </div>
      </div>
      <div style={{ marginBottom: 12 }}>
        <Seg
          value={tab}
          options={[
            { id: "tg" as const, label: "Telegram" },
            { id: "email" as const, label: t.emailNotifications },
          ]}
          onChange={setTab}
        />
      </div>
      {tab === "tg" ? <TelegramNotes /> : <EmailLetters />}
    </>
  );
}

function TelegramNotes() {
  const { t, toast } = useApp();
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api.get<{ items: Note[] }>("/api/admin/notifications"),
  });
  const [draft, setDraft] = useState<Record<string, Draft>>({});

  useEffect(() => {
    if (!list.data) return;
    const d: Record<string, Draft> = {};
    for (const n of list.data.items) d[n.event] = { text: n.text, enabled: n.enabled };
    setDraft(d);
  }, [list.data]);

  async function save(ev: string) {
    try {
      await api.patch(`/api/admin/notifications/${ev}`, draft[ev]);
      toast(`${t.saved} ✓`);
      void qc.invalidateQueries({ queryKey: ["notifications"] });
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <>
      <div className="grid" style={{ gap: 12 }}>
        {(list.data?.items ?? []).map((n) => {
          const d = draft[n.event] ?? { text: n.text, enabled: n.enabled };
          const dirty = d.text !== n.text || d.enabled !== n.enabled;
          return (
            <div key={n.event} className="card">
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                <b>{n.title}</b>
                <Toggle
                  on={d.enabled}
                  onChange={(v) => setDraft({ ...draft, [n.event]: { ...d, enabled: v } })}
                />
              </div>
              <textarea
                className="input"
                rows={3}
                value={d.text}
                onChange={(e) => setDraft({ ...draft, [n.event]: { ...d, text: e.target.value } })}
              />
              <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                {n.placeholders.map((p) => (
                  <button
                    key={p}
                    className="btn secondary sm mono"
                    onClick={() =>
                      setDraft({ ...draft, [n.event]: { ...d, text: `${d.text}{${p}}` } })
                    }
                  >
                    {`{${p}}`}
                  </button>
                ))}
                {dirty && (
                  <button
                    className="btn primary sm"
                    style={{ marginLeft: "auto" }}
                    onClick={() => save(n.event)}
                  >
                    {t.save}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

function EmailLetters() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["notifications-email"],
    queryFn: () => api.get<{ items: Letter[] }>("/api/admin/notifications/email"),
  });
  const [draft, setDraft] = useState<Record<string, LetterDraft>>({});

  useEffect(() => {
    if (!list.data) return;
    const d: Record<string, LetterDraft> = {};
    for (const n of list.data.items) d[n.event] = { subject: n.subject, text: n.text, enabled: n.enabled };
    setDraft(d);
  }, [list.data]);

  const refresh = () => void qc.invalidateQueries({ queryKey: ["notifications-email"] });

  async function save(ev: string) {
    try {
      await api.patch(`/api/admin/notifications/email/${ev}`, draft[ev]);
      toast(`${t.saved} ✓`);
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function reset(ev: string) {
    if (!(await confirm(t.emailResetConfirm))) return;
    try {
      await api.post(`/api/admin/notifications/email/${ev}/reset`);
      toast(`${t.saved} ✓`);
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <div className="grid" style={{ gap: 12 }}>
      <div className="card muted" style={{ fontSize: 13 }}>
        {t.emailNotificationsHint}
      </div>
      {(list.data?.items ?? []).map((n) => {
        const d = draft[n.event] ?? { subject: n.subject, text: n.text, enabled: n.enabled };
        const dirty = d.subject !== n.subject || d.text !== n.text || d.enabled !== n.enabled;
        const set = (patch: Partial<LetterDraft>) =>
          setDraft({ ...draft, [n.event]: { ...d, ...patch } });
        return (
          <div key={n.event} className="card">
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <b>{n.title}</b>
              <Toggle on={d.enabled} onChange={(v) => set({ enabled: v })} />
            </div>
            <label className="caps">{t.emailSubject}</label>
            <input
              className="input"
              style={{ marginBottom: 8 }}
              maxLength={255}
              value={d.subject}
              onChange={(e) => set({ subject: e.target.value })}
            />
            <label className="caps">{t.emailBody}</label>
            <textarea
              className="input"
              rows={10}
              value={d.text}
              onChange={(e) => set({ text: e.target.value })}
            />
            <div className="row" style={{ flexWrap: "wrap", gap: 6, marginTop: 8 }}>
              {n.placeholders.map((p) => (
                <button
                  key={p}
                  className="btn secondary sm mono"
                  onClick={() => set({ text: `${d.text}{${p}}` })}
                >
                  {`{${p}}`}
                </button>
              ))}
              <div className="row" style={{ gap: 6, marginLeft: "auto" }}>
                {!n.is_default && (
                  <button className="btn secondary sm" onClick={() => reset(n.event)}>
                    {t.emailResetDefault}
                  </button>
                )}
                {dirty && (
                  <button className="btn primary sm" onClick={() => save(n.event)}>
                    {t.save}
                  </button>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
