import type { ReactNode } from 'react';
import clsx from 'clsx';
import { ExternalLink as ExternalIcon, Lock } from 'lucide-react';
import { FOCUS_RING } from './Button';

export const LINK_CLASSES = `rounded-sm font-medium text-accent underline-offset-2 hover:text-accent-hover hover:underline ${FOCUS_RING}`;

/** Opens in a new tab and says so; `restricted` links point at private tools and say that too. */
export function ExternalLink({
  href,
  children,
  restricted = false,
  className,
}: {
  href: string;
  children: ReactNode;
  restricted?: boolean;
  className?: string;
}) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <a href={href} target="_blank" rel="noopener noreferrer" className={clsx('inline-flex items-center gap-1 rounded-sm', FOCUS_RING, className ?? LINK_CLASSES)}>
        {children}
        <ExternalIcon className="size-3.5 shrink-0" aria-hidden="true" />
        {' '}
        <span className="sr-only">(opens in a new tab)</span>
      </a>
      {restricted && (
        <span className="inline-flex items-center gap-1 text-xs text-fg-subtle">
          <Lock className="size-3 shrink-0" aria-hidden="true" />
          may require access
        </span>
      )}
    </span>
  );
}
