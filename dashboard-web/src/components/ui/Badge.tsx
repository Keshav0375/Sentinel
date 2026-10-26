import type { ReactNode } from 'react';
import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';
import { CHAIN_STATUS_META, TONE_CLASSES, type Tone } from '../../data/meta';
import type { ChainStatus } from '../../data/types';

export function Badge({
  tone,
  Icon,
  children,
  className,
}: {
  tone: Tone;
  Icon?: LucideIcon;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={clsx(
        'inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap',
        TONE_CLASSES[tone],
        className,
      )}
    >
      {Icon && <Icon className="size-3.5 shrink-0" aria-hidden="true" />}
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: ChainStatus }) {
  const m = CHAIN_STATUS_META[status];
  return (
    <Badge tone={m.tone} Icon={m.Icon}>
      {m.label}
    </Badge>
  );
}
