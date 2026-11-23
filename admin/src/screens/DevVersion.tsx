/* «📊 DevVersion» — diagnostics the Windows app sends after the user opted in: the kill switch,
   upload interval and feature flags; summaries (provider x node/protocol, direct-connection
   failures, game tests); the report list with the full JSON of one report; export; deletion
   of a subscription's reports. See src/web/routes/admin/devversion.py. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, bytesFmt, dtTime, getToken } from "../api/client";
import { Modal, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Settings = {
  enabled: boolean;
  upload_interval_s: number;
  flags: Record<string, unknown>;
  allowed_subscriptions: number[];
  can_edit: boolean;
  can_see_raw: boolean;
  can_export: boolean;
  consent_version: string;
  max_report_bytes: number;
  retention_days: number;
  geoip: { asn: string | null; city: string | null };
};
type Facets = {
  providers: { asn: number | null; as_org: string | null; reports: number }[];
  app_versions: { app_version: string; reports: number }[];
};
type Report = {
  id: number;
  subscription_id: number;
  subscription_label: string | null;
  install_id: string;
  received_at: string | null;
  app_version: string;
  consent_version: string;
  asn: number | null;
  as_org: string | null;
  country: string | null;
  region: string | null;
  city: string | null;
  mode: string | null;
  size: number | null;
  counts: Record<string, number>;
  payload?: unknown;
};
type Stats = {
  asn: number | null;
  as_org: string | null;
  subscriptions: number;
  reports: number;
  samples: number;
  failures: number;
  latency_median: number | null;
  loss_median: number | null;
  single_source: boolean;
};
type Summary = {
  reports: number;
  truncated: boolean;
  links: (Stats & { node: string | null; protocol: string | null })[];
  direct_failures: {
    kind: string;
    name: string;
    subscriptions: number;
    providers: number;
    reports: number;
    failures: number;
    single_source: boolean;
  }[];
  games: (Stats & { game: string | null; route: string | null; jitter_median: number | null })[];
};
type Filters = { asn: string; app_version: string; subscription_id: string; from: string; to: string };

const PAGE = 50;
const EMPTY: Filters = { asn: "", app_version: "", subscription_id: "", from: "", to: "" };

function query(f: Filters): string {
  const q = new URLSearchParams();
  if (f.asn) q.set("asn", f.asn);
  if (f.app_version) q.set("app_version", f.app_version);
  if (/^\d+$/.test(f.subscription_id.trim())) q.set("subscription_id", f.subscription_id.trim());
  if (f.from) q.set("date_from", f.from);
  if (f.to) q.set("date_to", f.to);
  return q.toString();
}

function provider(asn: number | null, org: string | null): string {
  if (asn === null) return "не определён";
  return org ? `AS${asn} · ${org}` : `AS${asn}`;
}

function num(v: number | null, unit = ""): string {
  return v === null ? "—" : `${v}${unit}`;
}

function place(r: Report): string {
  return [r.country, r.region, r.city].filter(Boolean).join(", ") || "—";
}

function sub(r: { subscription_id: number; subscription_label: string | null }): string {
  return `#${r.subscription_id}${r.subscription_label ? ` · ${r.subscription_label}` : ""}`;
}

const HEAD = { background: "var(--panel2)" };
// a row only one subscription reported — possibly noise or a made-up value
const SINGLE_HINT = "Только одна подписка — может быть шумом";
function single(on: boolean): { opacity?: number } {
  return on ? { opacity: 0.5 } : {};
}
const LINK_COLS = "1.4fr 1fr 1fr 70px 70px 70px 90px 80px 70px";
const DIRECT_COLS = "110px 1fr 80px 100px 80px 80px";
const GAME_COLS = "1fr 1.4fr 80px 70px 70px 90px 80px 80px 70px";

export default function DevVersion() {
  const { toast, confirm } = useApp();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [filters, setFilters] = useState<Filters>(EMPTY);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<Report | null>(null);
  const qs = query(filters);

  const settings = useQuery({
    queryKey: ["devversion-settings"],
    queryFn: () => api.get<Settings>("/api/admin/devversion/settings"),
  });
  const facets = useQuery({
    queryKey: ["devversion-facets"],
    queryFn: () => api.get<Facets>("/api/admin/devversion/facets"),
  });
  // a single subscription's summary is OWNER / DEV only — others get the overall one
  const summaryQs = query(
    settings.data?.can_see_raw ? filters : { ...filters, subscription_id: "" },
  );
  const summary = useQuery({
    queryKey: ["devversion-summary", summaryQs],
    queryFn: () => api.get<Summary>(`/api/admin/devversion/summary?${summaryQs}`),
    enabled: settings.data !== undefined,
  });
  const reports = useQuery({
    queryKey: ["devversion-reports", qs, offset],
    queryFn: () =>
      api.get<{ items: Report[]; total: number }>(
        `/api/admin/devversion/reports?${qs}&limit=${PAGE}&offset=${offset}`,
      ),
  });

  const [enabled, setEnabled] = useState(true);
  const [interval, setIntervalS] = useState("3600");
  const [flags, setFlags] = useState("{}");
  const [allowed, setAllowed] = useState("");
  useEffect(() => {
    if (!settings.data) return;
    setEnabled(settings.data.enabled);
    setIntervalS(String(settings.data.upload_interval_s));
    setFlags(JSON.stringify(settings.data.flags, null, 2));
    setAllowed(settings.data.allowed_subscriptions.join(", "));
  }, [settings.data]);

  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["devversion-facets"] });
    void qc.invalidateQueries({ queryKey: ["devversion-summary"] });
    void qc.invalidateQueries({ queryKey: ["devversion-reports"] });
  };

  async function saveSettings() {
    try {
      await api.put("/api/admin/devversion/settings", {
        enabled,
        upload_interval_s: Number(interval),
        flags,
        allowed_subscriptions: allowed,
      });
      void qc.invalidateQueries({ queryKey: ["devversion-settings"] });
      toast("Сохранено — приложения получат настройки при следующем запросе");
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function showReport(r: Report) {
    try {
      setOpen(await api.get<Report>(`/api/admin/devversion/reports/${r.id}`));
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function exportJson() {
    try {
      const res = await fetch(`/api/admin/devversion/export?${qs}`, {
        headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      });
      if (!res.ok) throw new Error(`Экспорт не удался (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `devversion-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      // revoking right after click() can cancel the download in some browsers
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function deleteFor(subId: number) {
    if (!(await confirm(`Удалить все отчёты DevVersion подписки #${subId}? Это нельзя отменить.`)))
      return;
    try {
      const res = await api.del<{ deleted: number }>(
        `/api/admin/devversion/subscriptions/${subId}/reports`,
      );
      toast(`Удалено отчётов: ${res.deleted}`);
      setOpen(null);
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  function apply() {
    setOffset(0);
    setFilters(draft);
  }

  const s = settings.data;
  const canEdit = s?.can_edit ?? false;
  const sm = summary.data;
  const items = reports.data?.items ?? [];
  const total = reports.data?.total ?? 0;
  const subFilter = /^\d+$/.test(filters.subscription_id.trim())
    ? Number(filters.subscription_id.trim())
    : null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="h1">📊 DevVersion</h1>
          <div className="caps sub">
            Диагностика из приложения для Windows (включает сам пользователь, после согласия).
            Отчёты хранятся {s?.retention_days ?? 90} дней
          </div>
        </div>
        <div className="actions">
          {s?.can_export && (
            <button className="btn secondary" onClick={exportJson}>
              Экспорт JSON
            </button>
          )}
          {subFilter !== null && (
            <button className="btn danger" onClick={() => deleteFor(subFilter)}>
              Удалить отчёты подписки #{subFilter}
            </button>
          )}
        </div>
      </div>

      {/* settings */}
      <div className="tbl" style={{ marginBottom: 16 }}>
        <div className="tr" style={{ gridTemplateColumns: "1fr auto" }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 13.5, fontWeight: 500 }}>Приём отчётов</b>
            <div className="muted" style={{ fontSize: 12, marginTop: 1 }}>
              Выключено — приложения перестают собирать и отправлять данные
            </div>
          </div>
          <Toggle on={enabled} onChange={(v) => canEdit && setEnabled(v)} />
        </div>
        <div className="tr" style={{ gridTemplateColumns: "1fr auto" }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 13.5, fontWeight: 500 }}>Интервал отправки, секунд</b>
            <div className="muted" style={{ fontSize: 12, marginTop: 1 }}>
              От 600 до 86400. Сервер в любом случае принимает не больше 6 отчётов в час и 60 в
              сутки с одной подписки
            </div>
          </div>
          <input
            className="input num"
            style={{ width: 110 }}
            value={interval}
            disabled={!canEdit}
            onChange={(e) => setIntervalS(e.target.value)}
          />
        </div>
        <div className="tr" style={{ gridTemplateColumns: "1fr auto" }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 13.5, fontWeight: 500 }}>Разрешённые подписки</b>
            <div className="muted" style={{ fontSize: 12, marginTop: 1 }}>
              Номера через запятую. Пусто — принимаются все платные подписки (пробные — никогда)
            </div>
          </div>
          <input
            className="input mono"
            style={{ width: 260 }}
            placeholder="все платные"
            value={allowed}
            disabled={!canEdit}
            onChange={(e) => setAllowed(e.target.value)}
          />
        </div>
        <div className="tr" style={{ gridTemplateColumns: "1fr" }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 13.5, fontWeight: 500 }}>Флаги функций</b>
            <div className="muted" style={{ fontSize: 12, margin: "1px 0 6px" }}>
              Плоский JSON-объект: ключи a-z, 0-9, _ (до 32 символов), значения — true/false,
              число или строка до 64 символов, не больше 50 флагов. Пусто — {"{}"}
            </div>
            <textarea
              className="input mono"
              style={{ width: "100%", minHeight: 70, fontSize: 12 }}
              value={flags}
              disabled={!canEdit}
              onChange={(e) => setFlags(e.target.value)}
            />
          </div>
        </div>
        <div className="tr" style={{ gridTemplateColumns: "1fr auto" }}>
          <div className="muted" style={{ fontSize: 12 }}>
            Согласие v{s?.consent_version ?? "…"} · отчёт до{" "}
            {s ? bytesFmt(s.max_report_bytes) : "…"} · база провайдеров:{" "}
            {s?.geoip.asn ? `ASN от ${dtTime(s.geoip.asn)}` : "ASN не загружена"},{" "}
            {s?.geoip.city ? `регионы от ${dtTime(s.geoip.city)}` : "регионы не загружены"} ·{" "}
            <a href="https://db-ip.com" target="_blank" rel="noreferrer">
              IP Geolocation by DB-IP
            </a>
          </div>
          {canEdit ? (
            <button className="btn primary" onClick={saveSettings}>
              Сохранить
            </button>
          ) : (
            <span className="caps">меняет владелец или разработчик</span>
          )}
        </div>
      </div>

      {/* filters */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          <select
            className="input"
            style={{ minWidth: 220 }}
            value={draft.asn}
            onChange={(e) => setDraft({ ...draft, asn: e.target.value })}
          >
            <option value="">Все провайдеры</option>
            {(facets.data?.providers ?? []).map((p) => (
              <option key={p.asn ?? 0} value={String(p.asn ?? 0)}>
                {provider(p.asn, p.as_org)} ({p.reports})
              </option>
            ))}
          </select>
          <select
            className="input"
            value={draft.app_version}
            onChange={(e) => setDraft({ ...draft, app_version: e.target.value })}
          >
            <option value="">Все версии</option>
            {(facets.data?.app_versions ?? []).map((v) => (
              <option key={v.app_version} value={v.app_version}>
                {v.app_version} ({v.reports})
              </option>
            ))}
          </select>
          <input
            className="input num"
            style={{ width: 130 }}
            placeholder="Подписка №"
            value={draft.subscription_id}
            onChange={(e) => setDraft({ ...draft, subscription_id: e.target.value })}
          />
          <input
            className="input"
            type="date"
            value={draft.from}
            onChange={(e) => setDraft({ ...draft, from: e.target.value })}
          />
          <input
            className="input"
            type="date"
            value={draft.to}
            onChange={(e) => setDraft({ ...draft, to: e.target.value })}
          />
          <button className="btn primary" onClick={apply}>
            Показать
          </button>
          <button
            className="btn secondary"
            onClick={() => {
              setDraft(EMPTY);
              setOffset(0);
              setFilters(EMPTY);
            }}
          >
            Сбросить
          </button>
        </div>
      </div>

      {/* summaries */}
      <div className="caps" style={{ margin: "4px 0 8px" }}>
        Провайдер × сервер/протокол · отчётов: {sm?.reports ?? "…"} · бледные строки — данные
        одной подписки
        {sm?.truncated ? " (учтены не все отчёты — сузьте период)" : ""}
      </div>
      <div className="tbl" style={{ marginBottom: 16 }}>
        <div className="tr caps" style={{ ...HEAD, gridTemplateColumns: LINK_COLS }}>
          <span>Провайдер</span>
          <span>Сервер</span>
          <span>Протокол</span>
          <span>Подписок</span>
          <span>Отчётов</span>
          <span>Замеров</span>
          <span>Задержка</span>
          <span>Потери</span>
          <span>Отказы</span>
        </div>
        {(sm?.links ?? []).map((r, i) => (
          <div
            key={i}
            className="tr"
            style={{ gridTemplateColumns: LINK_COLS, ...single(r.single_source) }}
            title={r.single_source ? SINGLE_HINT : undefined}
          >
            <span>{provider(r.asn, r.as_org)}</span>
            <span>{r.node ?? "—"}</span>
            <span className="mono">{r.protocol ?? "—"}</span>
            <span className="num">{r.subscriptions}</span>
            <span className="num">{r.reports}</span>
            <span className="num">{r.samples}</span>
            <span className="num">{num(r.latency_median, " мс")}</span>
            <span className="num">{num(r.loss_median, "%")}</span>
            <span className="num">{r.failures}</span>
          </div>
        ))}
        {(sm?.links ?? []).length === 0 && <div className="tr dim">Нет данных</div>}
      </div>

      <div className="caps" style={{ margin: "4px 0 8px" }}>
        Ошибки прямых соединений (приложения и домены) — по числу подписок и провайдеров
      </div>
      <div className="tbl" style={{ marginBottom: 16 }}>
        <div className="tr caps" style={{ ...HEAD, gridTemplateColumns: DIRECT_COLS }}>
          <span>Тип</span>
          <span>Имя</span>
          <span>Подписок</span>
          <span>Провайдеров</span>
          <span>Отчётов</span>
          <span title="не больше 1000 с одной подписки">Ошибок</span>
        </div>
        {(sm?.direct_failures ?? []).map((r) => (
          <div
            key={`${r.kind}:${r.name}`}
            className="tr"
            style={{ gridTemplateColumns: DIRECT_COLS, ...single(r.single_source) }}
            title={r.single_source ? SINGLE_HINT : undefined}
          >
            <span className="dim">{r.kind === "app" ? "приложение" : "домен"}</span>
            <span className="mono" style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
              {r.name}
            </span>
            <span className="num">{r.subscriptions}</span>
            <span className="num">{r.providers}</span>
            <span className="num">{r.reports}</span>
            <span className="num dim">{r.failures}</span>
          </div>
        ))}
        {(sm?.direct_failures ?? []).length === 0 && <div className="tr dim">Нет данных</div>}
      </div>

      <div className="caps" style={{ margin: "4px 0 8px" }}>
        Игровые тесты
      </div>
      <div className="tbl" style={{ marginBottom: 16 }}>
        <div className="tr caps" style={{ ...HEAD, gridTemplateColumns: GAME_COLS }}>
          <span>Игра</span>
          <span>Провайдер</span>
          <span>Маршрут</span>
          <span>Подписок</span>
          <span>Замеров</span>
          <span>Задержка</span>
          <span>Потери</span>
          <span>Джиттер</span>
          <span>Отказы</span>
        </div>
        {(sm?.games ?? []).map((r, i) => (
          <div
            key={i}
            className="tr"
            style={{ gridTemplateColumns: GAME_COLS, ...single(r.single_source) }}
            title={r.single_source ? SINGLE_HINT : undefined}
          >
            <span>{r.game ?? "—"}</span>
            <span>{provider(r.asn, r.as_org)}</span>
            <span className="dim">
              {r.route === "direct" ? "напрямую" : r.route === "vpn" ? "через VPN" : "—"}
            </span>
            <span className="num">{r.subscriptions}</span>
            <span className="num">{r.samples}</span>
            <span className="num">{num(r.latency_median, " мс")}</span>
            <span className="num">{num(r.loss_median, "%")}</span>
            <span className="num">{num(r.jitter_median, " мс")}</span>
            <span className="num">{r.failures}</span>
          </div>
        ))}
        {(sm?.games ?? []).length === 0 && <div className="tr dim">Нет данных</div>}
      </div>

      {/* reports */}
      <div className="caps" style={{ margin: "4px 0 8px" }}>
        Отчёты: {reports.data ? total : "…"}
      </div>
      <div className="tbl">
        <div className="tr caps" style={{ ...HEAD, gridTemplateColumns: "60px 120px 1fr 1.3fr 1fr 80px 60px 80px" }}>
          <span>ID</span>
          <span>Получен</span>
          <span>Подписка</span>
          <span>Провайдер</span>
          <span>Регион</span>
          <span>Версия</span>
          <span>Режим</span>
          <span>Размер</span>
        </div>
        {items.map((r) => (
          <div
            key={r.id}
            className="tr click"
            style={{ gridTemplateColumns: "60px 120px 1fr 1.3fr 1fr 80px 60px 80px" }}
            onClick={() => showReport(r)}
          >
            <span className="mono dim">{r.id}</span>
            <span className="dim">{dtTime(r.received_at)}</span>
            <span className="mono">{sub(r)}</span>
            <span>{provider(r.asn, r.as_org)}</span>
            <span className="dim">{place(r)}</span>
            <span className="mono">{r.app_version}</span>
            <span className="dim">{r.mode ?? "—"}</span>
            <span className="num">{r.size ? bytesFmt(r.size) : "—"}</span>
          </div>
        ))}
        {items.length === 0 && <div className="tr dim">Отчётов пока нет</div>}
      </div>
      {total > PAGE && (
        <div className="row" style={{ gap: 8, marginTop: 10, justifyContent: "flex-end" }}>
          <button
            className="btn secondary sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE))}
          >
            ‹ Новее
          </button>
          <span className="caps">
            {offset + 1}–{Math.min(offset + PAGE, total)} из {total}
          </span>
          <button
            className="btn secondary sm"
            disabled={offset + PAGE >= total}
            onClick={() => setOffset(offset + PAGE)}
          >
            Старее ›
          </button>
        </div>
      )}

      {open && (
        <Modal title={`Отчёт #${open.id}`} onClose={() => setOpen(null)} wide>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 8 }}>
            {sub(open)} · {dtTime(open.received_at)} · {provider(open.asn, open.as_org)} ·{" "}
            {place(open)} · v{open.app_version} · согласие v{open.consent_version} · установка{" "}
            <span className="mono">{open.install_id}</span>
          </div>
          <pre
            className="mono"
            style={{
              maxHeight: "60vh",
              overflow: "auto",
              fontSize: 11.5,
              background: "var(--panel2)",
              padding: 10,
              borderRadius: 4,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >
            {open.payload === "***"
              ? "Содержимое отчёта видят только владелец и разработчик"
              : JSON.stringify(open.payload, null, 2)}
          </pre>
          <div className="row" style={{ gap: 8, marginTop: 12, justifyContent: "flex-end" }}>
            <button className="btn danger" onClick={() => deleteFor(open.subscription_id)}>
              Удалить все отчёты подписки #{open.subscription_id}
            </button>
            <button className="btn secondary" onClick={() => setOpen(null)}>
              Закрыть
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
