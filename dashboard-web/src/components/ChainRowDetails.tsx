import { Maximize2 } from 'lucide-react';
import { Button } from './ui/Button';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { formatDuration, formatPct } from '../data/format';
import { STAGE_NAME_META, STAGE_STATUS_META } from '../data/meta';
import type { ChainSummary } from '../data/types';
import { useView } from '../state/url';

function Id({ label, value }: { label: string; value: string | number | null }) {
  if (value === null) return null;
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="font-mono text-xs break-all">{value}</dd>
    </div>
  );
}

export function ChainRowDetails({ id, chain, showOpen = true }: { id?: string; chain: ChainSummary; showOpen?: boolean }) {
  const { update } = useView();
  return (
    <div id={id} role="region" aria-label={`Stages of ${chain.title}`} className="space-y-4">
      <dl className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {chain.stages.map((s) => {
          const name = STAGE_NAME_META[s.name];
          const status = STAGE_STATUS_META[s.status];
          return (
            <div key={s.name} className="rounded-md border border-border bg-surface px-3 py-2">
              <dt className="flex items-center gap-1.5 text-xs font-medium text-fg-muted">
                <name.Icon className="size-3.5 shrink-0" aria-hidden="true" />
                {name.label}
                <span className="text-fg-subtle">·</span>
                <status.Icon className="size-3.5 shrink-0" aria-hidden="true" />
                {status.label}
              </dt>
              <dd className="mt-1 space-y-0.5 text-sm">
                <p>{s.label}</p>
                <p className="text-xs text-fg-subtle tabular-nums">
                  {s.at ? <Time iso={s.at} /> : '—'}
                  {s.duration_s !== null && ` · ${formatDuration(s.duration_s)}`}
                </p>
                {s.url && (
                  <p className="text-xs">
                    <ExternalLink href={s.url}>Open</ExternalLink>
                  </p>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
      {chain.summary && <p className="text-sm text-fg-muted">{chain.summary}</p>}
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
        <Id label="Chain" value={chain.chain_id} />
        <Id label="Correlation" value={chain.correlation_id} />
        <Id label="Incident" value={chain.incident_id} />
        <Id label="Run" value={chain.run_id} />
        <Id label="Commit" value={chain.sha} />
      </dl>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-fg-subtle tabular-nums">
        <span>
          {chain.event_count} {chain.event_count === 1 ? 'event' : 'events'}
        </span>
        {chain.confidence !== null && <span>Agent confidence {formatPct(chain.confidence)}</span>}
        {chain.eval_total !== null && <span>Eval score {formatPct(chain.eval_total)}</span>}
        {showOpen && (
          <Button variant="primary" size="sm" className="ml-auto" onClick={() => update({ chain: chain.chain_id }, { push: true })}>
            <Maximize2 className="size-3.5" aria-hidden="true" />
            Open full chain
          </Button>
        )}
      </div>
    </div>
  );
}
