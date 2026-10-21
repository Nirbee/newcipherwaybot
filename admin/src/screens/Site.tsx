/* Screen — Сайт: the public sales site (/) and the web cabinet (/web, e-mail sign-up/login and
   purchase without Telegram) in one place — links to hand out, the site's copy, the cabinet
   switches, SMTP with a real test letter, Google/Yandex/VK sign-in.
   Backend: src/web/routes/admin/site.py. */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { Field, Toggle } from "../components/ui";
import { useApp } from "../state/app";

type Feature = { id: string; icon: string; title: string; text: string };
type Faq = { id: string; q: string; a: string };
type Landing = {
  enabled?: boolean;
  headline?: string;
  subheadline?: string;
  cta_target?: "web" | "bot";
  features?: Feature[];
  faq?: Faq[];
};
type Secret = { set: boolean };
type SiteState = {
  site_url: string;
  cabinet_url: string;
  oauth_redirect: string;
  template: string;
  landing: Landing;
  mail_ready: boolean;
  settings: {
    WEB_CABINET_ENABLED: boolean;
    CABINET_URL: string;
    CABINET_EMAIL_VERIFICATION: boolean;
    SMTP_HOST: string;
    SMTP_PORT: number;
    SMTP_USER: string;
    SMTP_PASSWORD: Secret;
    SMTP_FROM: string;
    OAUTH_GOOGLE_CLIENT_ID: string;
    OAUTH_GOOGLE_CLIENT_SECRET: Secret;
    OAUTH_YANDEX_CLIENT_ID: string;
    OAUTH_YANDEX_CLIENT_SECRET: Secret;
    OAUTH_VK_CLIENT_ID: string;
    OAUTH_VK_CLIENT_SECRET: Secret;
  };
};

const SECRET_KEYS = [
  "SMTP_PASSWORD",
  "OAUTH_GOOGLE_CLIENT_SECRET",
  "OAUTH_YANDEX_CLIENT_SECRET",
  "OAUTH_VK_CLIENT_SECRET",
] as const;

const SMTP_PRESETS: { name: string; host: string; port: number; hint: string }[] = [
  { name: "Яндекс", host: "smtp.yandex.ru", port: 465, hint: "нужен «пароль приложения» в настройках Яндекс ID" },
  { name: "Mail.ru", host: "smtp.mail.ru", port: 465, hint: "нужен «пароль для внешних приложений»" },
  { name: "Gmail", host: "smtp.gmail.com", port: 587, hint: "нужен «пароль приложения» (включите 2FA)" },
];

let seq = 0;
const newId = (p: string) => `${p}${Date.now().toString(36)}${(seq++).toString(36)}`;

export default function Site() {
  const { toast } = useApp();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["site"], queryFn: () => api.get<SiteState>("/api/admin/site") });

  const [landing, setLanding] = useState<Landing>({});
  const [vals, setVals] = useState<Record<string, string | number | boolean>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testTo, setTestTo] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [preview, setPreview] = useState<"desktop" | "phone">("desktop");
  const [previewKey, setPreviewKey] = useState(0);

  useEffect(() => {
    if (!q.data || dirty) return;
    setLanding(q.data.landing ?? {});
    const s = q.data.settings;
    setVals({
      WEB_CABINET_ENABLED: s.WEB_CABINET_ENABLED,
      CABINET_URL: s.CABINET_URL ?? "",
      CABINET_EMAIL_VERIFICATION: s.CABINET_EMAIL_VERIFICATION,
      SMTP_HOST: s.SMTP_HOST ?? "",
      SMTP_PORT: s.SMTP_PORT ?? 587,
      SMTP_USER: s.SMTP_USER ?? "",
      SMTP_FROM: s.SMTP_FROM ?? "",
      OAUTH_GOOGLE_CLIENT_ID: s.OAUTH_GOOGLE_CLIENT_ID ?? "",
      OAUTH_YANDEX_CLIENT_ID: s.OAUTH_YANDEX_CLIENT_ID ?? "",
      OAUTH_VK_CLIENT_ID: s.OAUTH_VK_CLIENT_ID ?? "",
    });
    setSecrets({});
  }, [q.data, dirty]);

  function setVal(key: string, value: string | number | boolean) {
    setVals((v) => ({ ...v, [key]: value }));
    setDirty(true);
  }
  function patchLanding(p: Partial<Landing>) {
    setLanding((l) => ({ ...l, ...p }));
    setDirty(true);
  }
  function patchItem<K extends "features" | "faq">(key: K, i: number, field: string, value: string) {
    setLanding((l) => {
      const list = [...((l[key] as (Feature | Faq)[] | undefined) ?? [])];
      list[i] = { ...list[i], [field]: value } as Feature & Faq;
      return { ...l, [key]: list };
    });
    setDirty(true);
  }

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      toast(`${what}: ссылка скопирована`);
    } catch {
      toast("Не удалось скопировать — выделите ссылку вручную");
    }
  }

  async function save() {
    setSaving(true);
    try {
      const settings: Record<string, unknown> = { ...vals };
      for (const k of SECRET_KEYS) if (secrets[k]) settings[k] = secrets[k];
      await api.patch("/api/admin/site", { settings, landing });
      setDirty(false);
      setPreviewKey((k) => k + 1);
      void qc.invalidateQueries({ queryKey: ["site"] });
      void qc.invalidateQueries({ queryKey: ["miniapp"] });
      toast("Сохранено");
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function sendTest() {
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await api.post<{ ok: boolean; error?: string }>("/api/admin/site/test-email", { to: testTo.trim() }));
    } catch (e) {
      setTestResult({ ok: false, error: (e as Error).message });
    } finally {
      setTesting(false);
    }
  }

  const d = q.data;
  if (!d) return <div className="dim">{q.isError ? (q.error as Error).message : "Загрузка…"}</div>;
  const cabinetOn = Boolean(vals.WEB_CABINET_ENABLED);
  const siteUrl = d.site_url || `${window.location.origin}/`;
  const cabinetUrl = d.cabinet_url || `${window.location.origin}/web/`;

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="h1">Сайт</h1>
          <div className="sub dim" style={{ fontSize: 13 }}>
            Для тех, у кого нет VPN и не открывается Telegram: тарифы, регистрация по почте и оплата.
          </div>
        </div>
        <div className="actions">
          <button className="btn primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? "Сохраняю…" : dirty ? "Сохранить" : "Сохранено"}
          </button>
        </div>
      </div>

      {/* links */}
      <div className="site-links">
        {[
          { title: "Сайт с тарифами", url: siteUrl, hint: "Главная: тарифы, вопросы, кнопка «Личный кабинет»" },
          {
            title: "Личный кабинет",
            url: cabinetUrl,
            hint: "Регистрация и вход по почте, покупка и ссылка на подписку — без Telegram",
          },
          {
            title: "Продление по @username",
            url: `${cabinetUrl}?renew=1`,
            hint: "Для тех, кто покупал в боте и забыл продлить: вводит @username, платит — VPN снова работает",
          },
        ].map((l) => (
          <div key={l.title} className="card site-link">
            <span className="caps">{l.title}</span>
            <code className="site-url">{l.url}</code>
            <span className="dim" style={{ fontSize: 12 }}>{l.hint}</span>
            <div className="row" style={{ gap: 6 }}>
              <button className="btn primary sm" onClick={() => void copy(l.url, l.title)}>
                📋 Скопировать
              </button>
              <a className="btn secondary sm" href={l.url} target="_blank" rel="noreferrer">
                Открыть ↗
              </a>
            </div>
          </div>
        ))}
      </div>
      {!cabinetOn && (
        <div className="site-warn">
          ⚠️ Веб-кабинет выключен: на сайте нельзя зарегистрироваться и купить — кнопки ведут в бота.
          Включите «Регистрация и покупка на сайте» ниже.
        </div>
      )}

      <div className="cols">
        <div className="grid" style={{ gap: 16, flex: "1 1 420px", alignContent: "start" }}>
          {/* site copy */}
          <div className="card">
            <div className="row spread" style={{ marginBottom: 10 }}>
              <span className="caps">🌐 Содержимое сайта</span>
              <label className="row" style={{ gap: 6, fontSize: 13, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={landing.enabled !== false}
                  onChange={(e) => patchLanding({ enabled: e.target.checked })}
                />
                Сайт включён
              </label>
            </div>
            <div className="grid" style={{ gap: 10 }}>
              <Field label="Куда ведёт кнопка «Личный кабинет»">
                <select
                  className="input"
                  value={landing.cta_target ?? "web"}
                  onChange={(e) => patchLanding({ cta_target: e.target.value as "web" | "bot" })}
                >
                  <option value="web">В личный кабинет на сайте (без Telegram)</option>
                  <option value="bot">В Telegram-бота</option>
                </select>
              </Field>
              <Field label="Заголовок">
                <input
                  className="input"
                  value={landing.headline ?? ""}
                  placeholder="Свобода в сети — за одну минуту"
                  maxLength={120}
                  onChange={(e) => patchLanding({ headline: e.target.value })}
                />
              </Field>
              <Field label="Подзаголовок">
                <textarea
                  className="input"
                  style={{ minHeight: 48, resize: "vertical" }}
                  value={landing.subheadline ?? ""}
                  placeholder="Высокая скорость, стабильное соединение и простое подключение…"
                  maxLength={300}
                  onChange={(e) => patchLanding({ subheadline: e.target.value })}
                />
              </Field>

              <div className="row spread">
                <span className="caps">Преимущества (до 8)</span>
                <button
                  className="btn secondary sm"
                  onClick={() =>
                    patchLanding({
                      features: [...(landing.features ?? []), { id: newId("f"), icon: "", title: "", text: "" }],
                    })
                  }
                >
                  + Добавить
                </button>
              </div>
              {(landing.features ?? []).map((f, i) => (
                <div key={f.id} className="row" style={{ gap: 6 }}>
                  <input
                    className="input"
                    style={{ width: 46, textAlign: "center" }}
                    placeholder="⚡"
                    value={f.icon}
                    maxLength={8}
                    onChange={(e) => patchItem("features", i, "icon", e.target.value)}
                  />
                  <input
                    className="input"
                    style={{ flex: "1 1 110px" }}
                    placeholder="Заголовок"
                    value={f.title}
                    maxLength={60}
                    onChange={(e) => patchItem("features", i, "title", e.target.value)}
                  />
                  <input
                    className="input"
                    style={{ flex: "2 1 150px" }}
                    placeholder="Текст"
                    value={f.text}
                    maxLength={200}
                    onChange={(e) => patchItem("features", i, "text", e.target.value)}
                  />
                  <button
                    className="btn danger sm"
                    onClick={() => patchLanding({ features: (landing.features ?? []).filter((_, j) => j !== i) })}
                  >
                    ✕
                  </button>
                </div>
              ))}
              {!(landing.features ?? []).length && (
                <div className="dim" style={{ fontSize: 12 }}>Пусто — на сайте показываются стандартные преимущества.</div>
              )}

              <div className="row spread">
                <span className="caps">Вопросы и ответы (до 8)</span>
                <button
                  className="btn secondary sm"
                  onClick={() => patchLanding({ faq: [...(landing.faq ?? []), { id: newId("q"), q: "", a: "" }] })}
                >
                  + Добавить
                </button>
              </div>
              {(landing.faq ?? []).map((item, i) => (
                <div key={item.id} className="grid" style={{ gap: 4 }}>
                  <div className="row" style={{ gap: 6 }}>
                    <input
                      className="input"
                      style={{ flex: 1 }}
                      placeholder="Вопрос"
                      value={item.q}
                      maxLength={160}
                      onChange={(e) => patchItem("faq", i, "q", e.target.value)}
                    />
                    <button
                      className="btn danger sm"
                      onClick={() => patchLanding({ faq: (landing.faq ?? []).filter((_, j) => j !== i) })}
                    >
                      ✕
                    </button>
                  </div>
                  <textarea
                    className="input"
                    style={{ minHeight: 44, resize: "vertical" }}
                    placeholder="Ответ"
                    value={item.a}
                    maxLength={600}
                    onChange={(e) => patchItem("faq", i, "a", e.target.value)}
                  />
                </div>
              ))}
              <div className="dim" style={{ fontSize: 12 }}>
                Тема оформления — та же, что у мини-приложения («{d.template}»), меняется там. Тарифы на сайте берутся
                из «Тарифов и цен».
              </div>
            </div>
          </div>

          {/* sign-up / login */}
          <div className="card">
            <span className="caps">🔐 Регистрация и вход на сайте</span>
            <div className="grid" style={{ gap: 12, marginTop: 10 }}>
              <div className="row spread">
                <span>
                  Регистрация и покупка на сайте
                  <div className="dim" style={{ fontSize: 12 }}>Вход по почте и паролю, оплата без Telegram</div>
                </span>
                <Toggle on={cabinetOn} onChange={() => setVal("WEB_CABINET_ENABLED", !cabinetOn)} />
              </div>
              <div className="row spread">
                <span>
                  Подтверждать почту письмом
                  <div className="dim" style={{ fontSize: 12 }}>
                    Выкл — аккаунт работает сразу. Вкл — нужна настроенная почта ниже
                  </div>
                </span>
                <Toggle
                  on={Boolean(vals.CABINET_EMAIL_VERIFICATION)}
                  onChange={() => setVal("CABINET_EMAIL_VERIFICATION", !vals.CABINET_EMAIL_VERIFICATION)}
                />
              </div>
              {Boolean(vals.CABINET_EMAIL_VERIFICATION) && !d.mail_ready && (
                <div className="site-warn" style={{ margin: 0 }}>
                  ⚠️ Подтверждение включено, а почта не настроена — письма не уйдут и новые пользователи не
                  смогут войти. Настройте почту ниже или выключите подтверждение.
                </div>
              )}
              <Field label="Адрес личного кабинета">
                <input
                  className="input"
                  value={String(vals.CABINET_URL ?? "")}
                  placeholder={cabinetUrl}
                  onChange={(e) => setVal("CABINET_URL", e.target.value)}
                />
              </Field>
            </div>
          </div>

          {/* SMTP */}
          <div className="card">
            <div className="row spread">
              <span className="caps">✉️ Почта для писем</span>
              <span className={`cap-pill`} style={{ color: d.mail_ready ? "var(--good-ink)" : "var(--warn)" }}>
                {d.mail_ready ? "настроена" : "не настроена"}
              </span>
            </div>
            <div className="dim" style={{ fontSize: 12, margin: "6px 0 10px" }}>
              Письма подтверждения регистрации. Быстрая настройка:
              {SMTP_PRESETS.map((p) => (
                <button
                  key={p.name}
                  className="btn secondary sm"
                  style={{ marginLeft: 6 }}
                  title={p.hint}
                  onClick={() => {
                    setVal("SMTP_HOST", p.host);
                    setVal("SMTP_PORT", p.port);
                    toast(`${p.name}: ${p.hint}`);
                  }}
                >
                  {p.name}
                </button>
              ))}
            </div>
            <div className="grid" style={{ gap: 10 }}>
              <div className="row" style={{ gap: 8 }}>
                <Field label="SMTP-сервер">
                  <input
                    className="input"
                    value={String(vals.SMTP_HOST ?? "")}
                    placeholder="smtp.yandex.ru"
                    onChange={(e) => setVal("SMTP_HOST", e.target.value)}
                  />
                </Field>
                <Field label="Порт">
                  <input
                    className="input"
                    style={{ width: 90 }}
                    inputMode="numeric"
                    value={String(vals.SMTP_PORT ?? "")}
                    onChange={(e) => setVal("SMTP_PORT", Number(e.target.value.replace(/\D/g, "")) || 0)}
                  />
                </Field>
              </div>
              <Field label="Логин (обычно почта целиком)">
                <input
                  className="input"
                  value={String(vals.SMTP_USER ?? "")}
                  placeholder="noreply@cipherway.ru"
                  onChange={(e) => setVal("SMTP_USER", e.target.value)}
                />
              </Field>
              <Field label="Пароль приложения">
                <input
                  className="input"
                  type="password"
                  value={secrets.SMTP_PASSWORD ?? ""}
                  placeholder={d.settings.SMTP_PASSWORD.set ? "задан — оставьте пустым, чтобы не менять" : "пароль"}
                  onChange={(e) => {
                    setSecrets((s) => ({ ...s, SMTP_PASSWORD: e.target.value }));
                    setDirty(true);
                  }}
                />
              </Field>
              <Field label="Отправитель (адрес в поле «От кого»)">
                <input
                  className="input"
                  value={String(vals.SMTP_FROM ?? "")}
                  placeholder="CipherWay <noreply@cipherway.ru>"
                  onChange={(e) => setVal("SMTP_FROM", e.target.value)}
                />
              </Field>
              <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                <input
                  className="input"
                  style={{ flex: "1 1 200px" }}
                  placeholder="куда отправить тестовое письмо"
                  value={testTo}
                  onChange={(e) => setTestTo(e.target.value)}
                />
                <button
                  className="btn secondary"
                  disabled={testing || dirty || !testTo.includes("@")}
                  title={dirty ? "Сначала сохраните настройки" : ""}
                  onClick={() => void sendTest()}
                >
                  {testing ? "Отправляю…" : "Тестовое письмо"}
                </button>
              </div>
              {dirty && <div className="dim" style={{ fontSize: 12 }}>Сохраните настройки, затем отправьте тест.</div>}
              {testResult && (
                <div style={{ fontSize: 12.5, color: testResult.ok ? "var(--good-ink)" : "var(--bad-ink)" }}>
                  {testResult.ok ? "✓ Письмо отправлено — проверьте почту (и папку «Спам»)" : `✕ ${testResult.error}`}
                </div>
              )}
            </div>
          </div>

          {/* OAuth */}
          <details className="card">
            <summary className="caps" style={{ cursor: "pointer" }}>
              Вход через Google / Яндекс / VK (необязательно)
            </summary>
            <div className="grid" style={{ gap: 10, marginTop: 10 }}>
              <div className="dim" style={{ fontSize: 12 }}>
                В кабинете разработчика провайдера укажите адрес возврата (Redirect URI):
                <div className="row" style={{ gap: 6, marginTop: 4 }}>
                  <code className="site-url" style={{ flex: 1 }}>{d.oauth_redirect || cabinetUrl}</code>
                  <button className="btn secondary sm" onClick={() => void copy(d.oauth_redirect || cabinetUrl, "Redirect URI")}>
                    📋
                  </button>
                </div>
              </div>
              {(
                [
                  ["GOOGLE", "Google"],
                  ["YANDEX", "Яндекс"],
                  ["VK", "VK"],
                ] as const
              ).map(([key, name]) => {
                const idKey = `OAUTH_${key}_CLIENT_ID`;
                const secKey = `OAUTH_${key}_CLIENT_SECRET` as (typeof SECRET_KEYS)[number];
                const secretSet = d.settings[secKey].set;
                return (
                  <div key={key} className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    <span style={{ width: 70, fontSize: 13 }}>{name}</span>
                    <input
                      className="input"
                      style={{ flex: "1 1 160px" }}
                      placeholder="Client ID"
                      value={String(vals[idKey] ?? "")}
                      onChange={(e) => setVal(idKey, e.target.value)}
                    />
                    <input
                      className="input"
                      type="password"
                      style={{ flex: "1 1 160px" }}
                      placeholder={secretSet ? "Secret задан" : "Client Secret"}
                      value={secrets[secKey] ?? ""}
                      onChange={(e) => {
                        setSecrets((s) => ({ ...s, [secKey]: e.target.value }));
                        setDirty(true);
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </details>
        </div>

        {/* preview */}
        <div className="card" style={{ flex: "1 1 380px", alignSelf: "start", position: "sticky", top: 12 }}>
          <div className="row spread" style={{ marginBottom: 8 }}>
            <span className="caps">Предпросмотр</span>
            <span className="row" style={{ gap: 6 }}>
              <button
                className={`btn sm ${preview === "desktop" ? "primary" : "secondary"}`}
                onClick={() => setPreview("desktop")}
              >
                🖥
              </button>
              <button
                className={`btn sm ${preview === "phone" ? "primary" : "secondary"}`}
                onClick={() => setPreview("phone")}
              >
                📱
              </button>
            </span>
          </div>
          <div className={`site-preview ${preview}`}>
            <iframe key={previewKey} title="Сайт" src="/" />
          </div>
          <div className="dim" style={{ fontSize: 12, marginTop: 6 }}>
            Показывает сохранённую версию — после «Сохранить» обновится сам.
          </div>
        </div>
      </div>
    </>
  );
}
