/* Settings → «LAN для игр»: under the category's params — a Headscale connection check (with
   the SAVED server + key) and a read-only list of rooms with member counts.
   See src/web/routes/admin/lan.py. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, dtTime } from "../api/client";

type Room = {
  id: number;
  name: string;
  owner_subscription_id: number;
  members: number;
  created_at: string | null;
};
type Check =
  | {
      ok: true;
      database: boolean;
      users: number;
      nodes: number;
      online: number;
      policy: string;
    }
  | { ok: false; error: string };

export default function LanPanel() {
  const rooms = useQuery({
    queryKey: ["lan-rooms"],
    queryFn: () => api.get<{ items: Room[]; total: number }>("/api/admin/settings/lan/rooms"),
  });
  const [check, setCheck] = useState<Check | null>(null);
  const [checking, setChecking] = useState(false);

  async function runCheck() {
    setChecking(true);
    try {
      setCheck(await api.post<Check>("/api/admin/settings/lan/check"));
    } catch (e) {
      setCheck({ ok: false, error: (e as Error).message });
    } finally {
      setChecking(false);
    }
  }

  const items = rooms.data?.items ?? [];
  return (
    <>
      <div className="tbl" style={{ marginTop: 16 }}>
        <div className="tr" style={{ gridTemplateColumns: "1fr auto" }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 13.5, fontWeight: 500 }}>Соединение с Headscale</b>
            <div className="muted" style={{ fontSize: 12, marginTop: 1 }}>
              Проверяются сохранённые адрес и ключ (сначала сохраните изменения); бот заодно
              записывает текущую политику доступа — так видно, что policy.mode = database
            </div>
            {check && (
              <div style={{ fontSize: 12.5, marginTop: 6 }}>
                {check.ok
                  ? `✅ Работает: база ${check.database ? "доступна" : "недоступна"}, ` +
                    `пользователей ${check.users}, устройств ${check.nodes} (в сети ${check.online}), ` +
                    `политика ${check.policy}`
                  : `❌ ${check.error}`}
              </div>
            )}
          </div>
          <div className="row">
            <button className="btn secondary" disabled={checking} onClick={runCheck}>
              {checking ? "Проверяю…" : "Проверить соединение"}
            </button>
          </div>
        </div>
      </div>

      <div className="caps" style={{ margin: "18px 0 8px" }}>
        Комнаты: {rooms.data?.total ?? "…"}
      </div>
      <div className="tbl">
        <div
          className="tr caps"
          style={{ gridTemplateColumns: "60px 1fr 130px 90px 120px", background: "var(--panel2)" }}
        >
          <span>ID</span>
          <span>Название</span>
          <span>Подписка владельца</span>
          <span>Игроков</span>
          <span>Создана</span>
        </div>
        {items.map((r) => (
          <div key={r.id} className="tr" style={{ gridTemplateColumns: "60px 1fr 130px 90px 120px" }}>
            <span className="mono dim">{r.id}</span>
            <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{r.name}</span>
            <span className="mono">#{r.owner_subscription_id}</span>
            <span className="num">{r.members}</span>
            <span className="dim">{dtTime(r.created_at)}</span>
          </div>
        ))}
        {items.length === 0 && <div className="tr dim">Комнат пока нет</div>}
      </div>
    </>
  );
}
