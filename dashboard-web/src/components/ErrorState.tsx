import clsx from 'clsx';
import { CircleAlert, RefreshCw } from 'lucide-react';
import { Button } from './ui/Button';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import type { SnapshotError } from '../data/client';
import { formatAgo } from '../data/format';
import type { MetaFile } from '../data/types';
import { useNow } from '../state/now';

const RECONCILER_RUNS = 'https://github.com/Keshav0375/Sentinel/actions/workflows/ops_reconcile.yml';

export function ErrorState({
  error,
  meta,
  onRetry,
  compact = false,
  title,
  message,
}: {
  error: SnapshotError;
  meta: MetaFile | null;
  onRetry: () => void;
  compact?: boolean;
  /** Overrides the heading (the reload-failed banner). */
  title?: string;
  /** Overrides `error.message` (e.g. a chain missing from the snapshot). */
  message?: string;
}) {
  const now = useNow();
  const heading = title ?? (compact ? "Couldn't load this chain" : "Couldn't load the dashboard");
  return (
    <div
      role="alert"
      className={clsx(
        'rounded-lg border border-danger/30 bg-surface shadow-sm',
        compact ? 'p-4' : 'mx-auto max-w-xl p-6 sm:p-8',
      )}
    >
      <div className="flex items-start gap-3">
        <CircleAlert className={clsx('shrink-0 text-danger', compact ? 'mt-0.5 size-5' : 'mt-0.5 size-6')} aria-hidden="true" />
        <div className="min-w-0 flex-1 space-y-2">
          <h2 className={clsx('font-semibold', compact ? 'text-sm' : 'text-base')}>{heading}</h2>
          <p className="text-sm text-fg-muted">{message ?? error.message}</p>
          {!compact && (
            <p className="text-xs text-fg-subtle">
              {meta ? (
                <>
                  Last snapshot: generated <Time iso={meta.generated_at} />, {formatAgo(meta.generated_at, now)}
                </>
              ) : (
                'Snapshot age unknown'
              )}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
            <Button variant="primary" size={compact ? 'sm' : 'md'} onClick={onRetry}>
              <RefreshCw className="size-4" aria-hidden="true" />
              Retry
            </Button>
            {!compact && <ExternalLink href={RECONCILER_RUNS}>Check the reconciler runs</ExternalLink>}
          </div>
        </div>
      </div>
    </div>
  );
}
