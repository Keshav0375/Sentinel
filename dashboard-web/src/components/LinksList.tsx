import { Activity, GitPullRequest, ListTree } from 'lucide-react';
import { ExternalLink } from './ui/ExternalLink';
import { GitHubMark } from './ui/GitHubMark';
import type { Link, LinkKind } from '../data/types';

function KindIcon({ kind }: { kind: LinkKind }) {
  const cls = 'size-4 shrink-0 text-fg-subtle';
  switch (kind) {
    case 'github_run':
      return <GitHubMark className={cls} />;
    case 'github_pr':
      return <GitPullRequest className={cls} aria-hidden="true" />;
    case 'langfuse':
      return <ListTree className={cls} aria-hidden="true" />;
    case 'datadog':
      return <Activity className={cls} aria-hidden="true" />;
  }
}

export function LinksList({ links }: { links: Link[] }) {
  if (links.length === 0) return <p className="text-sm text-fg-muted">No links recorded.</p>;
  return (
    <div className="space-y-2">
      <ul aria-label="Links" className="space-y-2">
        {links.map((l) => (
          <li key={`${l.kind}-${l.url}`} data-link-kind={l.kind} className="flex items-center gap-2 text-sm">
            <KindIcon kind={l.kind} />
            <ExternalLink href={l.url} restricted={l.restricted}>
              {l.label}
            </ExternalLink>
          </li>
        ))}
      </ul>
      {links.some((l) => l.restricted) && (
        <p className="text-xs text-fg-subtle">
          LangFuse and Datadog are private tools; those links work only for viewers with access.
        </p>
      )}
    </div>
  );
}
