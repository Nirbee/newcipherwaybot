/* Screen — Роутеры: control-plane fleet (RouterDevice). List + create + host assignment +
   eligible-hosts allowlist + token rotate/revoke/delete. See src/web/routes/admin/routers.py. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, dtTime } from "../api/client";
import { Drawer, Field, Modal, SecretInput } from "../components/ui";
import { useApp } from "../state/app";

type Mode = "auto" | "force";
type Status = "pending" | "online" | "offline" | "revoked";

type RouterDevice = {
  id: number;
  label: string;
  subscription_id: number;
  subscription_label: string | null;
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
  note: string | null;
  created_at: string;
};
type AvailableHost = { uuid: string; remark: string; network: string; is_disabled: boolean };
type RouterDetail = RouterDevice & {
  install_report: Record<string, unknown> | null;
  available_hosts: AvailableHost[];
};
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
  primary_host_uuid: string | null;
  backup_host_uuid: string | null;
  warning: string | null;
};

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
  const [subId, setSubId] = useState("");
  const [label, setLabel] = useState("");
  const [mode, setMode] = useState<Mode>("auto");
  const [primaryHost, setPrimaryHost] = useState("");
  const [note, setNote] = useState("");

  const [tokenModal, setTokenModal] = useState<{ label: string; token: string; warning: string | null } | null>(null);
  const [hostsOpen, setHostsOpen] = useState(false);
  const [hostsChecked, setHostsChecked] = useState<Record<string, boolean>>({});

  const [openId, setOpenId] = useState<number | null>(null);
  const detail = useQuery({
    queryKey: ["routers", openId],
    queryFn: () => api.get<RouterDetail>(`/api/admin/routers/${openId}`),
    enabled: openId !== null,
  });

  function resetCreate() {
    setSubId("");
    setLabel("");
    setMode("auto");
    setPrimaryHost("");
    setNote("");
  }

  async function create() {
    const sid = Number(subId);
    if (!sid || !label.trim()) {
      toast(t.routersFillRequired);
      return;
    }
    if (mode === "force" && !primaryHost) {
      toast(t.routersForceNeedsHost);
      return;
    }
    try {
      const r = await api.post<CreateResp>("/api/admin/routers", {
        subscription_id: sid,
        label: label.trim(),
        mode,
        primary_host_uuid: mode === "force" ? primaryHost : undefined,
        note: note.trim() || undefined,
      });
      setCreateOpen(false);
      resetCreate();
      void qc.invalidateQueries({ queryKey: ["routers"] });
      setTokenModal({ label, token: r.token, warning: r.warning });
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function rotate(d: RouterDevice) {
    if (!(await confirm(t.routersRotateConfirm))) return;
    try {
      const r = await api.post<{ token: string }>(`/api/admin/routers/${d.id}/rotate`);
      setTokenModal({ label: d.label, token: r.token, warning: null });
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

  async function copy(text: string) {
    await navigator.clipboard.writeText(text);
    toast(t.copied);
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
          <button className="btn primary" onClick={() => setCreateOpen(true)}>
            {t.routersCreate}
          </button>
        </div>
      </div>

      <div className="tbl">
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
            <span>
              <b style={{ fontWeight: 500 }}>{d.label}</b>
              {d.note && (
                <div className="dim" style={{ fontSize: 11.5 }}>
                  {d.note}
                </div>
              )}
            </span>
            <span className="mono muted">{d.subscription_label ?? `#${d.subscription_id}`}</span>
            <span className={`st ${d.is_online ? "on" : d.status === "pending" ? "mid" : "off"}`}>
              {d.is_online && <span className="status-dot" />}
              {d.status === "pending"
                ? t.routersStPending
                : d.status === "revoked"
                  ? t.routersStRevoked
                  : d.is_online
                    ? t.routersStOnline
                    : t.routersStOffline}
            </span>
            <span className="cap-pill">{d.mode === "auto" ? t.routersModeAuto : t.routersModeForce}</span>
            <span className="mono" style={{ fontSize: 11.5 }}>
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
            <span className="mono muted">{d.last_seen_at ? dtTime(d.last_seen_at) : "—"}</span>
            <span className="row" style={{ gap: 4 }} onClick={(e) => e.stopPropagation()}>
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
            <Field label={t.routersSubId}>
              <input
                className="input mono"
                type="number"
                value={subId}
                onChange={(e) => setSubId(e.target.value)}
              />
              <span className="dim" style={{ fontSize: 11 }}>
                {t.routersSubIdHint}
              </span>
            </Field>
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
              <button className="btn primary" onClick={() => void create()}>
                {t.create}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {tokenModal && (
        <Modal title={t.routersTokenTitle} onClose={() => setTokenModal(null)}>
          <div className="grid" style={{ gap: 12 }}>
            <div className="dim" style={{ fontSize: 12.5 }}>
              {t.routersTokenHint}
            </div>
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
              <button className="btn primary" onClick={() => setTokenModal(null)}>
                {t.close}
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

      {openId !== null && (
        <Drawer onClose={() => setOpenId(null)}>
          {detail.data && (
            <div className="grid" style={{ gap: 14 }}>
              <div className="h1" style={{ fontSize: 18 }}>
                {detail.data.label}
              </div>
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
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted">{t.routersActiveOutbound}</span>
                  <span className="mono">{detail.data.active_outbound ?? "—"}</span>
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
