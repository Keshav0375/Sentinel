import clsx from 'clsx';
import { RefreshCw, Shield } from 'lucide-react';
import { Button, buttonClasses } from './ui/Button';
import { GitHubMark } from './ui/GitHubMark';
import { Tooltip } from './ui/Tooltip';
import { ThemeToggle } from './ThemeToggle';
import { formatAgo, formatUtc, shortSha } from '../data/format';
import type { MetaFile } from '../data/types';
import { useNow } from '../state/now';

export function AppHeader({
  meta,
  refreshing,
  onReload,
}: {
  meta: MetaFile | null;
  refreshing: boolean;
  onReload: () => void;
}) {
  const now = useNow();
  return (
    <header className="sticky top-0 z-20 border-b border-border bg-bg/85 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex min-w-0 items-center gap-3">
          <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-md bg-accent text-accent-fg shadow-sm">
            <Shield className="size-4.5" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h1 className="text-lg font-semibold tracking-tight sm:text-xl">Sentinel Ops</h1>
            <p className="hidden truncate text-xs text-fg-subtle sm:block">
              Builds, incidents, agent runs and revert PRs, end to end
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {meta ? (
            <Tooltip
              content={`Generated ${formatUtc(meta.generated_at)}${meta.ledger_head ? ` · ledger ${shortSha(meta.ledger_head)}` : ''}`}
            >
              <span className="inline-flex h-8 items-center rounded-full border border-border bg-surface px-3 text-xs whitespace-nowrap text-fg-muted tabular-nums">
                Updated&nbsp;
                <time dateTime={meta.generated_at}>{formatAgo(meta.generated_at, now)}</time>
              </span>
            </Tooltip>
          ) : (
            <span className="inline-flex h-8 items-center rounded-full border border-border bg-surface px-3 text-xs text-fg-muted">
              <span aria-hidden="true">—</span>
              <span className="sr-only">Snapshot not loaded</span>
            </span>
          )}
          <Button size="icon" variant="ghost" aria-label="Reload snapshot" disabled={refreshing} onClick={onReload}>
            <RefreshCw className={clsx('size-4', refreshing && 'motion-safe:animate-spin')} aria-hidden="true" />
          </Button>
          <ThemeToggle />
          <a
            href="https://github.com/Keshav0375/Sentinel"
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Sentinel on GitHub"
            className={buttonClasses('ghost', 'icon')}
          >
            <GitHubMark className="size-5" />
          </a>
        </div>
      </div>
    </header>
  );
}
