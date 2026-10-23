/* Screen — Ключи ИИ-агентов (owner-only): read-only admin API keys (cwk_...) for Claude and
   other AI agents. See src/web/routes/admin/api_keys.py. Reached from the Settings tile.
   The key is shown once, right after creation; the server keeps only its hash. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, dt, dtTime } from "../api/client";
import { Field, Modal, Seg } from "../components/ui";
import { useApp } from "../state/app";

type KeyRow = {
  id: number;
  name: string;
  prefix: string;
  status: "active" | "revoked" | "expired";
  allowed_screens: string[] | null;
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};
type ListResp = { items: KeyRow[]; screens: { key: string; label: string }[] };
type CreateResp = { key: string; item: KeyRow };
type LogEntry = { at: string; method: string; path: string; status: number };

type Days = "30" | "90" | "365";

export default function AiKeys() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();

  const data = useQuery({
    queryKey: ["ai-keys"],
    queryFn: () => api.get<ListResp>("/api/admin/admin-keys"),
  });
  const screens = data.data?.screens ?? [];
  const labelOf = (key: string) => screens.find((s) => s.key === key)?.label ?? key;

  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [days, setDays] = useState<Days>("90");
  const [allScreens, setAllScreens] = useState(true);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);

  const [logKey, setLogKey] = useState<KeyRow | null>(null);
  const log = useQuery({
    queryKey: ["ai-keys", "log", logKey?.id],
    queryFn: () => api.get<{ items: LogEntry[] }>(`/api/admin/admin-keys/${logKey!.id}/log`),
    enabled: logKey !== null,
  });

  function resetCreate() {
    setName("");
    setDays("90");
    setAllScreens(true);
    setPicked({});
  }

  async function create() {
    if (!name.trim()) {
      toast(t.aiKeysNameRequired);
      return;
    }
    const allowed_screens = allScreens
      ? null
      : Object.entries(picked).filter(([, v]) => v).map(([k]) => k);
    if (allowed_screens !== null && allowed_screens.length === 0) {
      toast(t.aiKeysPickOne);
      return;
    }
    setCreating(true);
    try {
      const res = await api.post<CreateResp>("/api/admin/admin-keys", {
        name: name.trim(),
        days: Number(days),
        allowed_screens,
      });
      setCreateOpen(false);
      resetCreate();
      setIssued(res.key);
      void qc.invalidateQueries({ queryKey: ["ai-keys"] });
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setCreating(false);
    }
  }

  async function revoke(k: KeyRow) {
    if (!(await confirm(t.aiKeysRevokeConfirm))) return;
    try {
      await api.post(`/api/admin/admin-keys/${k.id}/revoke`);
      void qc.invalidateQueries({ queryKey: ["ai-keys"] });
      toast("✕ " + k.name);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast(t.copied);
    } catch {
      toast("Не удалось скопировать — выделите ключ вручную");
    }
  }

  function statusPill(k: KeyRow) {
    if (k.status === "revoked") return <span className="st off">{t.aiKeysRevoked}</span>;
    if (k.status === "expired") return <span className="st mid">{t.aiKeysExpired}</span>;
    return <span className="st on">{t.aiKeysActive}</span>;
  }

  const cols = "1.2fr 0.9fr 0.8fr 0.8fr 0.9fr 0.8fr 1.4fr auto";

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.aiKeys}</h1>
        <div className="actions">
          <button className="btn primary" onClick={() => setCreateOpen(true)}>
            {t.aiKeysCreate}
          </button>
        </div>
      </div>

      <div className="muted" style={{ fontSize: 13, marginBottom: 16, maxWidth: 820 }}>
        {t.aiKeysIntro}
      </div>

      <div className="tbl">
        <div className="tr head" style={{ gridTemplateColumns: cols }}>
          <span>{t.aiKeysName}</span>
          <span>{t.aiKeysPrefix}</span>
          <span>{t.aiKeysCreated}</span>
          <span>{t.aiKeysExpires}</span>
          <span>{t.aiKeysLastUsed}</span>
          <span>{t.colStatus}</span>
          <span>{t.aiKeysScreens}</span>
          <span />
        </div>
        {(data.data?.items ?? []).map((k) => (
          <div key={k.id} className="tr" style={{ gridTemplateColumns: cols }}>
            <span>{k.name}</span>
            <span className="mono">{k.prefix}…</span>
            <span>{dt(k.created_at)}</span>
            <span>{dt(k.expires_at)}</span>
            <span>{dtTime(k.last_used_at)}</span>
            <span>{statusPill(k)}</span>
            <span style={{ fontSize: 12.5 }}>
              {k.allowed_screens === null ? (
                <span className="dim">{t.aiKeysAllScreens}</span>
              ) : (
                k.allowed_screens.map(labelOf).join(", ")
              )}
            </span>
            <span className="row" style={{ gap: 4 }}>
              <button className="btn secondary sm" onClick={() => setLogKey(k)}>
                {t.aiKeysJournal}
              </button>
              {k.status !== "revoked" && (
                <button className="btn danger sm" onClick={() => void revoke(k)}>
                  {t.aiKeysRevoke}
                </button>
              )}
            </span>
          </div>
        ))}
        {data.data && data.data.items.length === 0 && (
          <div className="tr dim">{t.aiKeysEmpty}</div>
        )}
      </div>

      {createOpen && (
        <Modal title={t.aiKeysCreate} onClose={() => setCreateOpen(false)}>
          <div className="grid" style={{ gap: 12 }}>
            <Field label={t.aiKeysName}>
              <input
                className="input"
                maxLength={48}
                placeholder={t.aiKeysNamePh}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            {/* Not a Field: its <label> would forward every click to the first segment. */}
            <div className="grid" style={{ gap: 6 }}>
              <span className="caps">{t.aiKeysTerm}</span>
              <Seg<Days>
                value={days}
                onChange={setDays}
                options={[
                  { id: "30", label: t.aiKeysDays30 },
                  { id: "90", label: t.aiKeysDays90 },
                  { id: "365", label: t.aiKeysDays365 },
                ]}
              />
            </div>
            <div className="grid" style={{ gap: 6 }}>
              <span className="caps">{t.aiKeysScreens}</span>
              <label className="row" style={{ gap: 8, fontSize: 13 }}>
                <input type="radio" checked={allScreens} onChange={() => setAllScreens(true)} />
                {t.aiKeysAllScreens}
              </label>
              <label className="row" style={{ gap: 8, fontSize: 13 }}>
                <input type="radio" checked={!allScreens} onChange={() => setAllScreens(false)} />
                {t.aiKeysPickScreens}
              </label>
              {!allScreens && (
                <div
                  className="grid"
                  style={{ gap: 4, gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))" }}
                >
                  {screens.map((s) => (
                    <label key={s.key} className="row" style={{ gap: 8, fontSize: 13 }}>
                      <input
                        type="checkbox"
                        checked={picked[s.key] ?? false}
                        onChange={(e) => setPicked((p) => ({ ...p, [s.key]: e.target.checked }))}
                      />
                      {s.label}
                    </label>
                  ))}
                </div>
              )}
            </div>
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

      {issued !== null && (
        <Modal title={t.aiKeysShownOnce} onClose={() => setIssued(null)}>
          <div className="grid" style={{ gap: 12 }}>
            <div style={{ fontSize: 13, color: "var(--warn)" }}>{t.aiKeysShownOnceWarn}</div>
            <div className="row">
              <input
                className="input mono"
                readOnly
                style={{ flex: 1, fontSize: 12 }}
                value={issued}
                onFocus={(e) => e.target.select()}
              />
              <button className="btn primary sm" onClick={() => void copy(issued)}>
                {t.aiKeysCopy}
              </button>
            </div>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setIssued(null)}>
                {t.aiKeysDone}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {logKey !== null && (
        <Modal title={`${t.aiKeysJournalTitle}: ${logKey.name}`} onClose={() => setLogKey(null)} wide>
          <div className="tbl" style={{ maxHeight: "60vh", overflow: "auto" }}>
            {(log.data?.items ?? []).map((e, i) => (
              <div
                key={i}
                className="tr"
                style={{ gridTemplateColumns: "0.9fr 0.5fr 3fr 0.5fr", fontSize: 12.5 }}
              >
                <span>{dtTime(e.at)}</span>
                <span className="mono">{e.method}</span>
                <span className="mono" style={{ wordBreak: "break-all" }}>
                  {e.path}
                </span>
                <span className={`st ${e.status < 400 ? "on" : "off"}`}>{e.status}</span>
              </div>
            ))}
            {log.data && log.data.items.length === 0 && (
              <div className="tr dim">{t.aiKeysJournalEmpty}</div>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}
