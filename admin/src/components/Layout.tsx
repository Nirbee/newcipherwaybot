/* App shell: sidebar (groups, badges, live statuses; off-canvas on phones) + topbar
   (crumbs, live panel badge, theme toggle, avatar). */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { api, setToken } from "../api/client";
import { useApp } from "../state/app";

type Me = { user_id: number; username: string; role: string; allowed_screens: string[] | null };
type Counters = { all: number };
type TicketsResp = { open_count: number };
type SystemInfo = { redis: string; panel: { status: string; version?: string } };

const ROLE_RU: Record<string, string> = {
  OWNER: "Владелец",
  ADMIN: "Администратор",
  MODERATOR: "Модератор",
};

/* Live build version, bottom-right. Polls /api/version; when the server ships a newer
   build than the one this page loaded with, it offers a one-click reload (index.html is
   served no-cache, so the reload pulls the fresh SPA). Fixes "фронт не подхватывает
   свежую версию" + shows the actual running version. */
function VersionBadge() {
  const v = useQuery({
    queryKey: ["app-version"],
    queryFn: () => api.get<{ version: string; build: string }>("/api/version"),
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const loaded = useRef<string | null>(null);
  const current = v.data?.version ?? null;
  if (current && loaded.current === null) loaded.current = current;
  const stale = current !== null && loaded.current !== null && current !== loaded.current;
  return (
    <div
      style={{
        position: "fixed",
        right: 12,
        bottom: 10,
        zIndex: 50,
        fontSize: 11,
        display: "flex",
        alignItems: "center",
        gap: 8,
        pointerEvents: stale ? "auto" : "none",
      }}
    >
      {stale && (
        <button
          onClick={() => window.location.reload()}
          style={{
            border: "1px solid var(--accent, #F7971D)",
            background: "var(--accent, #F7971D)",
            color: "#000",
            borderRadius: 20,
            padding: "4px 11px",
            cursor: "pointer",
            fontWeight: 600,
            fontSize: 11,
          }}
        >
          🔄 Новая версия — обновить
        </button>
      )}
      <span
        className="mono"
        style={{ color: "var(--dim)", background: "var(--panel)", padding: "3px 8px", borderRadius: 6, opacity: 0.85 }}
      >
        v{current ?? "…"}
      </span>
    </div>
  );
}

export function BrandLogo({ size = 15 }: { size?: number }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        fontFamily: "'Arial Black','Arial Bold',Arial,sans-serif",
        fontWeight: 900,
        fontSize: size,
        letterSpacing: "-0.5px",
        lineHeight: 1,
      }}
    >
      <span style={{ color: "var(--text)" }}>Cipher</span>
      <span
        style={{
          background: "#F7971D",
          color: "#000",
          borderRadius: size * 0.28,
          padding: `${size * 0.14}px ${size * 0.38}px`,
        }}
      >
        Way
      </span>
    </span>
  );
}

export default function Layout() {
  const { t, theme, setTheme } = useApp();
  const [navOpen, setNavOpen] = useState(false);
  const qc = useQueryClient();
  const loc = useLocation();
  const nav = useNavigate();

  const me = useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/admin/auth/me") });
  const counters = useQuery({
    queryKey: ["users", "counters"],
    queryFn: () => api.get<Counters>("/api/admin/users/counters"),
    refetchInterval: 60_000,
  });
  const tickets = useQuery({
    queryKey: ["tickets"],
    queryFn: () => api.get<TicketsResp>("/api/admin/tickets"),
    refetchInterval: 60_000,
  });
  const sys = useQuery({
    queryKey: ["system"],
    queryFn: () => api.get<SystemInfo>("/api/admin/dashboard/system"),
    refetchInterval: 60_000,
    retry: false,
  });
  const panelOk = sys.data ? sys.data.panel.status === "ok" : null;
  const redisOk = sys.data ? sys.data.redis === "ok" : null;
  const apiOk = sys.isError ? false : sys.data ? true : null;
  const dotCls = (ok: boolean | null) => `status-dot ${ok === null ? "" : ok ? "ok" : "err"}`;

  useEffect(() => setNavOpen(false), [loc.pathname]);

  const items: {
    group?: string;
    icon: string;
    path: string;
    label: string;
    badge?: number;
  }[] = [
    { icon: "📊", path: "/", label: t.dashboard },
    { group: t.gProduct, icon: "📈", path: "/stats", label: t.statsTitle },
    { icon: "👥", path: "/users", label: t.users, badge: counters.data?.all },
    { icon: "💳", path: "/tariffs", label: t.tariffs },
    { icon: "🏷️", path: "/promos", label: t.promos },
    { group: t.gConstructor, icon: "🧱", path: "/bot-buttons", label: t.botButtons },
    { icon: "🖼️", path: "/bot-images", label: t.botImages },
    { icon: "📱", path: "/miniapp", label: t.miniapp },
    { group: t.gMarketing, icon: "📣", path: "/broadcasts", label: t.broadcasts },
    { icon: "⏰", path: "/smart", label: t.smart },
    { icon: "🔔", path: "/notifications", label: t.notifications },
    { icon: "⏳", path: "/reminders", label: t.reminders },
    { icon: "📈", path: "/campaigns", label: t.campaigns },
    { icon: "🔥", path: "/sales", label: t.salesTitle },
    { icon: "🎯", path: "/promo-groups", label: t.promoGroups },
    { icon: "🤝", path: "/partners", label: t.partners },
    { group: t.gOps, icon: "💰", path: "/payments", label: t.payments },
    { icon: "🎫", path: "/tickets", label: t.tickets, badge: tickets.data?.open_count },
    { icon: "🤖", path: "/ai-support", label: t.aiSupport },
    { icon: "🚫", path: "/blacklist", label: t.blacklist },
    { group: t.gSystem, icon: "🌍", path: "/servers", label: t.servers },
    { icon: "📡", path: "/routers", label: t.routers },
    { icon: "⚙️", path: "/settings", label: t.settings },
    { icon: "🛠️", path: "/maintenance", label: t.maintenance },
    ...(me.data?.role === "OWNER"
      ? [{ icon: "🔑", path: "/admins", label: t.admins }]
      : []),
  ];

  // A scoped staff account only sees (and may only reach) its granted screens — the backend
  // already 403s anything else, this just keeps the sidebar and routing from offering dead ends.
  const allowedScreens = me.data?.allowed_screens ?? null;
  const visibleItems = allowedScreens
    ? items.filter((i) => allowedScreens.includes(i.path.slice(1)))
    : items;

  useEffect(() => {
    if (!allowedScreens) return;
    const segment = loc.pathname === "/" ? "" : loc.pathname.split("/")[1];
    if (!allowedScreens.includes(segment)) {
      nav(allowedScreens[0] ? `/${allowedScreens[0]}` : "/login", { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowedScreens, loc.pathname]);

  const current = visibleItems.find(
    (i) => i.path === (loc.pathname === "/" ? "/" : "/" + loc.pathname.split("/")[1]),
  );

  const [navQ, setNavQ] = useState("");
  const filteredItems = useMemo(() => {
    if (!navQ.trim()) return visibleItems;
    const n = navQ.toLowerCase();
    return visibleItems.filter((i) => i.label.toLowerCase().includes(n));
  }, [visibleItems, navQ]);

  // Settings quick-search: jump straight to the matching parameter block.
  const paramHits = useQuery({
    queryKey: ["nav-param-search", navQ],
    queryFn: () =>
      api.get<{ params: { key: string; name: string; category: string }[] }>(
        `/api/admin/settings?q=${encodeURIComponent(navQ)}`,
      ),
    enabled: navQ.trim().length >= 2,
  });

  return (
    <div className="shell">
      <div className={`nav-scrim${navOpen ? " open" : ""}`} onClick={() => setNavOpen(false)} />
      <aside className={`sidebar${navOpen ? " open" : ""}`}>
        <div className="side-logo">
          <div className="row" style={{ gap: 8 }}>
            <BrandLogo size={16} />
            <span className="caps">кабинет</span>
          </div>
          <div style={{ position: "relative", marginTop: 12 }}>
            <span className="dim" style={{ position: "absolute", left: 9, top: "50%", transform: "translateY(-50%)", fontSize: 13 }}>⌕</span>
            <input
              className="input"
              style={{ width: "100%", paddingLeft: 28, fontSize: 12.5 }}
              placeholder={t.sideSearch}
              value={navQ}
              onChange={(e) => setNavQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setNavQ("");
                if (e.key === "Enter" && filteredItems[0]) {
                  nav(filteredItems[0].path);
                  setNavQ("");
                }
              }}
            />
          </div>
        </div>
        <nav style={{ paddingBottom: 12 }}>
          {navQ.trim().length >= 2 && (paramHits.data?.params ?? []).length > 0 && (
            <>
              <div className="side-group caps">{t.sideSearchParams}</div>
              {(paramHits.data?.params ?? []).slice(0, 5).map((prm) => (
                <button
                  key={prm.key}
                  className="side-item"
                  style={{ width: "100%", textAlign: "left", background: "none", border: 0 }}
                  onClick={() => {
                    sessionStorage.setItem("settings_q", prm.name);
                    setNavQ("");
                    nav("/settings");
                  }}
                >
                  <span className="ico">⚙️</span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{prm.name}</span>
                </button>
              ))}
            </>
          )}
          {filteredItems.map((i) => (
            <span key={i.path}>
              {i.group && <div className="side-group caps">{i.group}</div>}
              <NavLink
                to={i.path}
                end={i.path === "/"}
                className={({ isActive }) => "side-item" + (isActive ? " active" : "")}
              >
                <span className="ico">{i.icon}</span>
                {i.label}
                {i.badge !== undefined && i.badge > 0 && (
                  <span className="badge">{i.badge.toLocaleString("ru-RU")}</span>
                )}
              </NavLink>
            </span>
          ))}
        </nav>
        <div className="side-footer">
          <span className="muted">
            <span className={dotCls(apiOk)} />
            Сервер бота {apiOk === false ? "— нет связи" : ""}
          </span>
          <span className="muted">
            <span className={dotCls(panelOk)} />
            Remnawave {sys.data?.panel.version ? `· ${sys.data.panel.version}` : ""}
          </span>
          <span className="muted">
            <span className={dotCls(redisOk)} />
            Redis
          </span>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <button
            className="icon-btn menu-btn"
            aria-label="Меню"
            onClick={() => setNavOpen((o) => !o)}
          >
            ☰
          </button>
          <span className="crumbs">
            <span className="hide-sm">Панель / </span>
            <b>{current?.label ?? ""}</b>
          </span>
          <span className="spacer" />
          <span className={`cap-pill hide-sm${panelOk === false ? "" : " accent"}`}>
            <span className={dotCls(panelOk)} />
            Remnawave · {panelOk === null ? "…" : panelOk ? "OK" : "ошибка"}
          </span>
          <button
            className="icon-btn"
            title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}
            aria-label="Сменить тему"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          >
            {theme === "dark" ? "☀️" : "🌙"}
          </button>
          <div className="row" style={{ gap: 8 }}>
            <div className="avatar-sq">
              {(me.data?.username ?? "??").slice(0, 2).toUpperCase()}
            </div>
            <div className="hide-sm" style={{ lineHeight: 1.25 }}>
              <div style={{ fontSize: 12.5, fontWeight: 500 }}>@{me.data?.username}</div>
              <div className="dim" style={{ fontSize: 11 }}>
                {ROLE_RU[me.data?.role ?? ""] ?? me.data?.role}
              </div>
            </div>
            <button
              className="icon-btn"
              title={t.logout}
              onClick={() => {
                setToken(null);
                qc.clear(); // drop the previous admin's cached data before the next login
                nav("/login");
              }}
            >
              ⎋
            </button>
          </div>
        </header>
        <main className="content">
          <div className="content-inner page-enter" key={loc.pathname}>
            <Outlet />
          </div>
        </main>
      </div>
      <VersionBadge />
    </div>
  );
}
