/* Thin fetch wrapper: bearer token, JSON, 401 -> logout redirect. */

const TOKEN_KEY = "admin_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(humanError(status, detail));
    this.status = status;
    this.detail = detail;
  }
}

/* Map raw API details to admin-friendly text. Unknown details fall back
   to a generic per-status message with the raw detail appended for debugging. */
const ERR: Record<string, string> = {
  "demo is read-only": "Демо-режим: только просмотр, изменения запрещены",
  "invalid credentials": "Неверный логин или пароль",
  unauthorized: "Сессия истекла — войди заново",
  "admin access revoked": "Доступ отозван — войди заново",
  "audience is empty": "В выбранной аудитории нет пользователей",
  "menu tree contains a cycle": "В дереве меню зацикленная вложенность — проверь родителей кнопок",
  "code already exists": "Такой промокод уже существует — сгенерируй другой",
  "start_param already in use": "Эта UTM-метка уже занята другой кампанией",
  "plan has subscriptions": "У тарифа есть активные подписки — выключи его вместо удаления",
  "promo group is bound to": "Группу используют активные промокоды или кампании — сначала отвяжи или выключи их",
  "promo group not found": "Такой промогруппы не существует",
  "insufficient balance": "Недостаточно средств на балансе",
  "user has no subscription": "У пользователя нет активной подписки",
  "cannot block a staff account": "Нельзя заблокировать администратора",
  "unsupported file type": "Формат файла не поддерживается (jpg, png, webp, gif, mp4)",
  "file too large": "Файл слишком большой — максимум 20 МБ",
  "panel error": "Панель Remnawave вернула ошибку — проверь адрес и токен в настройках",
  "panel sync failed": "Не удалось синхронизироваться с панелью Remnawave",
  "media broadcast requires": "Сначала загрузи файл для медиа-рассылки",
  "button needs a url or an action": "У кнопки должна быть ссылка или действие",
  "no changes": "Нет изменений для сохранения",
  "unknown config key": "Неизвестный параметр настроек — обнови страницу",
  "demo mode disabled": "Демо-режим выключен",
};

function humanError(status: number, detail: string): string {
  const lower = (detail || "").toLowerCase();
  // Panel errors need the raw backend detail (e.g. "panel 400: {...}" or "panel v3: cannot
  // resolve...") appended — it's the only place that diagnoses *why* the panel rejected the
  // call, and every other friendly message in ERR hides it on purpose.
  if (lower.includes("panel error")) {
    const base = ERR["panel error"];
    return detail ? `${base} · ${detail.slice(0, 200)}` : base;
  }
  for (const key of Object.keys(ERR)) {
    if (lower.includes(key)) return ERR[key];
  }
  const generic: Record<number, string> = {
    400: "Проверь заполнение полей — что-то введено неверно",
    401: "Сессия истекла — войди заново",
    403: "Действие запрещено для твоей роли",
    404: "Не найдено — возможно, уже удалено. Обнови страницу",
    409: "Уже существует — выбери другое имя или код",
    413: "Файл слишком большой — максимум 20 МБ",
    422: "Проверь заполнение полей — что-то введено неверно",
    500: "Внутренняя ошибка сервера — попробуй ещё раз",
    502: "Сервис недоступен (перезапуск или панель) — попробуй через минуту",
  };
  const g = generic[status];
  const base = g ?? "Неизвестная ошибка";
  return detail && detail !== "Internal Server Error" ? `${base} · ${detail.slice(0, 120)}` : base;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Нет связи с сервером — проверь интернет");
  }
  if (res.status === 401) {
    setToken(null);
    if (!location.hash.includes("login")) location.hash = "#/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = (await res.json()) as { detail?: unknown };
      if (typeof data.detail === "string") detail = data.detail;
      else if (data.detail) detail = JSON.stringify(data.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

/* ---- formatters ---- */

export function money(minor: number, currency = "RUB"): string {
  const v = minor / 100;
  const num = v.toLocaleString("ru-RU", {
    minimumFractionDigits: v % 1 ? 2 : 0,
    maximumFractionDigits: 2,
  });
  return currency === "RUB" ? `${num} ₽` : `${num} ${currency}`;
}

export function bytesFmt(n: number): string {
  if (!n) return "0";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  const v = n / 1024 ** i;
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

export function dt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}

export function dtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return (
    d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) +
    " " +
    d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
  );
}
