/* Charts: column (time series), sparkline, part-to-whole stacked bar, ranked bars.
   Specs follow the dataviz method: <=24px columns with a 4px rounded data-end on one
   baseline, hairline recessive grid, hover/focus tooltip on every mark, text in text
   tokens (never the series color), legend + direct values for part-to-whole. */

import { useEffect, useRef, useState } from "react";

export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

const reducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/* Animated number: eases from the previous value to the new one. */
export function useCountUp(target: number, duration = 700): number {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (reducedMotion()) {
      setShown(target);
      from.current = target;
      return;
    }
    const start = performance.now();
    const origin = from.current;
    let raf = 0;
    const tick = (now: number) => {
      const k = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      setShown(origin + (target - origin) * eased);
      if (k < 1) raf = requestAnimationFrame(tick);
      else from.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return shown;
}

function niceMax(max: number): number {
  if (max <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(max)));
  for (const m of [1, 2, 4, 5, 6, 8, 10]) {
    if (m * mag >= max) return m * mag;
  }
  return 10 * mag;
}

export function compact(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)} млн`;
  if (abs >= 1_000) return `${+(n / 1_000).toFixed(1)} тыс`;
  return `${+n.toFixed(1)}`;
}

function shortDate(iso: string): string {
  const [, m, d] = iso.split("-");
  return d && m ? `${d}.${m}` : iso;
}

export type ColumnPoint = { date: string; value: number };

export function ColumnChart({
  points,
  format,
  axisFormat = compact,
  height = 170,
  unit,
}: {
  points: ColumnPoint[];
  format: (v: number) => string;
  axisFormat?: (v: number) => string;
  height?: number;
  unit?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const max = niceMax(Math.max(0, ...points.map((p) => p.value)));
  const ticks = [0, max / 2, max];
  const n = points.length;
  const last = n - 1;
  const mid = Math.floor(last / 2);
  const hp = hover !== null ? points[hover] : null;
  return (
    <div className="cchart" onMouseLeave={() => setHover(null)}>
      <div className="yaxis" style={{ height }}>
        {ticks.map((tk) => (
          <span key={tk} style={{ top: `${100 - (tk / max) * 100}%` }}>
            {axisFormat(tk)}
            {unit && tk === max ? ` ${unit}` : ""}
          </span>
        ))}
      </div>
      <div className="plot" style={{ height }}>
        {ticks.slice(1).map((tk) => (
          <div key={tk} className="gridline" style={{ top: `${100 - (tk / max) * 100}%` }} />
        ))}
        <div className="cols-row">
          {points.map((p, i) => (
            <div
              key={p.date}
              className={`slot${hover !== null && hover !== i ? " dim-col" : ""}`}
              tabIndex={0}
              aria-label={`${shortDate(p.date)}: ${format(p.value)}`}
              onMouseEnter={() => setHover(i)}
              onFocus={() => setHover(i)}
              onBlur={() => setHover(null)}
            >
              <div
                className="col"
                style={{
                  height: p.value > 0 ? `max(2px, ${(p.value / max) * 100}%)` : 0,
                  animationDelay: `${Math.min(i * 14, 420)}ms`,
                  borderRadius: n > 60 ? "2px 2px 0 0" : undefined,
                }}
              />
            </div>
          ))}
        </div>
        {hp && hover !== null && (
          <div
            className="ctip"
            style={{
              left: `${((hover + 0.5) / n) * 100}%`,
              top: `${100 - (hp.value / max) * 100}%`,
            }}
          >
            <b>{format(hp.value)}</b>
            <span className="dim">{shortDate(hp.date)}</span>
          </div>
        )}
      </div>
      <div className="xaxis">
        {n > 0 && (
          <span className="first" style={{ left: 0 }}>
            {shortDate(points[0].date)}
          </span>
        )}
        {n > 2 && (
          <span style={{ left: `${((mid + 0.5) / n) * 100}%` }}>{shortDate(points[mid].date)}</span>
        )}
        {n > 1 && (
          <span className="last" style={{ left: "100%" }}>
            {shortDate(points[last].date)}
          </span>
        )}
      </div>
    </div>
  );
}

export function Sparkline({ values, height = 34 }: { values: number[]; height?: number }) {
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const w = 100;
  const step = w / (values.length - 1);
  const y = (v: number) => 30 - (v / max) * 26;
  const line = values.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(2)},${y(v).toFixed(2)}`).join("");
  const area = `${line}L${w},32L0,32Z`;
  const lastY = (y(values[values.length - 1]) / 32) * 100;
  return (
    <div className="spark" style={{ position: "relative", height }}>
      <svg viewBox="0 0 100 32" preserveAspectRatio="none" width="100%" height={height} aria-hidden>
        <path className="area" d={area} />
        <path className="line" d={line} vectorEffect="non-scaling-stroke" pathLength={600} />
      </svg>
      <span
        style={{
          position: "absolute",
          right: -4,
          top: `calc(${lastY}% - 4px)`,
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: "var(--chart-accent)",
          boxShadow: "0 0 0 2px var(--panel)",
        }}
      />
    </div>
  );
}

export type Part = { key: string; label: string; value: number };

export function StackedBar({
  parts,
  format,
  empty = "Нет данных за период",
}: {
  parts: Part[];
  format: (v: number) => string;
  empty?: string;
}) {
  const total = parts.reduce((a, p) => a + p.value, 0);
  const [hover, setHover] = useState<string | null>(null);
  if (total <= 0) return <span className="dim">{empty}</span>;
  return (
    <div>
      <div className="stack" role="img" aria-label={parts.map((p) => `${p.label}: ${format(p.value)}`).join(", ")}>
        {parts.map((p, i) =>
          p.value > 0 ? (
            <i
              key={p.key}
              title={`${p.label}: ${format(p.value)} (${Math.round((p.value / total) * 100)}%)`}
              onMouseEnter={() => setHover(p.key)}
              onMouseLeave={() => setHover(null)}
              style={{
                width: `${(p.value / total) * 100}%`,
                background: SERIES[i % SERIES.length],
                animationDelay: `${i * 80}ms`,
                opacity: hover && hover !== p.key ? 0.45 : 1,
              }}
            />
          ) : null,
        )}
      </div>
      <div className="legend">
        {parts.map((p, i) => (
          <div
            key={p.key}
            className="legend-row"
            style={{ opacity: hover && hover !== p.key ? 0.55 : 1, transition: "opacity .15s" }}
          >
            <span className="sw" style={{ background: SERIES[i % SERIES.length] }} />
            <span className="muted">{p.label}</span>
            <b style={{ fontWeight: 600 }}>{format(p.value)}</b>
            <span className="pct">{Math.round((p.value / total) * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export type RankRow = { label: string; value: number; hint?: string };

export function RankBars({
  rows,
  format,
  empty = "Нет данных",
}: {
  rows: RankRow[];
  format: (v: number) => string;
  empty?: string;
}) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  if (rows.length === 0 || max <= 0) return <span className="dim">{empty}</span>;
  return (
    <div className="rank">
      {rows.map((r, i) => (
        <div key={r.label} className="rank-row">
          <div className="rank-head">
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {r.label}
              {r.hint && <span className="dim"> · {r.hint}</span>}
            </span>
            <b style={{ fontWeight: 600 }}>{format(r.value)}</b>
          </div>
          <div className="rank-track">
            <div
              className="rank-fill"
              style={{ width: `${(r.value / max) * 100}%`, animationDelay: `${i * 60}ms` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
