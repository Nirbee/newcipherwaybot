/* Screen — Администраторы (owner-only): staff accounts scoped to specific admin screens.
   See src/web/routes/admin/staff.py. Reached from Settings' tile or the sidebar (OWNER-only). */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";
import { Field, Modal } from "../components/ui";
import type { Dict } from "../i18n";
import { useApp } from "../state/app";

type AdminRow = {
  id: number;
  username: string;
  role: string;
  status: string;
  allowed_screens: string[] | null;
  created_at: string;
};
type ListResp = { items: AdminRow[]; scopable_screens: string[] };

// Path segment -> the i18n key that already labels it in the sidebar (names don't always match
// the segment string, e.g. "stats" screen's label key is statsTitle, "sales" is salesTitle).
const LABEL_KEY: Record<string, keyof Dict> = {
  routers: "routers", users: "users", servers: "servers", settings: "settings",
  stats: "statsTitle", broadcasts: "broadcasts", notifications: "notifications",
  reminders: "reminders", campaigns: "campaigns", partners: "partners", sales: "salesTitle",
  "ai-support": "aiSupport", blacklist: "blacklist", miniapp: "miniapp",
};

function screenLabel(t: Dict, key: string): string {
  const k = LABEL_KEY[key];
  return k ? String(t[k]) : key;
}

export default function Admins() {
  const { t, toast, confirm } = useApp();
  const qc = useQueryClient();

  const data = useQuery({
    queryKey: ["admins"],
    queryFn: () => api.get<ListResp>("/api/admin/admins"),
  });

  const [createOpen, setCreateOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fullAccess, setFullAccess] = useState(false);
  const [screens, setScreens] = useState<Record<string, boolean>>({});

  const [editId, setEditId] = useState<number | null>(null);
  const [editScreens, setEditScreens] = useState<Record<string, boolean>>({});
  const [editFullAccess, setEditFullAccess] = useState(false);

  const scopable = data.data?.scopable_screens ?? [];

  function resetCreate() {
    setUsername("");
    setPassword("");
    setFullAccess(false);
    setScreens({});
  }

  async function create() {
    if (!username.trim() || password.length < 8) {
      toast(t.adminsFillRequired);
      return;
    }
    const allowed_screens = fullAccess
      ? null
      : Object.entries(screens).filter(([, v]) => v).map(([k]) => k);
    try {
      await api.post("/api/admin/admins", {
        username: username.trim(),
        password,
        allowed_screens,
      });
      setCreateOpen(false);
      resetCreate();
      void qc.invalidateQueries({ queryKey: ["admins"] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  function openEdit(a: AdminRow) {
    setEditId(a.id);
    setEditFullAccess(a.allowed_screens === null);
    const checked: Record<string, boolean> = {};
    for (const s of a.allowed_screens ?? []) checked[s] = true;
    setEditScreens(checked);
  }

  async function saveEdit() {
    if (editId === null) return;
    const allowed_screens = editFullAccess
      ? null
      : Object.entries(editScreens).filter(([, v]) => v).map(([k]) => k);
    try {
      await api.patch(`/api/admin/admins/${editId}`, { allowed_screens });
      setEditId(null);
      void qc.invalidateQueries({ queryKey: ["admins"] });
      toast("✓");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function revoke(a: AdminRow) {
    if (!(await confirm(t.adminsRevokeConfirm))) return;
    try {
      await api.post(`/api/admin/admins/${a.id}/revoke`);
      void qc.invalidateQueries({ queryKey: ["admins"] });
      toast("✕ " + a.username);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const cols = "1.2fr 0.8fr 0.8fr 2fr auto";

  return (
    <>
      <div className="page-head">
        <h1 className="h1">{t.admins}</h1>
        <div className="actions">
          <button className="btn primary" onClick={() => setCreateOpen(true)}>
            {t.adminsCreate}
          </button>
        </div>
      </div>

      <div className="tbl">
        <div className="tr head" style={{ gridTemplateColumns: cols }}>
          <span>{t.adminsUsername}</span>
          <span>{t.adminsRole}</span>
          <span>{t.colStatus}</span>
          <span>{t.adminsAccess}</span>
          <span />
        </div>
        {(data.data?.items ?? []).map((a) => (
          <div key={a.id} className="tr" style={{ gridTemplateColumns: cols }}>
            <span className="mono">@{a.username}</span>
            <span className="cap-pill">{a.role}</span>
            <span className={`st ${a.status === "active" ? "on" : "off"}`}>{a.status}</span>
            <span style={{ fontSize: 12.5 }}>
              {a.allowed_screens === null ? (
                <span className="dim">{t.adminsFullAccess}</span>
              ) : a.allowed_screens.length === 0 ? (
                <span className="dim">{t.adminsNoScreens}</span>
              ) : (
                a.allowed_screens.map((s) => screenLabel(t, s)).join(", ")
              )}
            </span>
            {a.role === "ADMIN" ? (
              <span className="row" style={{ gap: 4 }}>
                <button className="btn secondary sm" onClick={() => openEdit(a)}>
                  {t.adminsEditAccess}
                </button>
                <button className="btn danger sm" onClick={() => void revoke(a)}>
                  {t.adminsRevoke}
                </button>
              </span>
            ) : (
              <span className="dim" style={{ fontSize: 11.5 }}>
                {t.adminsProtected}
              </span>
            )}
          </div>
        ))}
        {data.data && data.data.items.length === 0 && <div className="tr dim">—</div>}
      </div>

      {createOpen && (
        <Modal title={t.adminsCreate} onClose={() => setCreateOpen(false)}>
          <div className="grid" style={{ gap: 12 }}>
            <Field label={t.adminsUsername}>
              <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} />
            </Field>
            <Field label={t.adminsPassword}>
              <input
                className="input mono"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={fullAccess} onChange={(e) => setFullAccess(e.target.checked)} />
              {t.adminsFullAccess}
            </label>
            {!fullAccess && (
              <div className="grid" style={{ gap: 4 }}>
                <span className="caps">{t.adminsAccess}</span>
                {scopable.map((s) => (
                  <label key={s} className="row" style={{ gap: 8, fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={screens[s] ?? false}
                      onChange={(e) => setScreens((sc) => ({ ...sc, [s]: e.target.checked }))}
                    />
                    {screenLabel(t, s)}
                  </label>
                ))}
              </div>
            )}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setCreateOpen(false)}>
                {t.cancel}
              </button>
              <button className="btn primary" onClick={() => void create()}>
                {t.create}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {editId !== null && (
        <Modal title={t.adminsEditAccess} onClose={() => setEditId(null)}>
          <div className="grid" style={{ gap: 12 }}>
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              <input
                type="checkbox"
                checked={editFullAccess}
                onChange={(e) => setEditFullAccess(e.target.checked)}
              />
              {t.adminsFullAccess}
            </label>
            {!editFullAccess && (
              <div className="grid" style={{ gap: 4 }}>
                {scopable.map((s) => (
                  <label key={s} className="row" style={{ gap: 8, fontSize: 13 }}>
                    <input
                      type="checkbox"
                      checked={editScreens[s] ?? false}
                      onChange={(e) => setEditScreens((sc) => ({ ...sc, [s]: e.target.checked }))}
                    />
                    {screenLabel(t, s)}
                  </label>
                ))}
              </div>
            )}
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setEditId(null)}>
                {t.cancel}
              </button>
              <button className="btn primary" onClick={() => void saveEdit()}>
                {t.save}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}
