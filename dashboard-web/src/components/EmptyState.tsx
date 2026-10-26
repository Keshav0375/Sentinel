import type { ReactNode } from 'react';
import { Inbox, type LucideIcon } from 'lucide-react';

export const TIMELINE_EMPTY = {
  title: 'No activity recorded yet',
  body:
    'When a build, deploy, incident, agent run or revert PR happens in Sentinel or Sentinel-deployment, ' +
    'it appears here as a chain: deploy → alert → agent → PR → review → merge.',
};

export function EmptyState({
  title,
  body,
  action,
  Icon = Inbox,
}: {
  title: string;
  body: string;
  action?: ReactNode;
  Icon?: LucideIcon;
}) {
  return (
    <div className="flex flex-col items-center px-4 py-10 text-center">
      <span className="mb-3 inline-flex size-12 items-center justify-center rounded-full bg-surface-2 text-fg-subtle">
        <Icon className="size-6" aria-hidden="true" />
      </span>
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mt-1 max-w-md text-sm text-fg-muted">{body}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
