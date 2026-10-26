import { useId } from 'react';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';
import { FOCUS_RING } from './ui/Button';
import type { KpiFocus } from '../data/derive';
import { useView } from '../state/url';

/** A KPI that doubles as a timeline filter: pressing it focuses the chains it counts. */
export function KpiCard({
  focus,
  label,
  value,
  sub,
  Icon,
  badge,
}: {
  focus: KpiFocus;
  label: string;
  value: string;
  sub: string;
  Icon: LucideIcon;
  badge?: string;
}) {
  const { view, update } = useView();
  const pressed = view.focus === focus;
  const subId = useId();
  // Name from content ("Deploys 6", so it matches what is on screen); the secondary line is
  // read once as the description.
  return (
    <>
      <button
        type="button"
        aria-pressed={pressed}
        aria-describedby={subId}
        onClick={() => update({ focus: pressed ? null : focus })}
        className={clsx(
          'group flex min-h-[104px] flex-col items-start gap-1 rounded-lg border p-3 text-left sm:p-4',
          'transition-colors duration-[var(--dur-fast)]',
          pressed ? 'border-accent bg-accent-soft' : 'border-border bg-surface hover:border-border-strong',
          FOCUS_RING,
        )}
      >
        <span className="flex w-full items-center gap-1.5 text-xs font-medium text-fg-muted">
          <Icon className={clsx('size-3.5 shrink-0', pressed ? 'text-accent' : 'text-fg-subtle')} aria-hidden="true" />
          <span className="truncate">{label}</span>
        </span>{' '}
        <span className="flex items-baseline gap-2">
          <span className={clsx('text-2xl font-semibold tabular-nums', pressed && 'text-accent')}>{value}</span>
          {badge && (
            <span
              aria-hidden="true"
              className="rounded-full border border-border px-1.5 text-[10px] leading-4 font-medium text-fg-subtle uppercase"
            >
              {badge}
            </span>
          )}
        </span>
        <span aria-hidden="true" className="text-xs text-fg-subtle tabular-nums">
          {sub}
        </span>
      </button>
      <span id={subId} className="sr-only">
        {badge ? `${sub} (${badge})` : sub}
      </span>
    </>
  );
}
