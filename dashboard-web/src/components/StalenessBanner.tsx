import clsx from 'clsx';
import { TriangleAlert } from 'lucide-react';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { isStale } from '../data/derive';
import { formatElapsed, secondsBetween } from '../data/format';
import { TONE_CLASSES } from '../data/meta';
import type { MetaFile } from '../data/types';
import { useNow } from '../state/now';

export function StalenessBanner({ meta }: { meta: MetaFile }) {
  const now = useNow();
  if (!isStale(meta, now)) return null;
  return (
    <div role="status" className={clsx('flex items-start gap-3 rounded-lg border px-4 py-3 text-sm', TONE_CLASSES.warning)}>
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <p>
        Snapshot is {formatElapsed(secondsBetween(meta.generated_at, now))} old (generated <Time iso={meta.generated_at} />).
        The reconciler normally refreshes it every 30 minutes, so it may be delayed.
        {meta.reconciler_run_url && (
          <>
            {' '}
            <ExternalLink href={meta.reconciler_run_url} className="font-medium underline underline-offset-2">
              Last reconciler run
            </ExternalLink>
          </>
        )}
      </p>
    </div>
  );
}
