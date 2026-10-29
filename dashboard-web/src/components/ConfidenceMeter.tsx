import clsx from 'clsx';
import type { Tone } from '../data/meta';

const FILL: Record<Tone, string> = {
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
  neutral: 'bg-neutral',
  accent: 'bg-accent',
};

export function confidenceBand(pct: number): { band: string; tone: Tone } {
  if (pct >= 80) return { band: 'High', tone: 'success' };
  if (pct >= 50) return { band: 'Medium', tone: 'warning' };
  return { band: 'Low', tone: 'danger' };
}

export function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const { band, tone } = confidenceBand(pct);
  return (
    <div className="flex min-w-48 flex-1 items-center gap-3">
      <div
        role="meter"
        aria-label="Agent confidence"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={`${pct}%, ${band}`}
        className="h-2 flex-1 overflow-hidden rounded-full bg-surface-2"
      >
        <div className={clsx('h-full rounded-full', FILL[tone])} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-sm font-medium whitespace-nowrap tabular-nums">
        {pct}% · {band}
      </span>
    </div>
  );
}

