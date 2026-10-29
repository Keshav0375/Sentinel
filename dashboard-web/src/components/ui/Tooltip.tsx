import type { ReactElement, ReactNode } from 'react';
import * as RadixTooltip from '@radix-ui/react-tooltip';
import { formatTime, formatUtc } from '../../data/format';
import type { Timestamp } from '../../data/types';

/** Hover/focus hint. Never the only carrier of information. */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactElement }) {
  return (
    <RadixTooltip.Root>
      <RadixTooltip.Trigger asChild>{children}</RadixTooltip.Trigger>
      <RadixTooltip.Portal>
        <RadixTooltip.Content
          sideOffset={6}
          className="z-50 max-w-xs rounded-md bg-fg px-2 py-1 text-xs text-bg shadow-md"
        >
          {content}
          <RadixTooltip.Arrow className="fill-fg" />
        </RadixTooltip.Content>
      </RadixTooltip.Portal>
    </RadixTooltip.Root>
  );
}

/**
 * A timestamp in the viewer's local zone (or `text` — e.g. a relative "3 h ago"), with the
 * exact UTC instant in a tooltip and in the machine-readable `dateTime`.
 */
export function Time({ iso, text, className }: { iso: Timestamp; text?: string; className?: string }) {
  return (
    <Tooltip content={formatUtc(iso)}>
      <time dateTime={iso} className={className}>
        {text ?? formatTime(iso)}
      </time>
    </Tooltip>
  );
}
