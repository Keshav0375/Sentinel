import clsx from 'clsx';
import { Info } from 'lucide-react';
import { TONE_CLASSES } from '../data/meta';
import type { MetaFile } from '../data/types';

export function SourceNotice({ meta }: { meta: MetaFile }) {
  const notes: string[] = [];
  if (meta.sources.github === 'error') {
    notes.push('GitHub could not be read in the last refresh; recent runs and PRs may be missing.');
  }
  if (meta.sources.langfuse === 'error') {
    notes.push('LangFuse could not be read in the last refresh; traces may be missing.');
  }
  if (notes.length === 0) return null;
  return (
    <div role="status" className={clsx('flex items-start gap-3 rounded-lg border px-4 py-3 text-sm', TONE_CLASSES.info)}>
      <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="space-y-1">
        {notes.map((n) => (
          <p key={n}>{n}</p>
        ))}
      </div>
    </div>
  );
}
