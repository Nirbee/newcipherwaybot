/* Screen — Роутеры: control-plane fleet (RouterDevice). List + create + host assignment +
   eligible-hosts allowlist + token rotate/revoke/delete. See src/web/routes/admin/routers.py. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useEffect, useState } from "react";

import { api, dtTime } from "../api/client";
import { Drawer, Field, Modal, Seg, SecretInput } from "../components/ui";
import { useApp } from "../state/app";

type Mode = "auto" | "force";
type Status = "pending" | "online" | "offline" | "revoked";

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
  note: string | null;
  created_at: string;
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
};

function QrImage({ text }: { text: string }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    QRCode.toDataURL(text, { margin: 1, width: 240 })
      .then(setSrc)
      .catch(() => setSrc(""));
  }, [text]);
  if (!src) return null;
  return (
    <img
      src={src}
      width={240}
      height={240}
      alt="QR"
      style={{ background: "#fff", padding: 8, borderRadius: 8, alignSelf: "center" }}
    />
  );
}

function installCommand(token: string): string {
  const base = window.location.origin;
  return (
    `opkg update && opkg install curl && curl -fsSL ${base}/api/agent/install.sh ` +
    `-o /tmp/cw-install.sh && sh /tmp/cw-install.sh ${token} ${base}`
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
    warning: string | null;
    claimUrl: string | null;
  } | null>(null);
  const [hostsOpen, setHostsOpen] = useState(false);
  const [hostsChecked, setHostsChecked] = useState<Record<string, boolean>>({});

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
      const r = await api.post<{ token: string }>(`/api/admin/routers/${d.id}/rotate`);
      setTokenModal({ label: d.label, token: r.token, warning: null, claimUrl: null });
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
            <span className="mono muted">
              {d.awaiting_claim ? t.routersAwaitingQr : (d.subscription_label ?? `#${d.subscription_id}`)}
            </span>
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
                  <div className="dim" style={{ fontSize: 12 }}>
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
              <div className="grid" style={{ gap: 6 }}>
                <textarea
                  className="input mono"
                  readOnly
                  rows={4}
                  style={{ fontSize: 11.5, resize: "none" }}
                  value={installCommand(tokenModal.token)}
                  onFocus={(e) => e.currentTarget.select()}
                />
                <div className="row" style={{ justifyContent: "flex-end" }}>
                  <button
                    className="btn secondary sm"
                    onClick={() => void copy(installCommand(tokenModal.token))}
                  >
                    {t.copy}
                  </button>
                </div>
              </div>
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
              <button className="btn primary" onClick={() => setTokenModal(null)}>
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

      {openId !== null && (
        <Drawer onClose={() => setOpenId(null)}>
          {detail.data && (
            <div className="grid" style={{ gap: 14 }}>
              <div className="h1" style={{ fontSize: 18 }}>
                {detail.data.label}
              </div>
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
              {detail.data.claim_url && (
                <Field label={t.routersClaimTitle}>
                  <div className="grid" style={{ gap: 8 }}>
                    <QrImage text={detail.data.claim_url} />
                    <div className="dim" style={{ fontSize: 12 }}>
                      {t.routersClaimHint}
                    </div>
                  </div>
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
              <Field label={t.routersDiagnostics}>
                {detail.data.diagnostics ? (
                  <div className="grid" style={{ gap: 6, fontSize: 13 }}>
                    {(() => {
                      const d = detail.data.diagnostics;
                      const dnsMismatch =
                        d.dns_router && d.dns_1111 && d.dns_router !== d.dns_1111
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
