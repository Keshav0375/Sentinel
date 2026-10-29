import { useLayoutEffect, useRef, useState, type MouseEvent } from 'react';
import type { DailyPoint } from '../data/types';

// Hand-rolled SVG so the chart costs a few KB instead of a charting library. Decorative only: the
// numbers reach assistive tech through DailyActivity's sr-only table, so the whole chart is
// aria-hidden and holds nothing focusable. Colours are CSS properties (not presentation
// attributes), so var() resolves and a theme switch repaints without re-rendering.

type SeriesKey = 'deploys' | 'deploy_failures' | 'revert_prs_merged' | 'incidents';
const BARS: { key: SeriesKey; name: string; color: string }[] = [
  { key: 'deploys', name: 'Deploys', color: 'var(--chart-1)' },
  { key: 'deploy_failures', name: 'Failed deploys', color: 'var(--chart-2)' },
  { key: 'revert_prs_merged', name: 'Merged revert PRs', color: 'var(--chart-4)' },
];
const LINE = { key: 'incidents', name: 'Incidents', color: 'var(--chart-3)' } as const;
const SERIES = [...BARS, LINE];

const HEIGHT = 168; // h-48 (192 px) minus the legend row
const FALLBACK_WIDTH = 640; // before layout, and in jsdom
const M = { top: 12, right: 8, bottom: 24, left: 32 };
const MAX_BAR = 18;
const BAR_GAP = 4;
const RADIUS = 3;
const TICK_LABEL_W = 44; // room one "25 Oct" label needs before the next

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** `2026-10-05` → `5 Oct` (the date is a UTC day; no zone conversion). */
export function dayLabel(date: string): string {
  const [, m, d] = date.split('-').map(Number);
  return `${d} ${MONTHS[(m ?? 1) - 1] ?? ''}`;
}

/** Integer ticks from 0 with a 1/2/5 × 10ⁿ step, about four intervals. */
export function yTicks(max: number): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? 10 * mag);
  const top = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= top; t += step) ticks.push(t);
  return ticks;
}

/** Bar with rounded top corners; `h` > 0. */
function barPath(x: number, y: number, w: number, h: number): string {
  const r = Math.min(RADIUS, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** Monotone cubic through the points, as d3's curveMonotoneX (Steffen 1990): never overshoots. */
export function monotonePath(xs: number[], ys: number[]): string {
  const n = Math.min(xs.length, ys.length);
  const x = (i: number) => xs[i] ?? 0;
  const y = (i: number) => ys[i] ?? 0;
  if (n === 0) return '';
  if (n === 1) return `M${x(0)},${y(0)}`;
  if (n === 2) return `M${x(0)},${y(0)}L${x(1)},${y(1)}`;
  const tangent: number[] = [];
  for (let i = 1; i < n - 1; i++) {
    const h0 = x(i) - x(i - 1);
    const h1 = x(i + 1) - x(i);
    const s0 = (y(i) - y(i - 1)) / (h0 || 1);
    const s1 = (y(i + 1) - y(i)) / (h1 || 1);
    const p = (s0 * h1 + s1 * h0) / (h0 + h1 || 1);
    tangent[i] = (Math.sign(s0) + Math.sign(s1)) * Math.min(Math.abs(s0), Math.abs(s1), 0.5 * Math.abs(p)) || 0;
  }
  const end = (i: number, j: number, t: number) => {
    const h = x(j) - x(i);
    return h ? ((3 * (y(j) - y(i))) / h - t) / 2 : t;
  };
  tangent[0] = end(0, 1, tangent[1] ?? 0);
  tangent[n - 1] = end(n - 2, n - 1, tangent[n - 2] ?? 0);
  let d = `M${x(0)},${y(0)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = (x(i + 1) - x(i)) / 3;
    d += `C${x(i) + h},${y(i) + h * (tangent[i] ?? 0)} ${x(i + 1) - h},${y(i + 1) - h * (tangent[i + 1] ?? 0)} ${x(i + 1)},${y(i + 1)}`;
  }
  return d;
}

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(FALLBACK_WIDTH);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const read = () => setWidth(el.clientWidth || FALLBACK_WIDTH);
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width] as const;
}

export default function TrendChart({ daily }: { daily: DailyPoint[] }) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);

  const n = daily.length;
  const plotW = Math.max(1, width - M.left - M.right);
  const plotH = HEIGHT - M.top - M.bottom;
  const band = plotW / Math.max(1, n);
  const ticks = yTicks(Math.max(0, ...daily.flatMap((d) => SERIES.map((s) => d[s.key]))));
  const top = ticks[ticks.length - 1] ?? 1;
  const y = (v: number) => M.top + plotH - (v / top) * plotH;
  const cx = (i: number) => M.left + band * (i + 0.5);

  const inner = band * 0.8;
  const gap = Math.min(BAR_GAP, inner / 12);
  const barW = Math.max(1, Math.min(MAX_BAR, (inner - gap * (BARS.length - 1)) / BARS.length));
  const groupW = barW * BARS.length + gap * (BARS.length - 1);
  const every = Math.max(1, Math.ceil(TICK_LABEL_W / band));

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const x = e.clientX - e.currentTarget.getBoundingClientRect().left - M.left;
    const i = Math.floor(x / band);
    setHover(i >= 0 && i < n ? i : null);
  };

  const point = hover === null ? undefined : daily[hover];
  const tipLeft = hover === null ? 0 : cx(hover) + band / 2 + 8;
  const tipFlip = tipLeft + 168 > width;

  return (
    <div ref={ref} className="relative h-48" aria-hidden="true" data-trend-chart>
      <svg width={width} height={HEIGHT} className="block" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {hover !== null && (
          <rect x={M.left + band * hover} y={M.top} width={band} height={plotH} style={{ fill: 'var(--surface-2)' }} />
        )}
        <g style={{ stroke: 'var(--border)' }} strokeDasharray="3 3">
          {ticks.map((t) => (
            <line key={t} x1={M.left} x2={M.left + plotW} y1={y(t)} y2={y(t)} />
          ))}
        </g>
        <g style={{ fill: 'var(--fg-subtle)' }} fontSize={12}>
          {ticks.map((t) => (
            <text key={t} x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end">
              {t}
            </text>
          ))}
          {daily.map((d, i) =>
            (n - 1 - i) % every === 0 ? (
              <text key={d.date} x={cx(i)} y={M.top + plotH + 16} textAnchor="middle">
                {dayLabel(d.date)}
              </text>
            ) : null,
          )}
        </g>
        <path d={`M${M.left},${M.top}V${M.top + plotH}H${M.left + plotW}`} style={{ fill: 'none', stroke: 'var(--border)' }} />
        {BARS.map((s, k) => (
          <g key={s.key} style={{ fill: s.color }} data-series={s.key}>
            {daily.map((d, i) => {
              const v = d[s.key];
              if (v <= 0) return null;
              const x = cx(i) - groupW / 2 + k * (barW + gap);
              return <path key={d.date} d={barPath(x, y(v), barW, M.top + plotH - y(v))} />;
            })}
          </g>
        ))}
        <g data-series={LINE.key}>
          <path
            d={monotonePath(daily.map((_, i) => cx(i)), daily.map((d) => y(d[LINE.key])))}
            style={{ fill: 'none', stroke: LINE.color }}
            strokeWidth={2}
          />
          {daily.map((d, i) => (
            <circle key={d.date} cx={cx(i)} cy={y(d[LINE.key])} r={3} style={{ fill: LINE.color }} />
          ))}
        </g>
      </svg>
      <ul className="flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs text-fg-subtle">
        {SERIES.map((s) => (
          <li key={s.key} className="flex items-center gap-1.5">
            <span className="size-2 rounded-full" style={{ background: s.color }} />
            {s.name}
          </li>
        ))}
      </ul>
      {hover !== null && point && (
        <div
          className="pointer-events-none absolute top-2 z-10 w-max rounded-lg border border-border bg-surface px-2.5 py-1.5 text-xs text-fg shadow-md"
          style={tipFlip ? { right: width - (cx(hover) - band / 2 - 8) } : { left: tipLeft }}
        >
          <p className="mb-0.5 font-semibold">{dayLabel(point.date)}</p>
          {SERIES.map((s) => (
            <p key={s.key} className="flex items-center gap-1.5">
              <span className="size-2 rounded-full" style={{ background: s.color }} />
              {s.name}: <span className="font-medium tabular-nums">{point[s.key]}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
