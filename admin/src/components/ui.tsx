/* Shared primitives: Toggle, Segmented, KPI/Stat, Delta, Bars, Modal, Drawer, Prog, SecretInput. */

import { type CSSProperties, type ReactNode, useState } from "react";

import { Sparkline, useCountUp } from "./charts";

/* A credential/secret input that (1) does NOT use type="password", so the browser never
   autofills a saved login password into it, and (2) shows its value by default with a 👁
   toggle to mask on demand. Masking uses -webkit-text-security (a CSS mask on a text field)
   instead of type=password, keeping autofill off while still hiding the value when wanted. */
export function SecretInput({
  value,
  onChange,
  onBlur,
  placeholder,
  className,
  style,
}: {
  value: string;
  onChange: (v: string) => void;
  onBlur?: (v: string) => void;
  placeholder?: string;
  className?: string;
  style?: CSSProperties;
}) {
  const [show, setShow] = useState(true);
  return (
    <span style={{ position: "relative", display: "flex", flex: style?.flex, width: style?.width }}>
      <input
        className={className}
        style={{
          ...style,
          width: "100%",
          paddingRight: 30,
          WebkitTextSecurity: show ? "none" : "disc",
        } as CSSProperties}
        type="text"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        data-lpignore="true"
        data-1p-ignore="true"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur ? (e) => onBlur(e.target.value) : undefined}
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        title={show ? "Скрыть" : "Показать"}
        style={{
          position: "absolute",
          right: 6,
          top: "50%",
          transform: "translateY(-50%)",
          background: "none",
          border: 0,
          cursor: "pointer",
          fontSize: 13,
          lineHeight: 1,
          color: "var(--dim)",
        }}
      >
        {show ? "🙈" : "👁"}
      </button>
    </span>
  );
}

export function Toggle({
  on,
  onChange,
  lg,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  lg?: boolean;
}) {
  return (
    <button
      type="button"
      className={`toggle${on ? " on" : ""}${lg ? " lg" : ""}`}
      onClick={() => onChange(!on)}
      aria-pressed={on}
    />
  );
}

export function Seg<T extends string>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { id: T; label: string; count?: number }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.id} className={value === o.id ? "on" : ""} onClick={() => onChange(o.id)}>
          {o.label}
          {o.count !== undefined && <span className="cnt">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Kpi({
  label,
  value,
  note,
  outlined,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  outlined?: boolean;
}) {
  return (
    <div className={`kpi${outlined ? " outlined" : ""}`}>
      <div className="caps">{label}</div>
      <div className="val">{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  );
}

export function Bars({
  data,
  tips,
}: {
  data: number[];
  tips?: string[];
}) {
  const max = Math.max(1, ...data);
  return (
    <div className="bars">
      {data.map((v, i) => (
        <div
          key={i}
          className={i === data.length - 1 ? "last" : ""}
          style={{ height: `${Math.max(3, (v / max) * 100)}%` }}
        >
          {tips?.[i] && <span className="tip">{tips[i]}</span>}
        </div>
      ))}
    </div>
  );
}

export function Prog({ pct }: { pct: number }) {
  return (
    <div className="prog">
      <i style={{ width: `${Math.min(100, Math.max(0, pct))}%` }} />
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

export function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="drawer-overlay" onClick={onClose} />
      <div className="drawer">{children}</div>
    </>
  );
}

export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <label className="grid" style={{ gap: 6 }}>
      <span className="caps">{label}</span>
      {children}
    </label>
  );
}

/* Signed change vs a named period. Color = direction x whether up is good; the arrow and
   the text carry it too, so it never relies on color alone. */
export function Delta({
  pct,
  goodWhenUp = true,
  label,
}: {
  pct: number | null;
  goodWhenUp?: boolean;
  label?: string;
}) {
  if (pct === null || !Number.isFinite(pct)) return null;
  const rounded = Math.round(pct);
  const cls = rounded === 0 ? "flat" : (rounded > 0) === goodWhenUp ? "up" : "down";
  const arrow = rounded === 0 ? "→" : rounded > 0 ? "▲" : "▼";
  return (
    <span className={`delta ${cls}`}>
      {arrow} {rounded > 0 ? "+" : ""}
      {rounded}%{label && <span className="dim" style={{ fontWeight: 400 }}>&nbsp;{label}</span>}
    </span>
  );
}

export function pctChange(current: number, previous: number): number | null {
  if (previous <= 0) return current > 0 ? null : 0;
  return ((current - previous) / previous) * 100;
}

/* Stat tile: label, count-up value, optional delta / note / sparkline. */
export function Stat({
  icon,
  label,
  value,
  format = (n) => Math.round(n).toLocaleString("ru-RU"),
  delta = null,
  goodWhenUp = true,
  deltaLabel,
  note,
  spark,
  onClick,
}: {
  icon?: string;
  label: string;
  value: number | null;
  format?: (n: number) => string;
  delta?: number | null;
  goodWhenUp?: boolean;
  deltaLabel?: string;
  note?: ReactNode;
  spark?: number[];
  onClick?: () => void;
}) {
  const shown = useCountUp(value ?? 0);
  return (
    <div
      className={`kpi${onClick ? " link" : ""}`}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}
    >
      <div className="kpi-top">
        {icon && <span className="kpi-ico">{icon}</span>}
        <span className="caps">{label}</span>
      </div>
      <div className="val">{value === null ? <span className="sk" style={{ width: 90 }} /> : format(shown)}</div>
      {(delta !== null || note) && (
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <Delta pct={delta} goodWhenUp={goodWhenUp} label={deltaLabel} />
          {note && <span className="note">{note}</span>}
        </div>
      )}
      {spark && spark.length > 1 && <Sparkline values={spark} />}
    </div>
  );
}
