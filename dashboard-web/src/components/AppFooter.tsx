import { CircleAlert, CircleCheck, CircleMinus } from 'lucide-react';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { formatCount, shortSha } from '../data/format';
import type { MetaFile, SourceStatus } from '../data/types';

function Source({ name, status }: { name: string; status: SourceStatus }) {
  const Icon = status === 'ok' ? CircleCheck : status === 'error' ? CircleAlert : CircleMinus;
  return (
    <span className="inline-flex items-center gap-1">
      <Icon
        className={`size-3.5 ${status === 'ok' ? 'text-success' : status === 'error' ? 'text-danger' : 'text-fg-subtle'}`}
        aria-hidden="true"
      />
      {name} {status}
    </span>
  );
}

export function AppFooter({ meta }: { meta: MetaFile | null }) {
  return (
    <footer className="mt-10 border-t border-border">
      <div className="mx-auto max-w-7xl space-y-3 px-4 py-6 text-xs text-fg-subtle sm:px-6 lg:px-8">
        {meta && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 tabular-nums">
            <span>
              Snapshot generated <Time iso={meta.generated_at} />
            </span>
            {meta.ledger_head && (
              <span>
                Ledger head{' '}
                <ExternalLink href={`https://github.com/Keshav0375/Sentinel/commit/${meta.ledger_head}`} className="font-mono text-accent hover:underline">
                  {shortSha(meta.ledger_head)}
                </ExternalLink>
              </span>
            )}
            <span>
              {formatCount(meta.event_count)} events · {formatCount(meta.chain_count)} chains
            </span>
            <span className="inline-flex items-center gap-2">
              Sources <Source name="GitHub" status={meta.sources.github} /> · <Source name="LangFuse" status={meta.sources.langfuse} />
            </span>
          </p>
        )}
        <p>
          Read-only view. Merging and approvals happen on GitHub. Public data only: no logs, secrets or raw tool output.
        </p>
        <p className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <ExternalLink href="https://github.com/Keshav0375/Sentinel/tree/main/dashboard-web">Source</ExternalLink>
          <span className="hidden sm:inline">
            Press <kbd className="rounded border border-border bg-surface-2 px-1 font-mono">/</kbd> to search
          </span>
          <span>Times are in your local time zone; hover one for UTC.</span>
        </p>
      </div>
    </footer>
  );
}
