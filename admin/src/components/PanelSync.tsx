/* «Синхронизация с панелью»: people who exist in Remnawave but not in the bot, adopted on
   demand. Backend: GET/POST /api/admin/users/panel-sync (src/application/services/panel_import.py). */

import { useState } from "react";

import { api, bytesFmt, dt } from "../api/client";
import { useApp } from "../state/app";
import { Modal } from "./ui";

type PanelOnly = {
  key: string;
  username: string;
  telegram_id: number | null;
  bot_user_id: number | null;
  bot_has_live_sub: boolean;
  status: string | null;
  expire_at: string | null;
  traffic_used_bytes: number;
  device_limit: number | null;
  tag: string | null;
  description: string | null;
};
type Preview = { panel_total: number; bot_total: number; items: PanelOnly[] };
type Result = {
  users_created: number;
  users_matched: number;
  subscriptions: number;
  named_from_telegram: number;
};

const STATUS_RU: Record<string, string> = {
  ACTIVE: "активна",
  DISABLED: "отключена",
  LIMITED: "лимит трафика",
  EXPIRED: "истекла",
};

export function PanelSyncButton({ onDone }: { onDone: () => void }) {
  const { toast } = useApp();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<Preview | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);

  async function load() {
    setOpen(true);
    setData(null);
    setResult(null);
    try {
      const r = await api.get<Preview>("/api/admin/users/panel-sync");
      setData(r);
      // Pre-tick real customers (with a Telegram ID) who don't already have a working
      // subscription in the bot; hand-made panel accounts stay unticked.
      setPicked(new Set(r.items.filter((i) => i.telegram_id && !i.bot_has_live_sub).map((i) => i.key)));
    } catch (e) {
      toast((e as Error).message);
      setOpen(false);
    }
  }

  async function run() {
    if (picked.size === 0) return;
    setBusy(true);
    try {
      const r = await api.post<Result>("/api/admin/users/panel-sync", { keys: [...picked] });
      setResult(r);
      onDone();
      const left = await api.get<Preview>("/api/admin/users/panel-sync");
      setData(left);
      setPicked(new Set());
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggle(key: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <>
      <button className="btn secondary" onClick={() => void load()}>
        ⇄ Синхронизация с панелью
      </button>
      {open && (
        <Modal title="Синхронизация с панелью" onClose={() => setOpen(false)} wide>
          {!data ? (
            <div className="dim">Загружаю пользователей панели…</div>
          ) : (
            <div className="grid" style={{ gap: 12 }}>
              <div style={{ fontSize: 13 }}>
                В панели <b>{data.panel_total}</b>, в боте <b>{data.bot_total}</b>. Только в панели:{" "}
                <b>{data.items.length}</b>.
              </div>
              {result && (
                <div className="sync-result">
                  ✓ Добавлено подписок: {result.subscriptions} (новых пользователей {result.users_created}
                  {result.users_matched ? `, найдено в боте ${result.users_matched}` : ""}). Имя из
                  Telegram подтянуто у {result.named_from_telegram} — остальные видны как «idN» с
                  Telegram ID.
                </div>
              )}
              {data.items.length === 0 ? (
                <div className="dim">Все пользователи панели уже есть в боте.</div>
              ) : (
                <>
                  <div className="dim" style={{ fontSize: 12, lineHeight: 1.45 }}>
                    Отмеченные появятся в боте с той же подпиской: панель не меняется, ссылки и
                    устройства у людей продолжат работать. После этого их подпиской управляет бот —
                    продлевайте через бота, а не в панели (ночная сверка вернёт значения бота).
                    Без Telegram ID — аккаунты, созданные вручную: появятся под именем из панели.
                  </div>
                  <div className="sync-list">
                    {data.items.map((i) => (
                      <label key={i.key} className="sync-row">
                        <input
                          type="checkbox"
                          checked={picked.has(i.key)}
                          onChange={() => toggle(i.key)}
                        />
                        <span className="sync-main">
                          <b className="mono">{i.username}</b>
                          <span className="dim">
                            {i.telegram_id ? ` · TG ${i.telegram_id}` : " · без Telegram"}
                            {i.bot_has_live_sub ? " · в боте уже есть своя подписка" : ""}
                            {i.tag ? ` · ${i.tag}` : ""}
                          </span>
                        </span>
                        <span className="sync-meta dim">
                          {STATUS_RU[i.status ?? ""] ?? i.status ?? "—"}
                          {i.expire_at ? ` до ${dt(i.expire_at)}` : ""} · {bytesFmt(i.traffic_used_bytes)}
                        </span>
                      </label>
                    ))}
                  </div>
                  <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
                    <span className="row" style={{ gap: 6 }}>
                      <button
                        className="btn secondary sm"
                        onClick={() => setPicked(new Set(data.items.map((i) => i.key)))}
                      >
                        Отметить все
                      </button>
                      <button className="btn secondary sm" onClick={() => setPicked(new Set())}>
                        Снять все
                      </button>
                    </span>
                    <button
                      className="btn primary"
                      disabled={busy || picked.size === 0}
                      onClick={() => void run()}
                    >
                      {busy ? "Добавляю…" : `Добавить в бота (${picked.size})`}
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </Modal>
      )}
    </>
  );
}
