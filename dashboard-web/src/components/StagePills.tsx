import clsx from 'clsx';
import { ChevronRight } from 'lucide-react';
import { Tooltip } from './ui/Tooltip';
import { formatDuration, formatTime } from '../data/format';
import { STAGE_NAME_META, STAGE_STATUS_META, TONE_CLASSES } from '../data/meta';
import type { Stage } from '../data/types';

export function stageAriaLabel(s: Stage): string {
  return `${STAGE_NAME_META[s.name].label}: ${STAGE_STATUS_META[s.status].label} — ${s.label}`;
}

export function StagePills({ stages, compact = false }: { stages: Stage[]; compact?: boolean }) {
  return (
    <ol aria-label="Stages" className="flex flex-wrap items-center gap-1">
      {stages.map((s, i) => {
        const name = STAGE_NAME_META[s.name];
        const status = STAGE_STATUS_META[s.status];
        return (
          <li key={s.name} aria-label={stageAriaLabel(s)} className="inline-flex items-center gap-1">
            {i > 0 && <ChevronRight className="size-3 shrink-0 text-fg-subtle" aria-hidden="true" />}
            <Tooltip
              content={`${s.label} · ${s.at ? formatTime(s.at) : 'not reached'} · ${formatDuration(s.duration_s)}`}
            >
              <span
                className={clsx(
                  'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
                  TONE_CLASSES[status.tone],
                  s.status === 'none' && 'border-dashed',
                )}
              >
                <name.Icon className="size-3.5 shrink-0" aria-hidden="true" />
                <span className={clsx(compact && 'sr-only md:not-sr-only')}>{name.label}</span>
                <status.Icon
                  className={clsx('size-3.5 shrink-0', s.status === 'running' && 'motion-safe:animate-spin')}
                  aria-hidden="true"
                />
              </span>
            </Tooltip>
          </li>
        );
      })}
    </ol>
  );
}
