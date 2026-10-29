import { CircleCheckBig } from 'lucide-react';
import { Badge } from './ui/Badge';
import { Button, buttonClasses } from './ui/Button';
import { Card } from './ui/Card';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { EmptyState } from './EmptyState';
import { needsActionGroups } from '../data/derive';
import { formatElapsed, secondsBetween } from '../data/format';
import { NEEDS_ACTION_META } from '../data/meta';
import type { ChainSummary } from '../data/types';
import { useNow } from '../state/now';
import { useView } from '../state/url';

export function NeedsActionLane({ chains }: { chains: ChainSummary[] }) {
  const now = useNow();
  const { update } = useView();
  const groups = needsActionGroups(chains);
  const total = groups.reduce((n, g) => n + g.items.length, 0);
  return (
    <Card
      as="section"
      title={
        <>
          Needs action
          <span className="rounded-full border border-border bg-surface-2 px-2 text-xs font-medium text-fg-muted tabular-nums">
            {total}
          </span>
        </>
      }
    >
      {total === 0 ? (
        <EmptyState
          Icon={CircleCheckBig}
          title="Nothing needs action"
          body="Revert PRs awaiting review, escalations and failed builds without an incident appear here, oldest first."
        />
      ) : (
        <div className="space-y-5 px-4 pt-3 pb-4 sm:px-5 sm:pb-5">
          {groups.map(({ reason, items }) => {
            const m = NEEDS_ACTION_META[reason];
            return (
              <div key={reason}>
                <h3 className="mb-2 flex items-center gap-2 text-sm font-medium text-fg-muted">
                  <m.Icon className="size-4 shrink-0" aria-hidden="true" />
                  {m.label}
                  <span className="text-xs text-fg-subtle tabular-nums">({items.length})</span>
                </h3>
                <ul className="divide-y divide-border overflow-hidden rounded-md border border-border" aria-label={m.label}>
                  {items.map((c) => {
                    const na = c.needs_action;
                    if (!na) return null;
                    return (
                      <li
                        key={c.chain_id}
                        data-needs-action
                        className="flex flex-col gap-3 bg-surface px-3 py-3 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-2">
                            <Badge tone={m.tone} Icon={m.Icon}>
                              {m.label}
                            </Badge>
                            <span className="min-w-0 truncate text-sm font-medium" title={c.title}>
                              {c.title}
                            </span>
                          </div>
                          <p className="text-xs text-fg-subtle tabular-nums">
                            <span className="font-medium text-fg-muted">
                              waiting{' '}
                              <time dateTime={na.since}>{formatElapsed(secondsBetween(na.since, now))}</time>
                            </span>
                            {' · since '}
                            <Time iso={na.since} />
                          </p>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          {na.url && (
                            <ExternalLink href={na.url} className={buttonClasses('secondary', 'sm')}>
                              Open on GitHub
                            </ExternalLink>
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={`Details for ${c.title}`}
                            onClick={() => update({ chain: c.chain_id }, { push: true })}
                          >
                            Details
                          </Button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}
