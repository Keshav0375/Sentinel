import { useEffect, useState } from 'react';
import { format, parseISO } from 'date-fns';
import { Bar, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { DailyPoint } from '../data/types';
import { useTheme } from '../state/theme';

const VARS = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--border', '--fg-subtle', '--fg', '--surface', '--surface-2'] as const;
type Palette = Record<(typeof VARS)[number], string>;

/** SVG presentation attributes do not reliably resolve var(), so read the computed tokens. */
function readPalette(): Palette {
  const css = getComputedStyle(document.documentElement);
  return Object.fromEntries(VARS.map((v) => [v, css.getPropertyValue(v).trim() || `var(${v})`])) as Palette;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

const dayLabel = (date: string) => format(parseISO(date), 'd MMM');

export default function TrendChart({ daily }: { daily: DailyPoint[] }) {
  const { resolved } = useTheme();
  const [c, setPalette] = useState<Palette>(readPalette);
  useEffect(() => setPalette(readPalette()), [resolved]);
  const animate = !prefersReducedMotion();

  return (
    <div className="h-48" aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 192 }}>
        <ComposedChart accessibilityLayer={false} data={daily} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={c['--border']} strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={dayLabel}
            tick={{ fill: c['--fg-subtle'], fontSize: 12 }}
            stroke={c['--border']}
            tickLine={false}
          />
          <YAxis allowDecimals={false} tick={{ fill: c['--fg-subtle'], fontSize: 12 }} stroke={c['--border']} tickLine={false} />
          <Tooltip
            labelFormatter={(d) => dayLabel(String(d))}
            cursor={{ fill: c['--surface-2'] }}
            contentStyle={{
              background: c['--surface'],
              border: `1px solid ${c['--border']}`,
              borderRadius: 8,
              color: c['--fg'],
              fontSize: 12,
            }}
            labelStyle={{ color: c['--fg'], fontWeight: 600 }}
          />
          <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12, color: c['--fg-subtle'] }} />
          <Bar dataKey="deploys" name="Deploys" fill={c['--chart-1']} radius={[3, 3, 0, 0]} maxBarSize={18} isAnimationActive={animate} />
          <Bar
            dataKey="deploy_failures"
            name="Failed deploys"
            fill={c['--chart-2']}
            radius={[3, 3, 0, 0]}
            maxBarSize={18}
            isAnimationActive={animate}
          />
          <Bar
            dataKey="revert_prs_merged"
            name="Merged revert PRs"
            fill={c['--chart-4']}
            radius={[3, 3, 0, 0]}
            maxBarSize={18}
            isAnimationActive={animate}
          />
          <Line
            dataKey="incidents"
            name="Incidents"
            type="monotone"
            stroke={c['--chart-3']}
            strokeWidth={2}
            dot={{ r: 3, fill: c['--chart-3'] }}
            isAnimationActive={animate}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
