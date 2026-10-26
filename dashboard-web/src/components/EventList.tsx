import { Time } from './ui/Tooltip';
import { kindFamily } from '../data/derive';
import { KIND_FAMILY_META, KIND_LABEL } from '../data/meta';
import type { OpsEvent } from '../data/types';
import { useView } from '../state/url';

export function EventList({ events }: { events: OpsEvent[] }) {
  const { view } = useView();
  if (events.length === 0) return <p className="text-sm text-fg-muted">No events recorded.</p>;
  return (
    <ol aria-label="Events" className="relative space-y-3 border-l border-border pl-5">
      {events.map((e) => {
        const { Icon } = KIND_FAMILY_META[kindFamily(e.kind)];
        return (
          <li key={e.event_id} className="relative">
            <span className="absolute top-0.5 -left-[31px] inline-flex size-5 items-center justify-center rounded-full border border-border bg-surface text-fg-muted">
              <Icon className="size-3" aria-hidden="true" />
            </span>
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="text-sm font-medium">{KIND_LABEL(e.kind)}</span>
              <span className="font-mono text-xs text-fg-subtle">{e.kind}</span>
            </div>
            <p className="text-xs text-fg-subtle tabular-nums">
              <Time iso={e.ts} />
              {e.source && (
                <>
                  {' · '}
                  <span className="font-mono">{e.source}</span>
                </>
              )}
            </p>
            {view.raw && (
              <pre
                // A scrollable region must be keyboard-reachable (WCAG 2.1.1).
                // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
                tabIndex={0}
                aria-label={`Raw JSON for ${e.kind}`}
                className="mt-2 max-h-80 overflow-auto rounded-md bg-surface-2 p-3 font-mono text-xs"
              >
                {JSON.stringify(e, null, 2)}
              </pre>
            )}
          </li>
        );
      })}
    </ol>
  );
}
